"""Pure-ASGI tenant context middleware (design doc §2.2).

MUST remain pure ASGI: BaseHTTPMiddleware runs the downstream app in a child
task, and a ContextVar set there does not propagate correctly — the tenant
would silently vanish (design doc §12, trap #1).

Mount order: added last via ``app.add_middleware`` → outermost. CORS and
deerflow's own auth/CSRF middlewares sit inside and therefore observe the
tenant context. Preflight (OPTIONS) requests and the public path whitelist
pass through without a tenant context — nothing on those paths may call
``get_tenant_id()`` (design doc §12, trap #5).

Dev mode (``TENANT_AUTH_MODE=dev``): trust ``x-tenant-id`` alone, for local
development only. A startup warning is emitted by the patch layer when this
mode is active (never enable in shared deployments).

An optional ``user`` claim (JWT) / ``x-user-id`` header (dev mode) names a user
WITHIN the tenant. It is bound alongside the tenant so the data plane's
synthetic identity (and with it the owner axis) becomes per-user. A ``role``
claim ("admin" | "user") rides the same token and is bound the same way;
tenant-level tokens carry no role and remain admin-capable (see
``tenancy.context.is_admin_role``).
"""

from __future__ import annotations

import json
import os

from tenancy.context import (
    is_valid_tenant_id,
    is_valid_user_name,
    reset_current_role,
    reset_current_user,
    reset_tenant_id,
    set_current_role,
    set_current_user,
    set_tenant_id,
)
from tenancy.security import extract_role_claim, extract_tenant_claim, extract_user_claim, verify_jwt

# Whitelisted request paths that never resolve a tenant. Mirrors the deerflow
# gateway's own public-path set (health/docs/webhooks) — the design doc's
# ``/healthz``-style list was a placeholder; these are the real ones.
# Override with ``TENANT_PUBLIC_PATHS`` (comma-separated prefixes).
_DEFAULT_PUBLIC_PATH_PREFIXES = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    # Inbound webhooks authenticate via provider signatures (HMAC), not the
    # tenant JWT. Cross-tenant webhook handling is patch P7 territory.
    "/api/webhooks/",
)
_PUBLIC_EXACT_PATHS = frozenset(
    {
        "/api/v1/auth/setup-status",
        "/api/v1/auth/initialize",
        "/api/v1/auth/providers",
    }
)

AUTH_MODE_ENV = "TENANT_AUTH_MODE"
DEV_MODE = "dev"


def _public_path_prefixes() -> tuple[str, ...]:
    override = os.environ.get("TENANT_PUBLIC_PATHS")
    if override is not None:
        return tuple(p for p in (s.strip() for s in override.split(",")) if p)
    return _DEFAULT_PUBLIC_PATH_PREFIXES


def _is_public(path: str) -> bool:
    stripped = path.rstrip("/")
    if stripped in _PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _public_path_prefixes())


class TenantContextMiddleware:
    """Resolve and bind the tenant ContextVar for every request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_public(path) or (scope["type"] == "http" and scope.get("method") == "OPTIONS"):
            # CORS preflight and public paths: no tenant resolution, no
            # tenant context. Downstream handlers on these paths must never
            # call get_tenant_id().
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        header_tenant = headers.get("x-tenant-id")

        if os.environ.get(AUTH_MODE_ENV, "jwt") == DEV_MODE:
            tenant = header_tenant
            if not is_valid_tenant_id(tenant):
                await _reject(send, 400, {"error": "invalid tenant id"})
                return
            user = headers.get("x-user-id")
            # Dev mode has no registry behind it, so there is no role to
            # trust: requests stay tenant-level (admin-capable).
            role = None
        else:
            claims = verify_jwt(headers.get("authorization"))
            if claims is None:
                await _reject(send, 401, {"error": "unauthorized"})
                return
            jwt_tenant = extract_tenant_claim(claims)
            # D5: the JWT claim is the source of truth; the header may only
            # agree with it.
            if header_tenant and header_tenant != jwt_tenant:
                await _reject(send, 403, {"error": "tenant mismatch"})
                return
            tenant = jwt_tenant
            if not is_valid_tenant_id(tenant):
                await _reject(send, 400, {"error": "invalid tenant id"})
                return
            # Optional user-within-tenant; the signed claim is authoritative.
            user = extract_user_claim(claims)
            # Optional role for that user ("admin" | "user"); tenant-level
            # tokens carry none and stay admin-capable (context.is_admin_role).
            role = extract_role_claim(claims)

        if user is not None and not is_valid_user_name(user):
            await _reject(send, 400, {"error": "invalid user"})
            return

        token = set_tenant_id(tenant)
        user_token = set_current_user(user)
        role_token = set_current_role(role)
        # Starlette's HTTPConnection.state lazily reads scope["state"]; the
        # ASGI "state" extension makes the tenant visible to downstream
        # handlers as request.state.tenant_id.
        scope.setdefault("state", {})["tenant_id"] = tenant
        if user is not None:
            scope["state"]["user"] = user
        if role is not None:
            scope["state"]["role"] = role
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_role(role_token)
            reset_current_user(user_token)
            reset_tenant_id(token)


async def _reject(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
