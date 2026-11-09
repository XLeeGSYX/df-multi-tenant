# df-multitenant

Multi-tenant layer for [DeerFlow](../deer-flow-demo), implemented per the
design doc *DeerFlow 2.2 多租户改造 · 落地方案设计* as an **empty project**:
DeerFlow itself is never edited — it is consumed from a read-only pinned
checkout, and every tenancy behavior is injected at runtime through
monkey-patches and middleware.

The recon record (placeholder symbols from the doc → real verified targets,
deviations, trap checklist) lives in
[docs/RECON.md](docs/RECON.md); the locked upstream ref lives in
[upstream/PIN.md](upstream/PIN.md).

## Architecture

```
                    ┌────────────────────────────────────────────┐
                    │ tenant-admin :8010        (management plane)│
                    │  tenant/user CRUD · JWT (RS256) · JWKS      │
                    │  never imports deerflow                     │
                    └───────────────┬────────────────────────────┘
                        issues JWTs │ serves JWKS
                    ┌───────────────▼────────────────────────────┐
   user ── Bearer ──▶ agent-runtime :8001        (data plane)     │
                    │  DeerFlow gateway (pinned checkout, readonly)│
                    │  + deerflow-patches (Phase A/B monkey-patch) │
                    │  + TenantContextMiddleware (pure ASGI)       │
                    └───────────────┬────────────────────────────┘
                                    │ per-request tenant ContextVar
                    .foundation/{tenant_id}/{skills,subagents,extensions,prompts}
                    .foundation/_platform/            (deployment state, DB)
```

Three isolation axes (design doc §1):

1. **Files** — every runtime path resolves to `.foundation/{tenant_id}/…`
   through deerflow's own chokepoints (`runtime_home()`, `Paths.base_dir`).
2. **Context** — a pure-ASGI middleware verifies the tenant JWT and binds a
   `ContextVar` that propagates through the whole request; background work
   uses `tenancy.propagation` helpers.
3. **Database** — deerflow-owned rows are separated by owner axis
   (per-tenant synthetic users `tenant-{tid}`, and per-user within a tenant
   `tenant-{tid}-{user}` via the optional JWT `user` claim); management-plane
   tables are tenant-scoped with ORM-level enforcement (`tenant_db.tenancy_events`).
