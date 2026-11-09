/**
 * 管理界面：租户管理 + 用户管理 + 智能体配置（管理面，经 Vite 代理 → tenant-admin :8010）。
 *
 * - 租户管理：建租户（同步 provision 目录）/ 停启用 / 删除 / 以租户身份进入对话
 * - 用户管理：租户内用户注册表；建用户（可选角色）/ 改角色 / 删除 / 以该用户身份进入对话
 *   （用户级 token 带 user + role claim → 数据面 owner 轴变为 tenant-{tid}-{user}）
 * - 智能体配置：leadAgent / subAgent / skill —— 仅管理员可操作。管理面端点要求
 *   「操作员 key 或与租户匹配的管理员 Bearer」：普通用户 token 会 403，界面
 *   对非管理员身份直接显示提示卡（权限隔离的 UI 侧）。
 */

import { useCallback, useEffect, useState } from "react";

import { adminApi } from "./api";
import { useSession } from "./session";
import {
  SKILL_NAME_RE,
  SUBAGENT_NAME_RE,
  TENANT_ID_RE,
  USER_NAME_RE,
  type SkillPayload,
  type SkillRow,
  type SubagentPayload,
  type SubagentRow,
  type TenantRow,
  type UserRow,
} from "./types";

interface AdminViewProps {
  onBack: () => void;
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** 智能体配置调用的 Bearer：当前会话 token（临期自动重签）。 */
async function adminBearer(session: ReturnType<typeof useSession>): Promise<string | undefined> {
  return (await session.ensureToken()) ?? undefined;
}

// ── 租户管理 ───────────────────────────────────────────────────────────────

function TenantsTab({ onEnter }: { onEnter: () => void }) {
  const session = useSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newId, setNewId] = useState("");
  const [newName, setNewName] = useState("");

  const idValid = TENANT_ID_RE.test(newId);

