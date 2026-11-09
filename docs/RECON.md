# Recon Record — design doc §5/§13 against the pinned deerflow

All findings verified against the pinned ref `f9f3127d`
(see [../upstream/PIN.md](../upstream/PIN.md)). Line numbers below are that
ref. The design doc's symbol names were explicitly placeholders
(§5: "以对安装包源码的 grep 结果为准替换") — this file is the replacement
record.

## §5 patch list → what was actually implemented

| # | Doc's placeholder target | Real target (verified) | Phase | Status |
|---|--------------------------|------------------------|-------|--------|
| P1 | `get_foundation_root` | **no such symbol.** Real chokepoints: `deerflow.config.runtime_paths.runtime_home()` (`runtime_paths.py:19`) and the `Paths.base_dir` property (`paths.py:160`), plus `Paths.host_base_dir` / `Paths._host_base_dir_str` (`paths.py:139`) | import | ✅ `patches/foundation_paths.py` |
| P2 | skill/subagent registry + cache | Resolved **by composition** — see R3 below | — | ✅ no patch needed |
| P3 | `extension_config.yaml` read | `ExtensionsConfig.resolve_config_path()` classmethod (`extensions_config.py:419`) — the file is **JSON** (`extensions_config.json`), not YAML; consumers: `app/gateway/routers/skills.py:722`, `app/gateway/routers/mcp.py:1185/1241/1283/1324` | runtime | ✅ `patches/extensions_config.py` |
| P4 | memory storage | `deerflow.agents.memory.manager.get_memory_manager()` (`agents/memory/manager.py:940`) — singleton that eagerly freezes `storage_path = str(runtime_home())` at `:973` via a call-time function-local import | runtime | ✅ `patches/memory_manager.py` |
| P5 | scheduler create/trigger + concurrency keys | — | runtime | ⛔ deferred (demo limitation) |
| P6 | sandbox mount/startup args | mount source = `DEER_FLOW_HOST_BASE_DIR` → `Paths.host_base_dir`/`_host_base_dir_str` (part of P1); thread data mounts resolve through `get_paths()` | runtime | 🟡 covered by P1's host-dir patches |
| P7 | IM channel callback entry | — | runtime | ⛔ deferred (demo limitation) |
| P8 | file download/static routes | all resolve through `get_paths()` / `runtime_home()` → per-tenant after P1; uploads live under `users/{uid}/threads/...` with per-tenant synthetic uids (P10) | runtime | ✅ covered by composition |
| P9 | log/trace tenant filter | — | runtime | ⛔ deferred (demo limitation) |
| P10 | run/thread persistence | `app.gateway.auth_disabled.get_auth_disabled_user()` (`auth_disabled.py:49`) under `DEER_FLOW_AUTH_DISABLED=1` — consumers `auth_middleware.py:162,165`; owner column `Thread.user_id: String(64)` (`persistence/thread_meta/model.py:19`) | runtime | ✅ `patches/auth_user.py` |
| D2 | `deerflow.gateway.app.create_app` | **`app.gateway.app`** — the gateway is app-layer (`app.*`, unpublished), not part of the deerflow package. `create_app()` at `app/gateway/app.py:698`; **module-level `app = create_app()` at `:1048`**; lifespan at `:310` | import | ✅ `patches/create_app.py` |

## §13 open recon items — answers

### R1 — `create_app` real module path and signature

`app.gateway.app:create_app() -> FastAPI` (app layer). Critically, the module
also constructs the instance at import time (`app = create_app()`, `:1048`), so
patching the factory symbol alone is not enough — any importer that already
bound `app.gateway.app.app` would hold an undecorated instance. D2 therefore
patches **both** the `create_app` symbol (future constructions) and the module
attribute `app` (decorate the existing instance in place — no double gateway
construction, no duplicate middleware).

### R2 — foundation path resolution chokepoint; called at import time?

There is no `get_foundation_root`. The two real chokepoints:

- `runtime_home()` — `DEER_FLOW_HOME` env or `{project_root()}/.deer-flow`,
  where `project_root()` is `DEER_FLOW_PROJECT_ROOT` env or CWD
  (`runtime_paths.py:7,19`).
- `Paths.base_dir` property — re-reads the env **per access**, falling back to
  `_default_local_base_dir()` → `runtime_home()` (`paths.py:160`). The
  `get_paths()` singleton (`:532`) is lazy, and the `Paths` instance it holds
  re-resolves `base_dir` on every access — so patching the property is
  sufficient; no singleton surgery needed.

`runtime_home()` **is** called at import time by eager modules, so P1 patches
run in phase `"import"` (Phase A, before the gateway module is imported by
`create_tenant_app`). The design doc's `.foundation` root is a **new concept**
this repo introduces: `DF_FOUNDATION_ROOT` (default `{cwd}/.foundation`),
per-tenant root `{root}/{tenant_id}`, mapping onto deerflow's
per-`DEER_FLOW_HOME` semantics.

