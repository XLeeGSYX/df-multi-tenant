/**
 * 用户对话界面：顶栏（右上角切换租户 / 进入管理）+ 左侧对话历史 + 中间对话。
 *
 * 全部经 LangGraph SDK 调 agent-runtime（同源代理 → :8001），每个请求自动带
 * 当前身份的租户 JWT。线程按 owner 轴隔离：不同租户（或同租户不同用户）
 * 看到各自的线程列表。
 *
 * 流式协议（对上游侦察确认）：
 * - POST /api/threads（幂等创建，owner = P10 合成用户）
 * - POST /api/threads/{id}/runs/stream，assistantId="lead_agent"，
 *   streamMode=["messages-tuple"] → event "messages"，data=[chunk, metadata]
 * - GET  /api/threads/{id}/state → values.messages（权威历史）+
 *   values.artifacts（交付物虚拟路径，present_files 归并而来）
 */

import type { Message, Thread } from "@langchain/langgraph-sdk";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ASSISTANT_ID,
  artifactFileName,
  extractText,
  fetchArtifactBlob,
  fetchMe,
  fetchModels,
  makeClient,
  saveBlob,
} from "./api";
import { useSession } from "./session";
import type { MeInfo, ModelInfo, UIMessage } from "./types";

/** 模型选择的本地记忆键（全局，不区分租户/用户）。 */
const MODEL_STORAGE_KEY = "dfmt.modelName";

/** 可内联预览的图片扩展名（SVG 属活动内容，排除 —— blob: 同源，防脚本执行）。 */
const IMAGE_PREVIEW_EXT = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]);
/** 可按纯文本预览的扩展名。 */
const TEXT_PREVIEW_EXT = new Set([
  ".txt",
  ".md",
  ".csv",
  ".json",
  ".log",
  ".py",
  ".js",
  ".ts",
  ".yaml",
  ".yml",
  ".toml",
]);

/** 预览策略：图片原样内联；文本包成 text/plain；其余（html/svg 等活动内容）只下载。 */
function previewKindOf(fileName: string): "image" | "text" | null {
  const dot = fileName.lastIndexOf(".");
  const ext = dot === -1 ? "" : fileName.slice(dot).toLowerCase();
  if (IMAGE_PREVIEW_EXT.has(ext)) return "image";
  if (TEXT_PREVIEW_EXT.has(ext)) return "text";
  return null;
}

interface ChatProps {
  onOpenAdmin: () => void;
}

interface WireMessage {
  type?: string;
  content?: unknown;
  id?: string;
  name?: string;
  tool_calls?: { name?: string }[];
  tool_call_chunks?: { name?: string | null }[];
}

/**
 * 线上消息类型（实测）：state 里是 "system"/"human"/"ai"/"tool"；
 * 流式增量是 LangChain chunk 类名 "AIMessageChunk"（type !== "ai"）。
 */
function roleOf(type: string | undefined): UIMessage["role"] | null {
  if (type === "human") return "human";
  if (type === "tool" || type === "ToolMessage") return "tool";
  if (type === "ai" || type === "AIMessage" || type === "AIMessageChunk") return "assistant";
  return null; // system 等：不展示
}

function toolNames(msg: WireMessage): string[] {
  const names = [
    ...(msg.tool_calls ?? []).map((tc) => String(tc.name ?? "tool")),
    ...(msg.tool_call_chunks ?? [])
      .map((tc) => String(tc.name ?? "").trim())
      .filter(Boolean),
  ];
  return Array.from(new Set(names));
}

function toUIMessages(wire: WireMessage[]): UIMessage[] {
  const out: UIMessage[] = [];
  for (const msg of wire) {
    const role = roleOf(msg.type);
    if (!role) continue;
    const text = extractText(msg.content);
    const tools = toolNames(msg);
    if (role === "assistant" && !text && tools.length === 0) continue;
    if (role === "tool") {
      out.push({
        key: msg.id ?? `tool-${out.length}`,
        role,
        text: msg.name ?? (text.slice(0, 120) || "tool"),
        tools: [],
      });
      continue;
    }
    if (role === "human" && !text) continue;
    out.push({ key: msg.id ?? `hist-${out.length}`, role, text, tools });
  }
  return out;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/** 右上角租户切换菜单（本地演示控制台：切换 = 向管理面重新签发 token）。 */
function TenantMenu({ disabled, onOpenAdmin }: { disabled: boolean; onOpenAdmin: () => void }) {
  const session = useSession();
  const [open, setOpen] = useState(false);
  const active = (session.tenants ?? []).filter((t) => t.status === "active");

  return (
    <div className="menu">
      <button
        type="button"
        className="menu-trigger"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        租户：{session.tenantId ?? "未选择"} ▾
      </button>
      {open && (
        <>
          <div className="menu-backdrop" onClick={() => setOpen(false)} />
          <ul className="menu-list">
            {active.map((t) => (
              <li key={t.id} className={t.id === session.tenantId ? "active" : ""}>
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    void session.selectIdentity(t.id, null);
                  }}
                >
                  <strong>{t.id}</strong> <span className="hint">{t.name}</span>
                </button>
              </li>
            ))}
            {active.length === 0 && <li className="hint menu-note">暂无启用中的租户</li>}
            <li className="menu-sep" />
            <li>
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  onOpenAdmin();
                }}
              >
                管理租户与用户…
              </button>
            </li>
          </ul>
        </>
      )}
    </div>
  );
}