  async function run(action: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const tenants = session.tenants ?? [];

  return (
    <section className="card">
      <h2>租户</h2>
      <p className="hint">
        建租户会在 <code>.foundation/&#123;tenant&#125;/</code> 下开通资源目录；「进入对话」以该租户身份打开对话界面。
      </p>

      <div className="row">
        <input
          placeholder="租户 ID（如 acme）"
          value={newId}
          onChange={(e) => setNewId(e.target.value.toLowerCase())}
        />
        <input
          placeholder="名称（如 ACME Corp）"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
        />
        <button
          type="button"
          className="primary"
          disabled={busy || !idValid || !newName.trim()}
          onClick={() =>
            void run(async () => {
              await adminApi.createTenant(newId, newName.trim(), session.adminKey);
              setNewId("");
              setNewName("");
              await session.refreshTenants();
            })
          }
        >
          创建
        </button>
      </div>
      {newId && !idValid && <p className="field-error">租户 ID 需匹配 ^[a-z][a-z0-9-]&#123;1,62&#125;$</p>}

      {error && <p className="error">管理面请求失败：{error}</p>}

      {tenants.length === 0 && <p className="hint">暂无租户，先创建一个。</p>}
      <ul className="tenant-list">
        {tenants.map((t) => (
          <li key={t.id} className={t.status === "active" ? "" : "muted"}>
            <div className="tenant-meta">
              <strong>{t.id}</strong>
              <span className="tag">{t.status === "active" ? "启用" : "已停用"}</span>
              <span className="hint">{t.name}</span>
            </div>
            <div className="tenant-actions">
              <button
                type="button"
                className="primary"
                disabled={busy || t.status !== "active" || t.id === session.tenantId}
                onClick={() =>
                  void run(async () => {
                    await session.selectIdentity(t.id, null);
                    onEnter();
                  })
                }
              >
                {t.id === session.tenantId ? "当前租户" : "进入对话"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await adminApi.setTenantStatus(
                      t.id,
                      t.status === "active" ? "disable" : "enable",
                      session.adminKey,
                    );
                    await session.refreshTenants();
                  })
                }
              >
                {t.status === "active" ? "停用" : "启用"}
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    if (!window.confirm(`确认删除租户 ${t.id}？其目录会移入 _trash（可人工恢复）。`)) return;
                    await adminApi.deleteTenant(t.id, session.adminKey);
                    await session.refreshTenants();
                  })
                }
              >
                删除
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ── 用户管理 ───────────────────────────────────────────────────────────────

function UsersTab({ onEnter }: { onEnter: () => void }) {
  const session = useSession();
  const activeTenants = (session.tenants ?? []).filter((t) => t.status === "active");
  const [tenantId, setTenantId] = useState<string>("");
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newUser, setNewUser] = useState("");
  const [newDisplay, setNewDisplay] = useState("");
  const [newRole, setNewRole] = useState("user");

  // 默认选中当前租户（或第一个启用的）
  useEffect(() => {
    if (!tenantId && activeTenants.length > 0) {
      setTenantId(session.tenantId && activeTenants.some((t) => t.id === session.tenantId)
        ? session.tenantId
        : activeTenants[0].id);
    }
  }, [tenantId, activeTenants, session.tenantId]);

  const reload = useCallback(
    async (tid: string) => {
      setBusy(true);
      setError(null);
      try {
        const { users: rows } = await adminApi.listUsers(tid, session.adminKey);
        setUsers(rows);
      } catch (err) {
        setUsers(null);
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [session.adminKey],
  );

  useEffect(() => {
    if (tenantId) void reload(tenantId);
    else setUsers(null);
  }, [tenantId, reload]);

  async function run(action: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const userValid = USER_NAME_RE.test(newUser);

  if (activeTenants.length === 0) {
    return (
      <section className="card">
        <h2>用户</h2>
        <p className="hint">先在「租户管理」创建（或启用）一个租户，再为它管理用户。</p>
      </section>
    );
  }

  return (
    <section className="card">
      <h2>用户</h2>
      <p className="hint">
        用户属于某个租户；「进入对话」会签发带 <code>user</code> + <code>role</code> claim 的 token，
        对话历史（owner 轴）即与租户级身份、其他用户互相隔离。<strong>只有管理员角色的用户可以配置
        智能体</strong>，普通用户只能跑流程（数据面的技能/子智能体变更接口同样只认管理员）。
      </p>

      <div className="row">
        <label className="field-label" htmlFor="user-tenant-select">
          租户
        </label>
        <select
          id="user-tenant-select"
          value={tenantId}
          onChange={(e) => setTenantId(e.target.value)}
        >
          {activeTenants.map((t: TenantRow) => (
            <option key={t.id} value={t.id}>
              {t.id}（{t.name}）
            </option>
          ))}
        </select>
      </div>

      <div className="row">
        <input
          placeholder="用户名（如 alice）"
          value={newUser}
          onChange={(e) => setNewUser(e.target.value.toLowerCase())}
        />
        <input
          placeholder="显示名（如 Alice Chen）"
          value={newDisplay}
          onChange={(e) => setNewDisplay(e.target.value)}
        />
        <select value={newRole} onChange={(e) => setNewRole(e.target.value)} aria-label="角色">
          <option value="user">普通用户</option>
          <option value="admin">管理员</option>
        </select>
        <button
          type="button"
          className="primary"
          disabled={busy || !userValid}
          onClick={() =>
            void run(async () => {
              await adminApi.createUser(tenantId, newUser, newDisplay.trim(), session.adminKey, newRole);
              setNewUser("");
              setNewDisplay("");
              setNewRole("user");
              await reload(tenantId);
            })
          }
        >
          创建用户
        </button>
      </div>
      {newUser && !userValid && <p className="field-error">用户名需匹配 ^[a-z][a-z0-9-]&#123;0,31&#125;$</p>}

      {error && <p className="error">管理面请求失败：{error}</p>}

      <div className="row">
        <button type="button" disabled={busy} onClick={() => void reload(tenantId)}>
          刷新
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              await session.selectIdentity(tenantId, null);
              onEnter();
            })
          }
        >
          以租户身份进入
        </button>
      </div>

      {users !== null && users.length === 0 && <p className="hint">该租户还没有用户。</p>}
      <ul className="tenant-list">
        {(users ?? []).map((u) => (
          <li key={u.username}>
            <div className="tenant-meta">
              <strong>{u.username}</strong>
              <span className={u.role === "admin" ? "tag" : "hint"}>{u.role === "admin" ? "管理员" : "普通用户"}</span>
              <span className="hint">{u.display_name || "—"}</span>
              {session.tenantId === tenantId && session.user === u.username && (
                <span className="tag tenant">当前身份</span>
              )}
            </div>
            <div className="tenant-actions">
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await adminApi.updateUser(
                      tenantId,
                      u.username,
                      { role: u.role === "admin" ? "user" : "admin" },
                      session.adminKey,
                    );
                    await reload(tenantId);
                  })
                }
              >
                {u.role === "admin" ? "设为普通用户" : "设为管理员"}
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await session.selectIdentity(tenantId, u.username);
                    onEnter();
                  })
                }
              >
                进入对话
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    if (!window.confirm(`确认删除用户 ${u.username}（属于 ${tenantId}）？`)) return;
                    await adminApi.deleteUser(tenantId, u.username, session.adminKey);
                    await reload(tenantId);
                  })
                }
              >
                删除
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ── 智能体配置 ─────────────────────────────────────────────────────────────

