/**
 * API 层：tenant-admin REST（管理面）+ agent-runtime（LangGraph SDK 数据面）。
 *
 * 所有请求走 Vite 开发代理（同源）。协议事实（对上游 f9f3127d 侦察确认）：
 * - SDK apiUrl = "/api"，网关原生路由 /api/threads*、/api/threads/{id}/runs/stream
 * - assistantId = "lead_agent"（前端 useThreadStream 默认值）
 * - CSRF 在 DEER_FLOW_AUTH_DISABLED=1 下整体豁免（csrf_middleware.should_check_csrf）
 * - 流模式 "messages-tuple"：event "messages"，data = [chunk, metadata]；
 *   流式块 type 是 "AIMessageChunk"（state 完整消息才是 "ai"）
 * - GET /api/threads/{id}/artifacts{path}?download=true —— 交付物下载（owner
 *   校验）；agent 写沙箱 outputs/ 并调 present_files 后进入 state.values.artifacts
 *
 * 租户身份：token 由 tenant-admin 签发（可选 user claim → 用户级身份）。
 * 本 UI 是本地演示控制台，直接向管理面申请 token；生产部署应在管理面
 * 前加操作员认证（X-Admin-Key / SSO），由服务端下发 token。
 */

import { Client } from "@langchain/langgraph-sdk/client";

import type {
  AuthHeaders,
  LeadAgentInfo,
  MeInfo,
  ModelInfo,
  SkillPayload,
  SkillRow,
  SubagentPayload,
  SubagentRow,
  TenantRow,
  TokenIssued,
  UserRow,
} from "./types";

export const ASSISTANT_ID = "lead_agent";

export function authHeaders(token: string, tenantId: string): AuthHeaders {
  return {
    Authorization: `Bearer ${token}`,
    // 一致性头：中间件要求其必须与 JWT claim 一致（伪造则 403）
    "x-tenant-id": tenantId,
  };
}

// ── tenant-admin（管理面）──────────────────────────────────────────────────

function adminHeaders(adminKey: string, bearer?: string): Record<string, string> {
  const headers: Record<string, string> = {};
  if (adminKey) headers["x-admin-key"] = adminKey;
  // 智能体配置端点要求「操作员 key 或管理员 Bearer」；普通用户 token 会被 403
  if (bearer) headers.authorization = `Bearer ${bearer}`;
  return headers;
}

