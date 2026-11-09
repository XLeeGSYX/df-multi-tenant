/**
 * 会话层：当前租户/用户身份 + 由 tenant-admin 签发的 token。
 *
 * 设计（本地演示控制台）：
 * - 进入即用：无登录页。挂载时拉租户列表，自动选中已保存（或第一个）
 *   启用中的租户并向管理面申请 token —— admin/runtime 都在时用户直接可聊。
 * - 切换身份 = 重新签发：selectIdentity(tenant, user|null)。
 * - token 只存内存（刷新即重签，永不过期残留）；localStorage 仅保存
 *   adminKey + 上次选择的租户/用户。adminKey 属演示便利，生产部署
 *   应由管理面 SSO 下发 token，而不是让页面持 key 自签。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { adminApi, authHeaders } from "./api";
import type { AuthHeaders, TenantRow } from "./types";

const STORAGE_KEY = "dfmt.session.v1";

interface Persisted {
  adminKey: string;
  tenantId: string | null;
  user: string | null;
}

function loadPersisted(): Persisted {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { adminKey: "", tenantId: null, user: null };
    const parsed = JSON.parse(raw) as Partial<Persisted>;
    return {
      adminKey: typeof parsed.adminKey === "string" ? parsed.adminKey : "",
      tenantId: typeof parsed.tenantId === "string" ? parsed.tenantId : null,
      user: typeof parsed.user === "string" ? parsed.user : null,
    };
  } catch {
    return { adminKey: "", tenantId: null, user: null };
  }
}

function persist(value: Persisted): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    /* storage unavailable — session just won't survive refresh */
  }
}

interface SessionValue {
  /** 管理面 X-Admin-Key（未配置鉴权时可留空） */
  adminKey: string;
  setAdminKey: (key: string) => void;
  /** null = 尚未加载完成 */
  tenants: TenantRow[] | null;
  tenantsError: string | null;
  refreshTenants: () => Promise<void>;
  /** 当前租户；null = 未选择（无租户或签发失败） */
  tenantId: string | null;
  /** 当前用户（null = 租户级身份） */
  user: string | null;
  /** 当前身份角色："admin"（租户级/管理员）| "user"；无 token 时 null */
  role: string | null;
  /** 管理员能力：租户级身份或 admin 角色用户（权限隔离的 UI 侧开关） */
  isAdminIdentity: boolean;
  token: string | null;
  tokenExp: number | null;
  mintError: string | null;
  /** 切换身份（重新签发 token）。user=null 为租户级。 */
  selectIdentity: (tenantId: string, user: string | null) => Promise<void>;
  /** 返回可用 token；临期（<60s）自动重签。无身份时返回 null。 */
  ensureToken: () => Promise<string | null>;
  headers: () => AuthHeaders | null;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [adminKey, setAdminKeyState] = useState("");
  const [tenants, setTenants] = useState<TenantRow[] | null>(null);
  const [tenantsError, setTenantsError] = useState<string | null>(null);
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [user, setUser] = useState<string | null>(null);
  const [role, setRole] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [tokenExp, setTokenExp] = useState<number | null>(null);
  const [mintError, setMintError] = useState<string | null>(null);

  const mint = useCallback(async (tid: string, usr: string | null, key: string) => {
    setMintError(null);
    try {
      const issued = await adminApi.issueToken(tid, key, usr);
      setTenantId(tid);
      setUser(usr);
      setRole(issued.role ?? (usr ? "user" : "admin"));
      setToken(issued.access_token);
      setTokenExp(Math.floor(Date.now() / 1000) + issued.expires_in);
      persist({ adminKey: key, tenantId: tid, user: usr });
      return issued.access_token;
    } catch (err) {
      setRole(null);
      setToken(null);
      setTokenExp(null);
      setMintError(err instanceof Error ? err.message : String(err));
      return null;
    }
  }, []);

  const refreshTenants = useCallback(async () => {
    try {
      const { tenants: rows } = await adminApi.listTenants(adminKey);
      setTenants(rows);
      setTenantsError(null);
    } catch (err) {
      setTenants(null);
      setTenantsError(err instanceof Error ? err.message : String(err));
    }
  }, [adminKey]);

  const selectIdentity = useCallback(
    async (tid: string, usr: string | null) => {
      await mint(tid, usr, adminKey);
    },
    [adminKey, mint],
  );

  const ensureToken = useCallback(async () => {
    if (!tenantId) return null;
    const now = Math.floor(Date.now() / 1000);
    if (token && tokenExp && tokenExp - now > 60) return token;
    return mint(tenantId, user, adminKey);
  }, [tenantId, token, tokenExp, user, adminKey, mint]);

  const setAdminKey = useCallback(
    (key: string) => {
      setAdminKeyState(key);
      persist({ adminKey: key, tenantId, user });
    },
    [tenantId, user],
  );

  // 挂载：恢复上次选择（或第一个启用租户），自动签发。
  useEffect(() => {
    const stored = loadPersisted();
    setAdminKeyState(stored.adminKey);
    void (async () => {
      let rows: TenantRow[] | null = null;
      try {
        rows = (await adminApi.listTenants(stored.adminKey)).tenants;
        setTenants(rows);
      } catch (err) {
        setTenantsError(err instanceof Error ? err.message : String(err));
        return;
      }
      const active = rows.filter((t) => t.status === "active");
      if (stored.tenantId && active.some((t) => t.id === stored.tenantId)) {
        await mint(stored.tenantId, stored.user, stored.adminKey);
      } else if (active.length > 0) {
        await mint(active[0].id, null, stored.adminKey);
      }
    })();
  }, [mint]);

  const headers = useCallback(
    (): AuthHeaders | null => (token && tenantId ? authHeaders(token, tenantId) : null),
    [token, tenantId],
  );

  // 与后端 tenancy.context.is_admin_role 同一条策略：租户级（user=null）
  // 或 admin 角色 → 可配置智能体；普通用户只能跑流程。
  const isAdminIdentity = user === null || role === "admin";

  const value = useMemo(
    () => ({
      adminKey,
      setAdminKey,
      tenants,
      tenantsError,
      refreshTenants,
      tenantId,
      user,
      role,
      isAdminIdentity,
      token,
      tokenExp,
      mintError,
      selectIdentity,
      ensureToken,
      headers,
    }),
    [
      adminKey,
      setAdminKey,
      tenants,
      tenantsError,
      refreshTenants,
      tenantId,
      user,
      role,
      isAdminIdentity,
      token,
      tokenExp,
      mintError,
      selectIdentity,
      ensureToken,
      headers,
    ],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession 必须在 <SessionProvider> 内使用");
  return ctx;
}