/** leadAgent 灵魂（SOUL.md）编辑器：人格描述，下一条消息即生效。 */
function SoulCard({ tenantId }: { tenantId: string }) {
  const session = useSession();
  const [draft, setDraft] = useState<string>("");
  const [loadedTenant, setLoadedTenant] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!tenantId) return;
    let cancelled = false;
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        const bearer = await adminBearer(session);
        const info = await adminApi.getLeadAgent(tenantId, session.adminKey, bearer);
        if (cancelled) return;
        setDraft(info.soul_md ?? "");
        setLoadedTenant(tenantId);
        setSavedAt(info.updated_at);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenantId, session]);

  if (!tenantId) return null;

  return (
    <section className="card">
      <h2>LeadAgent 灵魂（SOUL.md）</h2>
      <p className="hint">
        写入后物化为 <code>.foundation/&#123;tenant&#125;/SOUL.md</code>，deerflow 每次构建智能体时重新读取 ——
        <strong>下一条消息即生效</strong>，无需重启。留空保存 = 清除覆盖，恢复平台默认人格。
      </p>
      {savedAt && loadedTenant === tenantId && (
        <p className="hint">最近更新：{new Date(savedAt).toLocaleString()}</p>
      )}
      <textarea
        rows={8}
        placeholder={"# 灵魂\n你是一名严谨的中文助理，回答保持简洁。"}
        value={loadedTenant === tenantId ? draft : ""}
        onChange={(e) => setDraft(e.target.value)}
      />
      {error && <p className="error">管理面请求失败：{error}</p>}
      <div className="row">
        <button
          type="button"
          className="primary"
          disabled={busy || loadedTenant !== tenantId}
          onClick={() => {
            setBusy(true);
            setError(null);
            void (async () => {
              try {
                const bearer = await adminBearer(session);
                const res = await adminApi.putLeadAgent(tenantId, draft, session.adminKey, bearer);
                setDraft(res.soul_md ?? "");
                setSavedAt(new Date().toISOString());
              } catch (err) {
                setError(errorMessage(err));
              } finally {
                setBusy(false);
              }
            })();
          }}
        >
          保存灵魂
        </button>
      </div>
    </section>
  );
}