4. **Permissions** — a per-user `role` claim (`admin` / `user`) rides the JWT;
   regular users are blocked from agent configuration on both planes while
   chat/runs stay open (see [权限隔离](#permission-isolation-权限隔离) below).

## Repository layout

```
df-multitenant/
├── packages/
│   ├── tenancy-core/       # ContextVar context, JWT, ASGI middleware, propagation
│   ├── tenant-db/          # tenant-scoped ORM, events, directory provisioning
│   └── deerflow-patches/   # patch registry + all deerflow patches (data plane only)
├── apps/
│   ├── tenant_admin/       # management-plane FastAPI service
│   └── agent_runtime/      # data-plane entrypoint (create_tenant_app)
├── frontend/               # React 19 + Vite chat console (see below)
│   └── scripts/smoke.mjs   # end-to-end frontend smoke through the dev proxy
├── tests/
│   ├── patch_integrity/    # every patch target must exist in the pinned deerflow
│   └── cross_tenant/       # context/middleware/DB/path isolation
├── scripts/smoke.sh        # live auth + isolation smoke (needs running services)
├── upstream/PIN.md         # locked deerflow ref + re-pin procedure
└── docs/RECON.md           # source-recon record + deviations from the design doc
```

## Quickstart

```bash
make install     # uv sync + editable-install our packages into the backend venv
make config      # copy config.yaml from the checkout; DB -> .foundation/_platform/data

make admin       # terminal 1 — tenant-admin on :8010
make runtime     # terminal 2 — agent-runtime on :8001 (needs admin's JWKS)

# provision a tenant and get a token
curl -X POST localhost:8010/admin/tenants \
  -H 'content-type: application/json' \
  -d '{"id": "acme", "name": "ACME Corp"}'
curl -X POST localhost:8010/admin/tenants/acme/token \
  -H 'content-type: application/json' -d '{}'

# call the agent runtime as that tenant
curl localhost:8001/api/skills -H "authorization: Bearer $TOKEN"

make smoke       # runs the whole flow above end-to-end with assertions
```

## Frontend console

A React console (`frontend/`, Vite + React 19 + TypeScript, no CSS framework)
with two views and **no login page** — entering the app lands directly in the
chat, and a tenant is auto-selected (last choice, else the first active one):

- **对话（chat）** — center conversation pane, left sidebar with per-identity
  thread history, top-right corner **tenant switcher** (switching = re-minting
  the tenant token via tenant-admin) plus a button into the admin view. The
  identity chip shows the runtime synthetic identity (`tenant-{tid}` or
  `tenant-{tid}-{user}` from `/api/v1/auth/me`) as an in-UI isolation proof.
  The chat header carries a **model selector** (see
  [模型切换](#model-switching-对话切换模型)), and a **交付物 bar** below the
  message list surfaces the run's deliverables with 预览/下载 (see
  [交付物](#交付物-deliverables)).
- **管理（admin）** — three tabs over tenant-admin:
  - *租户管理*: create (provisions `.foundation/{tenant}/`) / disable / delete
    tenants; 「进入对话」 switches the console to that tenant.
  - *用户管理*: per-tenant user registry (create/delete users, each with a
    role — 普通用户 / 管理员 — set at creation and toggleable via
    设为管理员 / 设为普通用户); 「进入对话」 mints a token carrying the
    `user` claim, so the data-plane owner axis becomes
    `tenant-{tid}-{user}` — threads are isolated per user, per tenant,
    and from the tenant-level identity.
  - *智能体配置* (admin identities only — regular users see a hint card
    instead): per-tenant leadAgent / subAgent / skill configuration —
    the soul editor, subagent and skill lists with create/edit/delete plus a
    **participation toggle**（参与对话）on each. The management plane stores
    the config in its own tables and materializes deerflow-native files into
    the tenant root (`SOUL.md`, `managed-subagents/{name}.json`,
    `skills/custom/{name}/SKILL.md` + `_skill_states.json`); the runtime
    re-reads them per agent build, so **changes apply to the next message
    without a restart**. Skills are tenant-shared (all users of the tenant);
    disabling a skill keeps it installed but marks it unusable, disabling a
    subagent removes it from runtime discovery.

The Vite dev server proxies `/api` → agent-runtime and `/admin` + `/healthz` →
tenant-admin, so the browser stays same-origin (no CORS, token never leaves the
page origin). `DF_RUNTIME_PORT` / `DF_ADMIN_PORT` override the proxy targets.

```bash
make frontend-install   # pnpm install (once)
make admin              # terminal 1
make runtime            # terminal 2
make frontend           # terminal 3 — http://localhost:5173
make stop               # stop all three by port

# with all three running:
make frontend-smoke     # tenants -> users -> user tokens -> real streamed run
                        # -> model list + explicit model run + unknown-model 400
                        # -> deliverable (write -> present_files -> state -> authed download)
                        # -> per-user/per-tenant isolation checks -> cleanup
```

Demo-console trust note: the browser itself calls tenant-admin to mint tokens
(local demo ergonomics). Real deployments must front the management plane with
operator SSO and hand tokens out server-side instead.

Wire-format notes (verified against the live runtime): streaming chunks arrive
with `type:"AIMessageChunk"` (full messages in thread state use `"ai"`), GLM
reasoning deltas ride `additional_kwargs.reasoning_content` (empty `content`),
and thread state contains a leading `system` message the UI does not render.
The SDK client requires an **absolute** `apiUrl` — it runs
`new URL(apiUrl + path)` with no base, so a relative `"/api"` throws
`TypeError: Invalid URL` before any request is sent (the console therefore
builds it from `window.location.origin`).

`DEER_FLOW_ROOT` (default `../deer-flow-demo`) must point at the pinned
checkout. The runtime intentionally runs with `DEER_FLOW_AUTH_DISABLED=1`:
deerflow's own auth never challenges, and tenant identity is established
entirely by `TenantContextMiddleware`.

## Permission isolation (权限隔离)

Not everyone may configure agents — only admin identities; regular users can
only run flows. The single policy is `tenancy.context.is_admin_role`:

- **tenant-level tokens** (no `user` claim) are admin-capable — the tenant
  master identity;
- user tokens carry a `role` claim minted from the registry
  (`admin` | `user`, default `user`); only `admin` is admin-capable. An
  unknown role value degrades to tenant-level (admin) semantics rather than
  granting anything new.

Enforced on both planes (matrix live-verified against a running stack):

| Plane | Gate | Regular user gets |
|---|---|---|
| tenant-admin `:8010` — all 10 agent-config routes (lead-agent / subagents / skills) | operator key **or** admin-capable Bearer (`401` with neither) | `403 admin role required to configure agents` |
| agent-runtime `:8001` — deerflow's own admin-gated routes (skill/subagent/MCP/channel/integration mutations) | P10 demotes the synthetic user's `system_role` to `user`, so upstream `require_admin_user` rejects | `403` (upstream's own message) |
| agent-runtime `:8001` — threads, runs, chat | no admin requirement | **works** — 普通用户只能够跑流程 |

Roles are managed from the console's 用户管理 tab (`PUT
/admin/tenants/{tid}/users/{username}` behind it); a promotion takes effect
on the next minted token. Existing sqlite databases gain the `role` column
via `tenant_db.schema_upgrade.ensure_schema` — an idempotent startup ALTER
(verified live against the demo DB). The full auth matrix — 401/403/200 per
credential, cross-tenant tokens, forged and alg-confusion tokens, the
mini-migration — is pinned in `tests/cross_tenant/test_role_isolation.py`.

## Interrupted-run recovery (中断恢复)

Supported by the pinned upstream out of the box — verified empirically with
`kill -9` mid-run followed by a runtime restart:

1. A hard kill leaves the run and thread rows `running` (zombies).
2. At startup the gateway sweeps orphaned inflight runs
   (`RunManager.reconcile_orphaned_inflight_runs`; in single-worker/sqlite
   mode every NULL-lease inflight row is reclaimed) → the run ends as
   `error` with stop reason `orphan_recovered`, the thread as `error`.
3. LangGraph checkpoints persist the human message per super-step, so a new
   message on the same thread continues the conversation with full context —
   the agent can even summarize what it was doing when killed.
4. Reclaiming the zombie also releases the one-active-run-per-thread
   constraint, so the thread accepts new runs instead of 409ing.

Scope: the in-flight step itself is **not** resumed — the interrupted run
terminates as `error` and the request is re-issued. That is the upstream
semantic; no patch was needed, so none was added.

## Model switching (对话切换模型)

Chat users can switch models per message from the console. Two halves:

1. **Register the model** — models live in the `models:` list of this repo's
   `config.yaml` (gitignored; the file generated by `make config` puts the
   list at line 23). Append an entry (copy the closest example from the
   checkout's `config.example.yaml`) and restart the runtime —
   `GET /api/models` then serves exactly that list. Currently configured:
   `glm-5-3-flash` (default = first entry), `glm-5.3-flashx` (same Z.AI key,
   added so switching has two working models out of the box), and
   `deepseek-v4-flash` (DeepSeek official API, `api.deepseek.com`).
2. **Select it in the UI** — the chat header has a model selector fed by
   `GET /api/models`; the choice rides each run as `context.model_name`
   (validated against the config allowlist — unknown names get `400` before
   any run is created), takes effect on the very next message, and is
   remembered in `localStorage`.

The `deepseek-v4-flash` entry ships with a placeholder `api_key` — probing
showed the configured Z.AI key serves no DeepSeek model on any of its
endpoints (api.z.ai and open.bigmodel.cn, both OpenAI-compatible and
Anthropic-protocol), so paste a DeepSeek platform key into
`config.yaml` to activate it, or repoint `api_base` at the Volcengine Coding
Plan gateway (`https://ark.cn.beijing.volces.com/api/coding/v3`) if that is
where your key works (see the checkout's `config.example.yaml` DeepSeek
example). Until then, selecting it yields a readable in-chat provider error
and nothing else is affected.

## 交付物 (deliverables)

When the lead agent finishes a task it writes files into the sandbox's
`/mnt/user-data/outputs/` and calls the `present_files` tool; the thread state
then carries them in `values.artifacts` (virtual paths, order-preserving,
deduplicated by upstream's `merge_artifacts` reducer). The console renders a
交付物 bar between the message list and the composer — one row per artifact
with 预览 / 下载:

- A plain link cannot fetch these files: the Gateway requires the tenant Bearer
  header, and `<a href>` carries no `Authorization`. The console therefore
  fetches `/api/threads/{tid}/artifacts{path}?download=true` with the tenant
  JWT and saves the response blob through an object URL.
- Paths are percent-encoded per segment (Chinese filenames included), matching
  upstream's `encodeArtifactPath`.
- 预览 opens non-active content in a new tab (images inline, text files as
  plain text). Active content (`.html` / `.svg` / `.xml` …) is download-only:
  blob URLs share the page origin, and rendering agent-generated HTML there
  would execute scripts next to the session token.
- Artifact GETs are owner-checked (`threads:read` + owner), so a user only
  ever reaches their own threads' outputs; on disk they live under
  `.foundation/{tenant}/users/{owner}/threads/{tid}/user-data/outputs/`.

## Testing

Two environments, one suite (`tests/`):

```bash
make test          # root workspace env — deerflow-dependent tests self-skip
make test-patches  # backend venv — full suite incl. patch integrity
```

`tests/patch_integrity/` walks every registered `PatchSpec` against the
installed deerflow and fails loudly on drift — the guard required by the
design doc's "no guessing patches" rule.

## Security notes

- JWT claim `tenant` is the source of truth; `x-tenant-id` may only agree
  with it (mismatch → 403). Forged headers cannot change identity.
- The `role` claim is trusted only from a signature-verified JWT: tenant-admin
  verifies RS256 locally against the persisted public key, so self-asserted or
  alg-confusion role claims fail with `401` (pinned by tests).
- `TENANT_AUTH_MODE=dev` trusts `x-tenant-id` alone — local development only.
- tenant-admin binds loopback and takes an optional `TENANT_ADMIN_API_KEY`
  (`X-Admin-Key` header). Front it with operator SSO in real deployments.
- Tenant ids are validated (`^[a-z][a-z0-9-]{1,62}$`, reserved names
  rejected) and provisioning double-checks `resolve()` + `is_relative_to()`
  against the foundation root (path-traversal defense in depth).
- `.secrets/` (JWT signing key), `.foundation/` (tenant data), and
  `config.yaml` (model keys) are gitignored — never commit them.