### R3 — skill/subagent registry class and build entry

Recon found **no process-global foundation-scanned registry** of the shape §5.3
assumed (`get_skill_registry` does not exist). Skill/subagent storages are
built per **user id** (user-keyed caches; paths via `Paths.user_dir()` /
`get_paths()`). Combined with P10's per-tenant synthetic user ids
(`tenant-{tid}`), every cache slot and directory is tenant-distinct by
construction. The §5.3 cache-patch pattern is therefore unnecessary — the
composition of P1 (paths) + P10 (identity) covers it. This is the
"组合拳" resolution recorded as a deviation.

### R4 — is memory storage under `.foundation`?

Yes — default storage is `{runtime_home}/users/{uid}/memory.json` (FTS5 index
under `{storage_path}/.retrieval/`), inside the foundation root after P1.
**But** `get_memory_manager()` is a process-global singleton that freezes
`storage_path` at first call, so P4 rebuilds it per tenant. Two traps the P4
patch handles:

1. `original()` returns the module-global `_memory_manager` verbatim when
   non-None — after a tenant build it holds that tenant's manager, so a later
   platform/no-context caller would receive it (platform→tenant leak). The
   wrapper always resets before building; the global becomes a transient
   handoff slot nothing reads twice.
2. No-context callers map to the `_platform` key and build inside an explicit
   platform scope (never the deerflow default root outside `.foundation`).

### R5 — uploads / sandbox working directories

Uploads: `users/{user_id}/threads/{thread_id}/user-data/...` under `base_dir`
→ per-tenant after P1 (+ synthetic uids). Sandbox: container-internal
thread-data mounts resolve through `get_paths()` (covered by P1); the
**host-side** mount source is `DEER_FLOW_HOST_BASE_DIR`, so `host_base_dir`
and `_host_base_dir_str` are patched to resolve `{host_root}/{tenant_id}` when
that env is set — otherwise DooD bind mounts would collide across tenants.

### R6 — direct writes at the `.foundation` root

All are deployment-level shared state, and the wrapped lifespan runs
startup/shutdown inside the `_platform` scope so they land under
`.foundation/_platform`:

- `config.yaml` — read via `existing_project_file()` from the project root
  (env-overridable path; never per-tenant in this MVP).
- sqlite DB — `database.sqlite_dir` from `config.yaml` (default
  `.deer-flow/data`, CWD-relative → **not** under `runtime_home`, which is why
  `make config` rewrites it to `.foundation/_platform/data`).
- `extensions_config.json` — resolved env-first by deerflow; P3 puts the
  tenant file first instead (`{tenant_root}/extensions/extensions_config.json`
  when it exists).

## Deviations from the design doc

1. **Base pin** — doc assumed DeerFlow 2.2; actual pin is `f9f3127d`
   (`v2.1.0-rc0` + 25 commits). `v2.2.0` could not be fetched (network).
   See [../upstream/PIN.md](../upstream/PIN.md).
2. **Dependency form (§9)** — deerflow is not on PyPI and the backend has no
   build-system, so the doc's "方式3 variant" applies: our packages are
   editable-installed into the backend venv; the runtime imports deerflow via
   that venv + `PYTHONPATH`. No site-packages edits, no vendored copies.
3. **Thread/Run/Message tables (§6)** — the doc wanted them redefined with
   `TenantScopedMixin`. Those ORM models are owned by deerflow; adding
   `tenant_id` columns would mean editing upstream source. Demo isolation is
   therefore **owner-axis**: per-tenant synthetic user ids (P10) give every
   deerflow-owned row a tenant-distinct owner, plus per-tenant file roots.
   `tenant-db` defines only management-plane tables (`Tenant`, `Skill`,
   `SubAgent`, `CustomAgent`, `ScheduledTask`, `ChannelConnection`,
   `AuditLog`) — documented in `tenant_db/models.py`.
4. **Shared deployment DB** — one sqlite DB at `sqlite_dir` for all tenants;
   row separation relies on the user-id axis only. Demo-grade: production
   wants per-tenant databases or an upstream tenant-column change.
5. **P2 by composition** — see R3.
6. **P5/P7/P9 deferred** — scheduler tenant-scoping, IM channel callback
   context, and log/trace tenant tags are not in this MVP. The propagation
   helpers (`tenancy.propagation`) exist so P5/P7 have the building blocks.
7. **Auth mode** — the demo runs `DEER_FLOW_AUTH_DISABLED=1` so deerflow's own
   auth never challenges; tenant identity comes entirely from
   `TenantContextMiddleware` (RS256 JWT, JWKS from tenant-admin; HS256 or
   header-trusting `dev` mode for local work). The synthetic user id is
   derived from the verified tenant context, never from client input.
