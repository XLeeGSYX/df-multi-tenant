/** Shared UI types. Wire-level shapes come from @langchain/langgraph-sdk. */

export interface TenantRow {
  id: string;
  name: string;
  status: string;
}

/** GET /admin/tenants/{tid}/users —— 管理面用户注册表 */
export interface UserRow {
  username: string;
  display_name: string;
  /** "admin" | "user" —— 权限轴：仅 admin 可配置智能体 */
  role: string;
  created_at?: string;
}

/** GET /api/v1/auth/me —— P10 补丁后返回合成身份（tenant-{tid}[-{user}]） */
export interface MeInfo {
  id: string;
  email: string;
  system_role?: string;
}

/** GET /api/models —— 数据面可用模型（来自 config.yaml 的 models: 列表） */
export interface ModelInfo {
  name: string;
  model: string;
  display_name: string | null;
  description: string | null;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
}

// ── 智能体配置（管理面 agent-config 端点）──────────────────────────────────

/** GET/PUT /admin/tenants/{tid}/lead-agent —— 租户级 leadAgent 灵魂（SOUL.md） */
export interface LeadAgentInfo {
  soul_md: string | null;
  updated_at: string | null;
}

/** GET /admin/tenants/{tid}/subagents —— DB enabled + 物化 JSON 合并视图 */
export interface SubagentRow {
  name: string;
  display_name: string | null;
  description: string;
  system_prompt: string;
  tools: string[] | null;
  skills: string[] | null;
  model: string;
  max_turns: number;
  timeout_seconds: number;
  enabled: boolean;
}

/** GET /admin/tenants/{tid}/skills —— SKILL.md 解析视图 */
export interface SkillRow {
  name: string;
  description: string;
  content: string;
  enabled: boolean;
}

/** 子智能体创建/更新载荷（tools/skills/model 等走后端默认值） */
export interface SubagentPayload {
  name?: string;
  display_name?: string;
  description?: string;
  system_prompt?: string;
  enabled?: boolean;
}

/** 技能创建/更新载荷 */
export interface SkillPayload {
  name?: string;
  description?: string;
  content?: string;
  enabled?: boolean;
}

/** 与 tenant_db.agent_config 的校验规则一致 */
export const SUBAGENT_NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
export const SKILL_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

export interface UIMessage {
  key: string;
  role: "human" | "assistant" | "tool";
  text: string;
  /** 该条助手消息触发的工具名（展示用） */
  tools: string[];
  streaming?: boolean;
}

/** 与 tenancy.context 的规则保持一致 */
export const TENANT_ID_RE = /^[a-z][a-z0-9-]{1,62}$/;
export const USER_NAME_RE = /^[a-z][a-z0-9-]{0,31}$/;

/** 数据面请求头（Bearer + 一致性租户头） */
export type AuthHeaders = Record<string, string>;

/** POST /admin/tenants/{tid}/token —— 签发结果（role 随 token 返回） */
export interface TokenIssued {
  access_token: string;
  expires_in: number;
  /** "admin"（租户级 token / 管理员用户）| "user" */
  role: string;
}
