"""P10 (adapted): per-tenant synthetic user under deerflow's auth-disabled mode.

Target version: deer-flow @ f9f3127d (see upstream/PIN.md).
Original signature (app/gateway/auth_disabled.py:58):

    def get_auth_disabled_user(): ...

Recon result: deerflow's own persistence tables (threads_meta, runs, ...)
have no ``tenant_id`` column and must stay untouched. In auth-disabled mode
(``DEER_FLOW_AUTH_DISABLED=1``) every request runs as one synthetic admin
user; this patch makes that identity tenant-scoped, so the deployment-level
database separates rows on the OWNER axis (``user_id = tenant-<tid>``)
without any schema change. File state is already separated by the P1 path
patch (both layers double-scope: ``.foundation/{tid}/users/tenant-{tid}/...``).

User axis: when the tenant JWT carries a ``user`` claim (bound by
TenantContextMiddleware from the management-plane user registry), the
synthetic id becomes ``tenant-<tid>-<user>`` — same tenant, different owner,
so per-user threads/lists separate on the same owner axis with no further
schema change. Tokens without the claim keep the tenant-level identity.

Role axis (permission isolation): a ``role`` claim ("admin" | "user") rides
the same token. Upstream's auth-disabled mode gives EVERY caller
``system_role == "admin"``, and the gateway's ``require_admin_user`` gate
(skills/subagents/MCP/channel mutations) trusts exactly that attribute — so
without this override a regular user could mutate the tenant-shared config.
The wrapper demotes ``system_role`` to "user" for non-admin roles; tenant-
level and admin-role identities keep the base "admin". The policy lives in
``tenancy.context.is_admin_role`` so both planes share one definition.

Consumers (grep-verified): app/gateway/auth_middleware.py:162,165 and
app/gateway/routers/browser.py:138 call the function per request (rebound by
the registry's ``from x import y`` scan). The ``AUTH_DISABLED_USER_ID``
constant bindings (langgraph_auth.py, channels/manager.py) belong to the
LangGraph-server compat and IM paths, which run deployment-level under the
platform scope — patch P7 territory, documented in RECON.md.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from deerflow_patches.registry import patch
from tenancy.context import PLATFORM_TENANT_ID, get_current_role, get_current_user_optional, get_tenant_id_optional, is_admin_role

# deerflow's threads_meta.user_id is String(64); keep synthetic ids within it.
_MAX_USER_ID_LEN = 64


def tenant_user_id(tenant_id: str, user: str | None = None) -> str:
    """Deterministic, charset-safe ([A-Za-z0-9_-]) synthetic user id.

    ``tenant-<tid>`` for tenant-level requests, ``tenant-<tid>-<user>`` for
    user-scoped ones; over-long ids fall back to a digest (still unique per
    (tenant, user) pair).
    """
    direct = f"tenant-{tenant_id}-{user}" if user else f"tenant-{tenant_id}"
    if len(direct) <= _MAX_USER_ID_LEN:
        return direct
    digest = hashlib.sha256(f"{tenant_id}:{user or ''}".encode("utf-8")).hexdigest()[:48]
    return f"t-{digest}"


@patch("app.gateway.auth_disabled", "get_auth_disabled_user", phase="runtime")
def patched_get_auth_disabled_user(original):
    def wrapper():
        tid = get_tenant_id_optional()
        if tid is None or tid == PLATFORM_TENANT_ID:
            return original()
        user = get_current_user_optional()
        base = original()
        attrs = dict(vars(base))  # already contains the base 'id'/'email'
        attrs["id"] = tenant_user_id(tid, user)
        attrs["email"] = f"{user}@{tid}.tenant.local" if user else f"admin@{tid}.tenant.local"
        if not is_admin_role(get_current_role()):
            # Regular users lose deerflow's blanket auth-disabled admin role;
            # every require_admin_user gate in the gateway then 403s them.
            attrs["system_role"] = "user"
        return SimpleNamespace(**attrs)

    return wrapper