8. **User axis within tenants (addition, not in the doc)** — added 2026-11-08
   at the user's request for the console UI. tenant-admin keeps a per-tenant
   user registry (`tenant_users`) and mints tokens with an optional `user`
   claim; the middleware binds it and P10 derives `tenant-{tid}-{user}` from
   it, so the existing owner axis separates users within a tenant with zero
   schema change. Thread/Run rows remain owner-separated exactly as in
   deviation 3 — only the synthetic id gained a user suffix.
9. **Per-tenant agent config (addition, not in the doc)** — added 2026-11-08
   at the user's request: tenants configure their leadAgent soul, managed
   subagents, and custom skills from the console, with participation toggles.
   Management-plane tables (`lead_agents`; `Skill`/`SubAgent` reused as the
   name → enabled index) are the source of truth; `tenant_db.agent_config`
   materializes them into the tenant root in deerflow-native formats
   (`SOUL.md`, `managed-subagents/{name}.json`, `skills/custom/{name}/SKILL.md`
   + `skills/_skill_states.json`), which the pinned runtime already re-reads
   per agent build. Two upstream behaviors needed patch P11: custom skills are
   user-scoped upstream (paths redirected to the tenant-shared
   `{base_dir}/skills`), and the enabled-skills cache only saw in-process
   mutations (a filesystem-signature check now invalidates it on admin-side
   writes). The admin never imports deerflow — the file shapes are pinned by
   `tests/cross_tenant/test_agent_config_patches.py`, which round-trips them
   through the real deerflow models.
10. **Role axis / permission isolation (addition, not in the doc)** — added
   2026-11-09 at the user's request: only admin identities may configure
   leadAgent/subAgent/skill; regular users only run flows. `tenant_users`
   gains a `role` column (`admin` | `user`, default `user`; existing sqlite
   DBs are upgraded at admin startup by `tenant_db.schema_upgrade`). User
   tokens carry a `role` claim, the middleware binds it as `_CURRENT_ROLE`,
   and the single policy is `tenancy.context.is_admin_role` — tenant-level
   tokens (no role claim) stay admin-capable. Enforcement is two-plane: the
   management plane gates all 10 agent-config endpoints on operator key OR
   admin-capable Bearer (`apps/tenant_admin/main.py`:
   `_require_agent_config_admin`, RS256-verified locally); the data plane
   extends P10 so the synthetic user's `system_role` is demoted to `user`
   for regular users, making deerflow's own `require_admin_user` 403 every
   admin-gated runtime route (skill/subagent/MCP/channel/integration
   mutations) with zero route-by-route patching. Auth matrix pinned in
   `tests/cross_tenant/test_role_isolation.py`.

## Verified upstream behaviors (no patch needed)

- **Interrupted-run recovery** — verified 2026-11-09 with `kill -9` mid-run +
  restart against the pinned runtime, answering the design question "支持任务
  中断恢复吗": yes, upstream already handles it. Gateway startup calls
  `RunManager.reconcile_orphaned_inflight_runs` (registered in
  `app/gateway/deps.py` lifespan; implemented in
  `packages/harness/deerflow/runtime/runs/manager.py`), which in
  single-worker/sqlite mode reclaims every NULL-lease inflight row — the
  zombie run ends `error` with stop reason `orphan_recovered`, the thread
  `error`, which also releases the `uq_runs_thread_active` one-active-run
  constraint so the thread accepts new runs. LangGraph checkpoints persist
  the human message per super-step, so the next message on the same thread
  continues with full context (verified: the agent summarized the
  interrupted essay plan). The in-flight step itself is not resumed — the
  run terminates as `error` and the request is re-issued. Documented in the
  README's 中断恢复 section; nothing was patched.

## §12 trap checklist (how each is handled)

| Trap | Handling |
|------|----------|
| #1 BaseHTTPMiddleware loses ContextVar | pure-ASGI `TenantContextMiddleware` (`tenancy/middleware.py`) |
| #2 import-time path resolution | Phase A `"import"` patches applied before gateway import (`bootstrap.create_tenant_app`) |
| #3 module-level singleton freeze (memory) | P4 per-tenant manager cache with always-reset discipline |
| #4 user-keyed caches crossing tenants | resolved by composition — synthetic per-tenant user ids (R3) |
| #5 public paths must not call `get_tenant_id()` | public whitelist + OPTIONS bypass documented in the middleware |
| #6 executor threads don't inherit ContextVar | `tenant_wrapped` / `spawn_in_tenant` / `call_in_tenant` |
| #7 leaked context polluting reused workers | every helper/middleware sets and resets in try/finally |