async function adminJson<T>(path: string, init: RequestInit, adminKey: string, bearer?: string): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...adminHeaders(adminKey, bearer), ...init.headers },
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      /* keep status-only detail */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const adminApi = {
  listTenants: (adminKey: string) =>
    adminJson<{ tenants: TenantRow[] }>("/admin/tenants", { method: "GET" }, adminKey),

  createTenant: (id: string, name: string, adminKey: string) =>
    adminJson<TenantRow>(
      "/admin/tenants",
      { method: "POST", body: JSON.stringify({ id, name }) },
      adminKey,
    ),

  setTenantStatus: (id: string, action: "disable" | "enable", adminKey: string) =>
    adminJson<TenantRow>(`/admin/tenants/${id}/${action}`, { method: "POST" }, adminKey),

  deleteTenant: (id: string, adminKey: string) =>
    adminJson<{ id: string }>(`/admin/tenants/${id}`, { method: "DELETE" }, adminKey),

  /** 签发租户级（user=null）或用户级 token；role 随响应返回 */
  issueToken: (id: string, adminKey: string, user: string | null = null) =>
    adminJson<TokenIssued>(
      `/admin/tenants/${id}/token`,
      { method: "POST", body: JSON.stringify(user ? { user } : {}) },
      adminKey,
    ),

  listUsers: (id: string, adminKey: string) =>
    adminJson<{ users: UserRow[] }>(`/admin/tenants/${id}/users`, { method: "GET" }, adminKey),

  createUser: (id: string, username: string, displayName: string, adminKey: string, role: string = "user") =>
    adminJson<UserRow>(
      `/admin/tenants/${id}/users`,
      { method: "POST", body: JSON.stringify({ username, display_name: displayName, role }) },
      adminKey,
    ),

  /** 改角色 / 显示名 —— 角色是权限轴的开关 */
  updateUser: (id: string, username: string, body: { role?: string; display_name?: string }, adminKey: string) =>
    adminJson<UserRow>(`/admin/tenants/${id}/users/${username}`, { method: "PUT", body: JSON.stringify(body) }, adminKey),

  deleteUser: (id: string, username: string, adminKey: string) =>
    adminJson<{ username: string }>(`/admin/tenants/${id}/users/${username}`, { method: "DELETE" }, adminKey),

  // ── 智能体配置：管理面 CRUD，物化为租户根目录下 deerflow 原生格式 ──────
  // 这些端点要求管理员身份：操作员 key，或与租户匹配的管理员 Bearer token
  // （租户级 token 天然是管理员；普通用户 token 403）。调用方传 session
  // token 作为 bearer。

  /** leadAgent 灵魂：SOUL.md 内容（null = 未覆盖，用默认人格） */
  getLeadAgent: (id: string, adminKey: string, bearer?: string) =>
    adminJson<LeadAgentInfo>(`/admin/tenants/${id}/lead-agent`, { method: "GET" }, adminKey, bearer),

  putLeadAgent: (id: string, soulMd: string, adminKey: string, bearer?: string) =>
    adminJson<{ soul_md: string | null }>(
      `/admin/tenants/${id}/lead-agent`,
      { method: "PUT", body: JSON.stringify({ soul_md: soulMd }) },
      adminKey,
      bearer,
    ),

  listSubagents: (id: string, adminKey: string, bearer?: string) =>
    adminJson<{ subagents: SubagentRow[] }>(`/admin/tenants/${id}/subagents`, { method: "GET" }, adminKey, bearer),

  createSubagent: (id: string, payload: SubagentPayload, adminKey: string, bearer?: string) =>
    adminJson<{ name: string }>(
      `/admin/tenants/${id}/subagents`,
      { method: "POST", body: JSON.stringify(payload) },
      adminKey,
      bearer,
    ),

  updateSubagent: (id: string, name: string, payload: SubagentPayload, adminKey: string, bearer?: string) =>
    adminJson<{ name: string; enabled: boolean }>(
      `/admin/tenants/${id}/subagents/${name}`,
      { method: "PUT", body: JSON.stringify(payload) },
      adminKey,
      bearer,
    ),

  deleteSubagent: (id: string, name: string, adminKey: string, bearer?: string) =>
    adminJson<{ name: string }>(`/admin/tenants/${id}/subagents/${name}`, { method: "DELETE" }, adminKey, bearer),

  listSkills: (id: string, adminKey: string, bearer?: string) =>
    adminJson<{ skills: SkillRow[] }>(`/admin/tenants/${id}/skills`, { method: "GET" }, adminKey, bearer),

  createSkill: (id: string, payload: SkillPayload, adminKey: string, bearer?: string) =>
    adminJson<{ name: string }>(
      `/admin/tenants/${id}/skills`,
      { method: "POST", body: JSON.stringify(payload) },
      adminKey,
      bearer,
    ),

  updateSkill: (id: string, name: string, payload: SkillPayload, adminKey: string, bearer?: string) =>
    adminJson<{ name: string; enabled: boolean }>(
      `/admin/tenants/${id}/skills/${name}`,
      { method: "PUT", body: JSON.stringify(payload) },
      adminKey,
      bearer,
    ),

  deleteSkill: (id: string, name: string, adminKey: string, bearer?: string) =>
    adminJson<{ name: string }>(`/admin/tenants/${id}/skills/${name}`, { method: "DELETE" }, adminKey, bearer),
};

// ── agent-runtime（数据面）─────────────────────────────────────────────────

/**
 * SDK 每次请求都在内部执行 new URL(`${apiUrl}${path}`)（无 base 参数），
 * 相对路径 "/api" 会直接抛 TypeError: Invalid URL —— 请求根本发不出去
 * （界面正常、devtools 里却没有任何调用）。必须传绝对地址；同源代理下
 * 直接取页面 origin 即可，请求仍走 Vite 代理、无 CORS。
 */
