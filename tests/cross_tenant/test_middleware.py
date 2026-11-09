"""TenantContextMiddleware behavior at the pure-ASGI level (design doc §2.2, §10).

Uses a minimal inner ASGI app so no deerflow import is needed. JWT mode is
exercised with a shared secret (HS256) — the RS256 path differs only in key
resolution, which tenancy.security tests cover separately.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from tenancy.context import get_current_role, get_current_user_optional, get_tenant_id_optional, is_admin_role
from tenancy.middleware import TenantContextMiddleware
from tenancy.security import extract_role_claim, issue_jwt, verify_jwt


class _CaptureApp:
    """Innermost ASGI app capturing the tenant/user/role context it observed."""

    def __init__(self):
        self.observed: list[str | None] = []
        self.observed_users: list[str | None] = []
        self.observed_roles: list[str | None] = []
        self.scope_state: dict = {}

    async def __call__(self, scope, receive, send):
        self.observed.append(get_tenant_id_optional())
        self.observed_users.append(get_current_user_optional())
        self.observed_roles.append(get_current_role())
        self.scope_state = dict(scope.get("state", {}))
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


def _request(
    middleware,
    path="/api/threads",
    *,
    method="GET",
    headers: dict[str, str] | None = None,
):
    encoded = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "headers": encoded, "query_string": b""}

    captured: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
        elif message["type"] == "http.response.body":
            captured["body"] = message.get("body", b"")

    async def run():
        await middleware(scope, receive, send)

    asyncio.run(run())
    return captured


@pytest.fixture()
def hs256_env(monkeypatch):
    monkeypatch.setenv("TENANT_AUTH_MODE", "jwt")
    monkeypatch.setenv("TENANT_JWT_ALG", "hs256")
    monkeypatch.setenv("TENANT_JWT_SECRET", "0123456789abcdef0123456789abcdef")


_TEST_SECRET = "0123456789abcdef0123456789abcdef"  # 32 bytes: RFC 7518 minimum for HS256


def _auth(tenant: str) -> str:
    return f"Bearer {issue_jwt(subject='u1', tenant_id=tenant, secret=_TEST_SECRET)}"


def test_jwt_mode_rejects_missing_token(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app))
    assert result["status"] == 401
    assert app.observed == []


def test_jwt_mode_binds_tenant_and_state(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": _auth("tenant-a"), "x-tenant-id": "tenant-a"})
    assert result["status"] == 200
    assert app.observed == ["tenant-a"]
    assert app.scope_state.get("tenant_id") == "tenant-a"


def test_header_token_mismatch_rejected(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": _auth("tenant-a"), "x-tenant-id": "tenant-b"})
    assert result["status"] == 403
    assert app.observed == []


def test_invalid_tenant_claim_rejected(hs256_env):
    app = _CaptureApp()
    # Reserved name smuggled into a signed token still fails validation.
    token = issue_jwt(subject="u1", tenant_id="skills", secret="0123456789abcdef0123456789abcdef")
    result = _request(TenantContextMiddleware(app), headers={"Authorization": f"Bearer {token}"})
    assert result["status"] == 400
    assert app.observed == []


def test_garbage_token_rejected(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": "Bearer not-a-jwt"})
    assert result["status"] == 401


def test_public_paths_bypass(hs256_env):
    app = _CaptureApp()
    for path in ("/health", "/health/ready", "/docs", "/openapi.json", "/api/webhooks/github"):
        result = _request(TenantContextMiddleware(app), path=path)
        assert result["status"] == 200, path
        assert app.observed[-1] is None, f"{path} must run WITHOUT tenant context"


def test_options_preflight_bypass(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), method="OPTIONS", headers={"Origin": "http://x", "Access-Control-Request-Method": "POST"})
    assert result["status"] == 200
    assert app.observed == [None]


def test_context_reset_after_request(hs256_env):
    app = _CaptureApp()
    for _ in range(10):
        _request(TenantContextMiddleware(app), headers={"Authorization": _auth("tenant-a")})
    assert get_tenant_id_optional() is None, "context leaked into the surrounding task"


def test_many_tenants_no_leak(hs256_env):
    """100 consecutive different-tenant requests leave no residual context."""
    app = _CaptureApp()
    for i in range(100):
        tenant = f"t{i:03d}"
        _request(TenantContextMiddleware(app), headers={"Authorization": _auth(tenant)})
        assert app.observed[-1] == tenant
    assert get_tenant_id_optional() is None


def test_dev_mode_trusts_header(monkeypatch):
    monkeypatch.setenv("TENANT_AUTH_MODE", "dev")
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"x-tenant-id": "tenant-a"})
    assert result["status"] == 200
    assert app.observed == ["tenant-a"]


def test_dev_mode_rejects_invalid_header(monkeypatch):
    monkeypatch.setenv("TENANT_AUTH_MODE", "dev")
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"x-tenant-id": "../etc"})
    assert result["status"] == 400
    assert app.observed == []


def test_verify_jwt_hs256_roundtrip(hs256_env):
    token = issue_jwt(subject="u1", tenant_id="tenant-a", secret="0123456789abcdef0123456789abcdef")
    claims = verify_jwt(f"Bearer {token}")
    assert claims is not None and claims["tenant"] == "tenant-a"
    assert verify_jwt(None) is None
    assert verify_jwt("Basic abc") is None


# ── user-within-tenant claim ───────────────────────────────────────────────


def _auth_user(tenant: str, user: str | None) -> str:
    extra = {"user": user} if user else None
    return f"Bearer {issue_jwt(subject='u1', tenant_id=tenant, secret=_TEST_SECRET, extra_claims=extra)}"


def test_user_claim_binds_user_context(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": _auth_user("tenant-a", "alice")})
    assert result["status"] == 200
    assert app.observed == ["tenant-a"]
    assert app.observed_users == ["alice"]
    assert app.scope_state.get("user") == "alice"


def test_tenant_level_token_has_no_user(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": _auth_user("tenant-a", None)})
    assert result["status"] == 200
    assert app.observed_users == [None]
    assert "user" not in app.scope_state


def test_invalid_user_claim_rejected(hs256_env):
    app = _CaptureApp()
    token = issue_jwt(subject="u1", tenant_id="tenant-a", secret=_TEST_SECRET, extra_claims={"user": "Bad_User!"})
    result = _request(TenantContextMiddleware(app), headers={"Authorization": f"Bearer {token}"})
    assert result["status"] == 400
    assert app.observed == []


def test_user_context_reset_after_request(hs256_env):
    app = _CaptureApp()
    for _ in range(5):
        _request(TenantContextMiddleware(app), headers={"Authorization": _auth_user("tenant-a", "alice")})
        _request(TenantContextMiddleware(app), headers={"Authorization": _auth_user("tenant-a", None)})
    assert get_current_user_optional() is None, "user context leaked into the surrounding task"


def test_dev_mode_trusts_user_header(monkeypatch):
    monkeypatch.setenv("TENANT_AUTH_MODE", "dev")
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"x-tenant-id": "tenant-a", "x-user-id": "bob"})
    assert result["status"] == 200
    assert app.observed_users == ["bob"]


# ── role claim (permission isolation) ─────────────────────────────────────


def _auth_role(tenant: str, user: str | None, role: str | None) -> str:
    extra: dict[str, str] = {}
    if user:
        extra["user"] = user
    if role:
        extra["role"] = role
    return f"Bearer {issue_jwt(subject='u1', tenant_id=tenant, secret=_TEST_SECRET, extra_claims=extra or None)}"


def test_role_claim_binds_role_context(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": _auth_role("tenant-a", "bob", "user")})
    assert result["status"] == 200
    assert app.observed == ["tenant-a"]
    assert app.observed_users == ["bob"]
    assert app.observed_roles == ["user"]
    assert app.scope_state.get("role") == "user"


def test_admin_role_claim_binds(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": _auth_role("tenant-a", "alice", "admin")})
    assert result["status"] == 200
    assert app.observed_roles == ["admin"]


def test_tenant_level_token_has_no_role(hs256_env):
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": _auth_role("tenant-a", None, None)})
    assert result["status"] == 200
    assert app.observed_roles == [None]
    assert "role" not in app.scope_state


def test_unknown_role_value_reads_as_absent(hs256_env):
    """A foreign role value degrades to tenant-level (admin) semantics."""
    app = _CaptureApp()
    result = _request(TenantContextMiddleware(app), headers={"Authorization": _auth_role("tenant-a", "bob", "root")})
    assert result["status"] == 200
    assert app.observed_roles == [None]
    assert extract_role_claim({"role": "root"}) is None
    assert extract_role_claim({"role": "admin"}) == "admin"
    assert extract_role_claim({}) is None


def test_role_context_reset_after_request(hs256_env):
    app = _CaptureApp()
    for _ in range(5):
        _request(TenantContextMiddleware(app), headers={"Authorization": _auth_role("tenant-a", "bob", "user")})
        _request(TenantContextMiddleware(app), headers={"Authorization": _auth_role("tenant-a", None, None)})
    assert get_current_role() is None, "role context leaked into the surrounding task"


def test_is_admin_role_policy():
    assert is_admin_role(None)  # tenant-level master identity
    assert is_admin_role("admin")
    assert not is_admin_role("user")