/** 子智能体列表：创建 / 编辑 / 删除 + 是否参与对话开关。 */
function SubagentsCard({ tenantId }: { tenantId: string }) {
  const session = useSession();
  const [rows, setRows] = useState<SubagentRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [newName, setNewName] = useState("");
  const [newDisplay, setNewDisplay] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newPrompt, setNewPrompt] = useState("");

  const reload = useCallback(
    async (tid: string) => {
      setBusy(true);
      setError(null);
      try {
        const bearer = await adminBearer(session);
        const { subagents } = await adminApi.listSubagents(tid, session.adminKey, bearer);
        setRows(subagents);
      } catch (err) {
        setRows(null);
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [session],
  );

  useEffect(() => {
    if (tenantId) void reload(tenantId);
    else setRows(null);
  }, [tenantId, reload]);

  async function run(action: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (!tenantId) return null;

  const nameValid = SUBAGENT_NAME_RE.test(newName);

  return (
    <section className="card">
      <h2>子智能体（subAgent）</h2>
      <p className="hint">
        物化为 <code>managed-subagents/&#123;name&#125;.json</code>；leadAgent 委派任务时可选用。
        「参与对话」关闭后，注册表会在下一次构建时把它从可用子智能体里排除。
      </p>

      <div className="row">
        <button type="button" disabled={busy} onClick={() => setCreating(!creating)}>
          {creating ? "收起创建表单" : "新建子智能体"}
        </button>
        <button type="button" disabled={busy} onClick={() => void reload(tenantId)}>
          刷新
        </button>
      </div>

      {creating && (
        <div className="card nested">
          <div className="row">
            <input
              placeholder="名称（如 researcher）"
              value={newName}
              onChange={(e) => setNewName(e.target.value.toLowerCase())}
            />
            <input
              placeholder="显示名（如 研究员）"
              value={newDisplay}
              onChange={(e) => setNewDisplay(e.target.value)}
            />
          </div>
          {newName && !nameValid && <p className="field-error">名称需匹配 ^[a-z0-9][a-z0-9-]&#123;0,62&#125;$</p>}
          <input
            placeholder="描述（供 leadAgent 决定何时委派，必填）"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
          />
          <textarea
            rows={4}
            placeholder="系统提示词（该子智能体的人格与职责，必填）"
            value={newPrompt}
            onChange={(e) => setNewPrompt(e.target.value)}
          />
          <div className="row">
            <button
              type="button"
              className="primary"
              disabled={busy || !nameValid || !newDesc.trim() || !newPrompt.trim()}
              onClick={() =>
                void run(async () => {
                  const bearer = await adminBearer(session);
                  await adminApi.createSubagent(
                    tenantId,
                    {
                      name: newName,
                      display_name: newDisplay.trim(),
                      description: newDesc.trim(),
                      system_prompt: newPrompt,
                    },
                    session.adminKey,
                    bearer,
                  );
                  setNewName("");
                  setNewDisplay("");
                  setNewDesc("");
                  setNewPrompt("");
                  setCreating(false);
                  await reload(tenantId);
                })
              }
            >
              创建
            </button>
          </div>
        </div>
      )}

      {error && <p className="error">管理面请求失败：{error}</p>}
      {rows !== null && rows.length === 0 && <p className="hint">该租户还没有自定义子智能体（内置 general-purpose / bash 始终可用）。</p>}
      <ul className="tenant-list">
        {(rows ?? []).map((s) => (
          <li key={s.name} className={s.enabled ? "" : "muted"}>
            <div className="tenant-meta">
              <strong>{s.name}</strong>
              <span className="tag">{s.enabled ? "参与对话" : "已停用"}</span>
              <span className="hint">{s.display_name || s.description}</span>
            </div>
            <div className="tenant-actions">
              <button
                type="button"
                className={s.enabled ? "" : "primary"}
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const bearer = await adminBearer(session);
                    await adminApi.updateSubagent(tenantId, s.name, { enabled: !s.enabled }, session.adminKey, bearer);
                    await reload(tenantId);
                  })
                }
              >
                {s.enabled ? "停止参与" : "恢复参与"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setEditing(editing === s.name ? null : s.name)}
              >
                {editing === s.name ? "收起" : "编辑"}
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    if (!window.confirm(`确认删除子智能体 ${s.name}？`)) return;
                    const bearer = await adminBearer(session);
                    await adminApi.deleteSubagent(tenantId, s.name, session.adminKey, bearer);
                    await reload(tenantId);
                  })
                }
              >
                删除
              </button>
            </div>
            {editing === s.name && (
              <SubagentEditForm
                row={s}
                busy={busy}
                onSave={async (payload) => {
                  const bearer = await adminBearer(session);
                  await adminApi.updateSubagent(tenantId, s.name, payload, session.adminKey, bearer);
                  setEditing(null);
                  await reload(tenantId);
                }}
              />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function SubagentEditForm({
  row,
  busy,
  onSave,
}: {
  row: SubagentRow;
  busy: boolean;
  onSave: (payload: SubagentPayload) => Promise<void>;
}) {
  const [display, setDisplay] = useState(row.display_name ?? "");
  const [description, setDescription] = useState(row.description);
  const [prompt, setPrompt] = useState(row.system_prompt);

  return (
    <div className="card nested">
      <div className="row">
        <input placeholder="显示名" value={display} onChange={(e) => setDisplay(e.target.value)} />
      </div>
      <input
        placeholder="描述（供 leadAgent 决定何时委派）"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <textarea
        rows={4}
        placeholder="系统提示词"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
      />
      <div className="row">
        <button
          type="button"
          className="primary"
          disabled={busy || !description.trim() || !prompt.trim()}
          onClick={() =>
            void onSave({
              display_name: display.trim(),
              description: description.trim(),
              system_prompt: prompt,
            })
          }
        >
          保存修改
        </button>
      </div>
    </div>
  );
}

/** 技能列表：创建 / 编辑 / 删除 + 是否参与对话开关。 */
function SkillsCard({ tenantId }: { tenantId: string }) {
  const session = useSession();
  const [rows, setRows] = useState<SkillRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newContent, setNewContent] = useState("");

  const reload = useCallback(
    async (tid: string) => {
      setBusy(true);
      setError(null);
      try {
        const bearer = await adminBearer(session);
        const { skills } = await adminApi.listSkills(tid, session.adminKey, bearer);
        setRows(skills);
      } catch (err) {
        setRows(null);
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [session],
  );

  useEffect(() => {
    if (tenantId) void reload(tenantId);
    else setRows(null);
  }, [tenantId, reload]);

  async function run(action: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (!tenantId) return null;

  const nameValid = SKILL_NAME_RE.test(newName);

  return (
    <section className="card">
      <h2>技能（skill）</h2>
      <p className="hint">
        物化为 <code>skills/custom/&#123;name&#125;/SKILL.md</code>，租户内所有用户共享；
        开启的技能会注入 leadAgent 的系统提示（技能清单随对话生效，无需重启）。
      </p>

      <div className="row">
        <button type="button" disabled={busy} onClick={() => setCreating(!creating)}>
          {creating ? "收起创建表单" : "新建技能"}
        </button>
        <button type="button" disabled={busy} onClick={() => void reload(tenantId)}>
          刷新
        </button>
      </div>

      {creating && (
        <div className="card nested">
          <div className="row">
            <input
              placeholder="技能名（如 meeting-notes）"
              value={newName}
              onChange={(e) => setNewName(e.target.value.toLowerCase())}
            />
          </div>
          {newName && !nameValid && <p className="field-error">技能名需匹配 ^[a-z0-9]+(-[a-z0-9]+)*$</p>}
          <input
            placeholder="描述（必填，模型据此决定是否使用该技能）"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
          />
          <textarea
            rows={6}
            placeholder={"技能正文（Markdown，指导模型如何执行）\n\n## 步骤\n1. …"}
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
          />
          <div className="row">
            <button
              type="button"
              className="primary"
              disabled={busy || !nameValid || !newDesc.trim()}
              onClick={() =>
                void run(async () => {
                  const bearer = await adminBearer(session);
                  await adminApi.createSkill(
                    tenantId,
                    { name: newName, description: newDesc.trim(), content: newContent },
                    session.adminKey,
                    bearer,
                  );
                  setNewName("");
                  setNewDesc("");
                  setNewContent("");
                  setCreating(false);
                  await reload(tenantId);
                })
              }
            >
              创建
            </button>
          </div>
        </div>
      )}

      {error && <p className="error">管理面请求失败：{error}</p>}
      {rows !== null && rows.length === 0 && <p className="hint">该租户还没有自定义技能。</p>}
      <ul className="tenant-list">
        {(rows ?? []).map((s) => (
          <li key={s.name} className={s.enabled ? "" : "muted"}>
            <div className="tenant-meta">
              <strong>{s.name}</strong>
              <span className="tag">{s.enabled ? "参与对话" : "已停用"}</span>
              <span className="hint">{s.description}</span>
            </div>
            <div className="tenant-actions">
              <button
                type="button"
                className={s.enabled ? "" : "primary"}
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const bearer = await adminBearer(session);
                    await adminApi.updateSkill(tenantId, s.name, { enabled: !s.enabled }, session.adminKey, bearer);
                    await reload(tenantId);
                  })
                }
              >
                {s.enabled ? "停止参与" : "恢复参与"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setEditing(editing === s.name ? null : s.name)}
              >
                {editing === s.name ? "收起" : "编辑"}
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    if (!window.confirm(`确认删除技能 ${s.name}？`)) return;
                    const bearer = await adminBearer(session);
                    await adminApi.deleteSkill(tenantId, s.name, session.adminKey, bearer);
                    await reload(tenantId);
                  })
                }
              >
                删除
              </button>
            </div>
            {editing === s.name && (
              <SkillEditForm
                row={s}
                busy={busy}
                onSave={async (payload) => {
                  const bearer = await adminBearer(session);
                  await adminApi.updateSkill(tenantId, s.name, payload, session.adminKey, bearer);
                  setEditing(null);
                  await reload(tenantId);
                }}
              />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function SkillEditForm({
  row,
  busy,
  onSave,
}: {
  row: SkillRow;
  busy: boolean;
  onSave: (payload: SkillPayload) => Promise<void>;
}) {
  const [description, setDescription] = useState(row.description);
  const [content, setContent] = useState(row.content);

  return (
    <div className="card nested">
      <input
        placeholder="描述（模型据此决定是否使用该技能）"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <textarea
        rows={6}
        placeholder="技能正文（Markdown）"
        value={content}
        onChange={(e) => setContent(e.target.value)}
      />
      <div className="row">
        <button
          type="button"
          className="primary"
          disabled={busy || !description.trim()}
          onClick={() => void onSave({ description: description.trim(), content })}
        >
          保存修改
        </button>
      </div>
    </div>
  );
}

/** 智能体配置页：租户选择 + 灵魂 / 子智能体 / 技能三块（仅管理员身份）。 */
function AgentConfigTab() {
  const session = useSession();
  const activeTenants = (session.tenants ?? []).filter((t) => t.status === "active");
  const [tenantId, setTenantId] = useState<string>("");

  // 默认选中当前租户（或第一个启用的）
  useEffect(() => {
    if (!tenantId && activeTenants.length > 0) {
      setTenantId(
        session.tenantId && activeTenants.some((t) => t.id === session.tenantId)
          ? session.tenantId
          : activeTenants[0].id,
      );
    }
  }, [tenantId, activeTenants, session.tenantId]);

  // 配置端点要求 Bearer 与所选租户匹配：管理员身份切换租户时同步切换会话
  // 身份（租户级 = 管理员）。普通用户不切换 —— 他们本来就不该配置别的租户。
  useEffect(() => {
    if (tenantId && session.isAdminIdentity && session.tenantId !== tenantId) {
      void session.selectIdentity(tenantId, null);
    }
  }, [tenantId, session]);

  if (activeTenants.length === 0) {
    return (
      <section className="card">
        <h2>智能体配置</h2>
        <p className="hint">先在「租户管理」创建（或启用）一个租户，再配置它的智能体。</p>
      </section>
    );
  }

  if (!session.isAdminIdentity) {
    return (
      <section className="card">
        <h2>智能体配置</h2>
        <p className="hint">
          当前身份是普通用户 <code>{session.user}</code>（租户 {session.tenantId}）。
          只有<strong>管理员</strong>可以配置 leadAgent / subAgent / skill；
          普通用户只能运行对话流程 —— 管理面会直接拒绝（403），
          数据面的技能 / 子智能体变更接口同样只认管理员。
        </p>
        <p className="hint">
          如需配置，请在「用户管理」中以租户身份或管理员用户进入对话，再回到本页。
        </p>
      </section>
    );
  }

  return (
    <>
      <section className="card">
        <h2>智能体配置</h2>
        <p className="hint">
          每个租户独立的 leadAgent 灵魂、子智能体与技能；管理面把配置物化为租户目录里的
          deerflow 原生文件，运行时按租户读取，下一轮对话即生效。当前以
          {session.user ? ` 管理员 ${session.user}` : " 租户级身份"}操作。
        </p>
        <div className="row">
          <label className="field-label" htmlFor="agent-tenant-select">
            租户
          </label>
          <select
            id="agent-tenant-select"
            value={tenantId}
            onChange={(e) => setTenantId(e.target.value)}
          >
            {activeTenants.map((t: TenantRow) => (
              <option key={t.id} value={t.id}>
                {t.id}（{t.name}）
              </option>
            ))}
          </select>
        </div>
      </section>
      <SoulCard tenantId={tenantId} />
      <SubagentsCard tenantId={tenantId} />
      <SkillsCard tenantId={tenantId} />
    </>
  );
}

// ── 管理界面外壳 ───────────────────────────────────────────────────────────

export default function AdminView({ onBack }: AdminViewProps) {
  const session = useSession();
  const [tab, setTab] = useState<"tenants" | "users" | "agents">("tenants");
  const [keyDraft, setKeyDraft] = useState(session.adminKey);

  return (
    <div className="admin-page">
      <header className="topbar">
        <div className="topbar-left">
          <span className="brand-mark">DeerFlow 多租户</span>
          <span className="tag">管理界面</span>
        </div>
        <div className="topbar-right">
          <button type="button" className="primary" onClick={onBack}>
            返回对话
          </button>
        </div>
      </header>

      <div className="admin-body">
        <nav className="tabs">
          <button
            type="button"
            className={tab === "tenants" ? "active" : ""}
            onClick={() => setTab("tenants")}
          >
            租户管理
          </button>
          <button
            type="button"
            className={tab === "users" ? "active" : ""}
            onClick={() => setTab("users")}
          >
            用户管理
          </button>
          <button
            type="button"
            className={tab === "agents" ? "active" : ""}
            onClick={() => setTab("agents")}
          >
            智能体配置
          </button>
        </nav>

        <section className="card admin-key-card">
          <div className="row">
            <input
              type="password"
              placeholder="X-Admin-Key（tenant-admin 未配置鉴权时留空）"
              value={keyDraft}
              onChange={(e) => setKeyDraft(e.target.value)}
            />
            <button
              type="button"
              onClick={() => {
                session.setAdminKey(keyDraft);
                void session.refreshTenants();
              }}
            >
              保存并刷新
            </button>
          </div>
          {session.tenantsError && <p className="error">管理面请求失败：{session.tenantsError}</p>}
        </section>

        {tab === "tenants" ? (
          <TenantsTab onEnter={onBack} />
        ) : tab === "users" ? (
          <UsersTab onEnter={onBack} />
        ) : (
          <AgentConfigTab />
        )}
      </div>
    </div>
  );
}