export function makeClient(token: string, tenantId: string): Client {
  return new Client({
    apiUrl: `${window.location.origin}/api`,
    defaultHeaders: authHeaders(token, tenantId),
  });
}

/** 租户身份回显：P10 补丁使 /me 返回 tenant-{tid}[-{user}] 合成身份 —— UI 内的隔离证明。 */
export async function fetchMe(token: string, tenantId: string): Promise<MeInfo | null> {
  try {
    const res = await fetch("/api/v1/auth/me", { headers: authHeaders(token, tenantId) });
    if (!res.ok) return null;
    return (await res.json()) as MeInfo;
  } catch {
    return null;
  }
}

/** 可用模型列表（GET /api/models）。选中的 name 通过 run 的 context.model_name 下发，
 *  网关按 config.yaml 的 models: 允许列表校验（未知名称 → 400）。 */
export async function fetchModels(token: string, tenantId: string): Promise<ModelInfo[]> {
  try {
    const res = await fetch("/api/models", { headers: authHeaders(token, tenantId) });
    if (!res.ok) return [];
    const body = (await res.json()) as { models?: ModelInfo[] };
    return body.models ?? [];
  } catch {
    return [];
  }
}

// ── 交付物（artifacts，来自 state.values.artifacts）────────────────────────

/**
 * 虚拟路径（/mnt/user-data/outputs/报告.md）→ 网关 artifacts 路由的编码路径。
 * 对齐上游 encodeArtifactPath：逐段 encodeURIComponent、保留 /（中文文件名安全）。
 */
function encodeArtifactPath(virtualPath: string): string {
  return virtualPath
    .split("/")
    .map((segment) => {
      try {
        return encodeURIComponent(decodeURIComponent(segment));
      } catch {
        return encodeURIComponent(segment);
      }
    })
    .join("/");
}

/** GET /api/threads/{tid}/artifacts{path}?download=true（owner 校验）。 */
export function artifactUrl(threadId: string, virtualPath: string, download: boolean): string {
  const base = `/api/threads/${encodeURIComponent(threadId)}/artifacts${encodeArtifactPath(virtualPath)}`;
  return download ? `${base}?download=true` : base;
}

/** 交付物显示名：虚拟路径的最后一段（state 里已是解码后的原文）。 */
export function artifactFileName(virtualPath: string): string {
  return virtualPath.split("/").pop() || virtualPath || "artifact";
}

/**
 * 拉取交付物为 Blob。网关要求 Bearer + x-tenant-id，直链 <a href> 带不上
 * Authorization 头 —— 必须先 fetch 成 blob 再交给浏览器保存/预览。
 */
export async function fetchArtifactBlob(
  token: string,
  tenantId: string,
  threadId: string,
  virtualPath: string,
  download: boolean,
): Promise<Blob> {
  const res = await fetch(artifactUrl(threadId, virtualPath, download), {
    headers: authHeaders(token, tenantId),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      /* 非 JSON 错误体（如 404 纯文本） */
    }
    throw new Error(`交付物获取失败：${detail}`);
  }
  return res.blob();
}

/** Blob → 浏览器保存（objectURL + 隐藏 <a download> 点击，稍后回收）。 */
export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

// ── 消息文本提取（对齐上游 extractTextFromMessage 的简化版）───────────────

/** 去掉思考模型的 <think> 段（流式期间未闭合的 think 之后全部视为思考内容）。 */
function stripThink(text: string): string {
  let out = text.replace(/<think>[\s\S]*?<\/think>/g, "");
  const open = out.lastIndexOf("<think>");
  if (open !== -1 && !out.includes("</think>", open)) out = out.slice(0, open);
  return out;
}

export function extractText(content: unknown): string {
  if (typeof content === "string") return stripThink(content);
  if (Array.isArray(content)) {
    return content
      .map((block) => {
        if (typeof block === "string") return block;
        if (block && typeof block === "object" && (block as { type?: string }).type === "text") {
          return String((block as { text?: string }).text ?? "");
        }
        return "";
      })
      .join("");
  }
  return "";
}