export default function Chat({ onOpenAdmin }: ChatProps) {
  const session = useSession();
  const { token, tenantId, user, ensureToken } = session;
  const client = useMemo(
    () => (token && tenantId ? makeClient(token, tenantId) : null),
    [token, tenantId],
  );
  const [me, setMe] = useState<MeInfo | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelName, setModelName] = useState<string>(() => {
    try {
      return localStorage.getItem(MODEL_STORAGE_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [artifacts, setArtifacts] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    void ensureToken(); // 临期自动重签
  }, [ensureToken]);

  const reloadThreads = useCallback(async () => {
    if (!client) return;
    try {
      const rows = await client.threads.search({ limit: 50 });
      setThreads(rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [client]);

  useEffect(() => {
    if (!client || !tenantId) return;
    setMe(null);
    void fetchMe(token ?? "", tenantId).then(setMe);
    void reloadThreads();
    // 模型列表来自 config.yaml 的 models:（经 /api/models）；持久化的选择若已
    // 不在列表中（配置变了）则回落到第一个。
    void fetchModels(token ?? "", tenantId).then((list) => {
      setModels(list);
      setModelName((current) =>
        current && list.some((m) => m.name === current) ? current : (list[0]?.name ?? ""),
      );
    });
  }, [client, tenantId, token, reloadThreads]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const loadThreadState = useCallback(
    async (tid: string) => {
      if (!client) return;
      setLoadingHistory(true);
      try {
        const state = await client.threads.getState(tid);
        const values = (state.values ?? {}) as {
          messages?: WireMessage[];
          artifacts?: unknown;
        };
        setMessages(toUIMessages(values.messages ?? []));
        // 交付物：agent 调 present_files 后由 merge_artifacts 归并（服务端已
        // 按序去重）；这里再过滤非字符串并去重一手，防线双保险。
        const arts = Array.isArray(values.artifacts)
          ? values.artifacts.filter((p): p is string => typeof p === "string")
          : [];
        setArtifacts(Array.from(new Set(arts)));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoadingHistory(false);
      }
    },
    [client],
  );

  useEffect(() => {
    if (threadId) void loadThreadState(threadId);
    else {
      setMessages([]);
      setArtifacts([]);
    }
  }, [threadId, loadThreadState]);

  function upsertStreamMessage(next: UIMessage) {
    setMessages((prev) => {
      const idx = prev.findIndex((m) => m.key === next.key);
      if (idx === -1) return [...prev, next];
      const copy = [...prev];
      copy[idx] = next;
      return copy;
    });
  }

  function handleChunk(chunk: WireMessage) {
    if (chunk.type === "human") return; // 本地已乐观渲染
    const role = roleOf(chunk.type);
    if (role === "tool") {
      upsertStreamMessage({
        key: chunk.id ?? `tool-${Date.now()}`,
        role: "tool",
        text: chunk.name ?? "tool",
        tools: [],
      });
      return;
    }
    if (role !== "assistant") return;
    // AI 增量块：按消息 id 累加文本（思考增量 reasoning_content 不在 content 里，自然跳过）
    const key = chunk.id ?? "__live__";
    setMessages((prev) => {
      const idx = prev.findIndex((m) => m.key === key);
      const delta = extractText(chunk.content);
      const tools = toolNames(chunk);
      if (idx === -1) {
        if (!delta && tools.length === 0) return prev;
        return [...prev, { key, role: "assistant", text: delta, tools, streaming: true }];
      }
      const existing = prev[idx];
      const copy = [...prev];
      copy[idx] = {
        ...existing,
        text: existing.text + delta,
        tools: tools.length ? Array.from(new Set([...existing.tools, ...tools])) : existing.tools,
        streaming: true,
      };
      return copy;
    });
  }

  /**
   * 交付物操作：预览（新标签页 blob）/ 下载（保存到本地）。
   * 网关要求 Bearer 头，直链带不上 Authorization —— 必须 fetch 成 blob 再交给
   * 浏览器；活动内容（html/svg 等）只允许下载路径（blob: 同源，防脚本执行）。
   */
  async function handleArtifact(virtualPath: string, mode: "preview" | "download") {
    if (!threadId || !tenantId) return;
    setError(null);
    const freshToken = await ensureToken();
    if (!freshToken) {
      setError("获取租户身份失败，请重试");
      return;
    }
    const fileName = artifactFileName(virtualPath);
    const kind = previewKindOf(fileName);
    const wantDownload = mode === "download" || kind === null;
    try {
      const blob = await fetchArtifactBlob(freshToken, tenantId, threadId, virtualPath, wantDownload);
      if (wantDownload) {
        saveBlob(blob, fileName);
        return;
      }
      const previewBlob =
        kind === "text" ? new Blob([await blob.text()], { type: "text/plain;charset=utf-8" }) : blob;
      const url = URL.createObjectURL(previewBlob);
      if (!window.open(url, "_blank")) saveBlob(previewBlob, fileName); // 弹窗被拦截时退化为下载
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy || !tenantId) return;
    setError(null);
    setInput("");

    const freshToken = await ensureToken();
    if (!freshToken) {
      setError("获取租户身份失败，请在右上角重新选择租户");
      return;
    }

    let tid = threadId;
    try {
      const runClient = makeClient(freshToken, tenantId);
      if (!tid) {
        const created = await runClient.threads.create();
        tid = created.thread_id;
        setThreadId(tid);
      }
      const targetTid = tid;
      setMessages((prev) => [
        ...prev,
        { key: `local-${Date.now()}`, role: "human", text, tools: [] },
      ]);
      setBusy(true);

      const stream = runClient.runs.stream(targetTid, ASSISTANT_ID, {
        input: {
          messages: [{ type: "human", content: [{ type: "text", text }] }],
        },
        config: { recursion_limit: 1000 },
        // 按请求选模型：网关按 config.yaml 的 models: 允许列表校验（未知 → 400）
        context: modelName ? { model_name: modelName } : undefined,
        streamMode: ["messages-tuple"],
      });

      for await (const ev of stream) {
        if (ev.event === "messages") {
          const [chunk] = ev.data as [Message, unknown];
          handleChunk(chunk as WireMessage);
        } else if (ev.event === "error") {
          const data = ev.data as { message?: string; error?: string };
          throw new Error(data.message ?? data.error ?? "运行失败");
        }
      }

      // 流结束后用权威 state 覆盖（工具消息、标题等以服务端为准）
      await loadThreadState(targetTid);
      await reloadThreads();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const hasIdentity = Boolean(token && tenantId);

  return (
    <div className="workspace">
      <header className="topbar">
        <div className="topbar-left">
          <span className="brand-mark">DeerFlow 多租户</span>
          <span className="tag tenant">{tenantId ?? "未选择租户"}</span>
          {user && (
            <span className="tag">
              用户 {user}
              {session.role === "admin" ? "（管理员）" : ""}
            </span>
          )}
        </div>
        <div className="topbar-right">
          {hasIdentity && (
            <span className="identity-chip" title={me?.email ?? ""}>
              身份 {me ? me.id : `${tenantId}…`}
            </span>
          )}
          <TenantMenu disabled={busy} onOpenAdmin={onOpenAdmin} />
          <button type="button" onClick={onOpenAdmin}>
            管理
          </button>
        </div>
      </header>

      <div className="workspace-body">
        <aside className="sidebar">
          <button
            type="button"
            className="primary block"
            disabled={!hasIdentity}
            onClick={() => {
              setThreadId(null);
              setMessages([]);
              setArtifacts([]);
            }}
          >
            ＋ 新建对话
          </button>

          <ul className="thread-list">
            {threads.map((t) => (
              <li key={t.thread_id}>
                <button
                  type="button"
                  className={t.thread_id === threadId ? "active" : ""}
                  onClick={() => setThreadId(t.thread_id)}
                >
                  <span className="thread-title">
                    {(t.values as { title?: string } | undefined)?.title?.trim() ||
                      `对话 ${t.thread_id.slice(0, 8)}`}
                  </span>
                  <span className="thread-time">
                    {formatTime(t.updated_at ?? t.created_at ?? "")}
                  </span>
                </button>
              </li>
            ))}
            {hasIdentity && threads.length === 0 && <li className="hint">还没有对话</li>}
          </ul>

          <div className="sidebar-footer">
            <p className="hint">
              对话历史按租户/用户隔离；文件落盘 <code>.foundation/&#123;tenant&#125;/</code>
            </p>
          </div>
        </aside>

        <main className="chat">
          {!hasIdentity ? (
            <div className="empty-state">
              {session.mintError ? (
                <>
                  <h2>身份获取失败</h2>
                  <p className="error">{session.mintError}</p>
                  <button type="button" className="primary" onClick={() => void session.refreshTenants()}>
                    重试
                  </button>
                </>
              ) : session.tenantsError ? (
                <>
                  <h2>无法连接管理面（tenant-admin）</h2>
                  <p className="error">{session.tenantsError}</p>
                  <p className="hint">请确认 make admin 已启动；若配置了 X-Admin-Key，请到管理界面填写。</p>
                  <button type="button" className="primary" onClick={onOpenAdmin}>
                    打开管理界面
                  </button>
                </>
              ) : (session.tenants ?? []).length === 0 ? (
                <>
                  <h2>还没有租户</h2>
                  <p className="hint">先到管理界面创建一个租户（可选再建用户），然后回来开始对话。</p>
                  <button type="button" className="primary" onClick={onOpenAdmin}>
                    去创建租户
                  </button>
                </>
              ) : (
                <>
                  <h2>选择租户</h2>
                  <p className="hint">点击右上角「租户」菜单选择一个租户开始对话。</p>
                </>
              )}
            </div>
          ) : (
            <>
              <div className="chat-header">
                <span>{threadId ? `线程 ${threadId.slice(0, 8)}` : "新对话"}</span>
                <div className="chat-header-actions">
                  <select
                    className="model-select"
                    value={modelName}
                    disabled={busy || models.length === 0}
                    title="对话使用的模型（config.yaml 的 models: 列表；每条消息即时生效）"
                    aria-label="选择模型"
                    onChange={(e) => {
                      setModelName(e.target.value);
                      try {
                        localStorage.setItem(MODEL_STORAGE_KEY, e.target.value);
                      } catch {
                        /* 无痕模式等场景忽略 */
                      }
                    }}
                  >
                    {models.length === 0 && <option value="">默认模型</option>}
                    {models.map((m) => (
                      <option key={m.name} value={m.name}>
                        {m.display_name ?? m.name}
                      </option>
                    ))}
                  </select>
                  <span className="hint">Enter 发送 · Shift+Enter 换行</span>
                </div>
              </div>

              <div className="messages">
                {loadingHistory && <p className="hint center">加载历史…</p>}
                {!loadingHistory && messages.length === 0 && (
                  <p className="hint center">向 lead_agent 发送第一条消息</p>
                )}
                {messages.map((m) =>
                  m.role === "tool" ? (
                    <div key={m.key} className="tool-chip">
                      🔧 {m.text}
                    </div>
                  ) : (
                    <div key={m.key} className={`bubble ${m.role}`}>
                      {m.tools.length > 0 && (
                        <div className="bubble-tools">
                          {m.tools.map((name) => (
                            <span key={name}>🔧 {name}</span>
                          ))}
                        </div>
                      )}
                      <div className="bubble-text">
                        {m.text || (m.streaming ? "…" : "")}
                        {m.streaming && m.text && <span className="cursor">▍</span>}
                      </div>
                    </div>
                  ),
                )}
                {busy && <p className="hint center">运行中…</p>}
                <div ref={bottomRef} />
              </div>

              {artifacts.length > 0 && (
                <section className="artifacts-bar" aria-label="交付物">
                  <span className="artifacts-bar-title">📎 交付物（{artifacts.length}）</span>
                  {artifacts.map((p) => {
                    const name = artifactFileName(p);
                    const kind = previewKindOf(name);
                    return (
                      <div key={p} className="artifact-row">
                        <span className="artifact-name" title={p}>
                          {name}
                        </span>
                        {kind && (
                          <button type="button" onClick={() => void handleArtifact(p, "preview")}>
                            预览
                          </button>
                        )}
                        <button type="button" onClick={() => void handleArtifact(p, "download")}>
                          下载
                        </button>
                      </div>
                    );
                  })}
                </section>
              )}

              {error && <p className="error center">{error}</p>}

              <footer className="composer">
                <textarea
                  rows={2}
                  placeholder="输入消息…（Enter 发送，Shift+Enter 换行）"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                />
                <button
                  type="button"
                  className="primary"
                  disabled={busy || !input.trim()}
                  onClick={() => void send()}
                >
                  发送
                </button>
              </footer>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
