"""Permission isolation on the management plane (agent-config endpoints).

The agent-configuration endpoints accept EITHER the operator key OR a Bearer
token whose tenant matches the path and whose role is admin-capable
(tenant-level tokens and admin-role users). Regular users get 403 — they may
only run flows. This file pins the full auth matrix; the operator-key CRUD
path is covered by test_agent_config_admin.py.

Also covers the sqlite ``role``-column mini-migration (tenant_db.schema_upgrade)
for databases created before the column existed.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys

import pytest


@pytest.fixture(scope="module")
def admin_app(tmp_path_factory):
    base = tmp_path_factory.mktemp("admin")
    env = {
        "TENANT_ADMIN_DB": str(base / "admin.db"),
        "DF_FOUNDATION_ROOT": str(base / "foundation"),
        "DF_TENANT_TRASH_DIR": str(base / "trash"),
        "TENANT_ADMIN_SECRETS_DIR": str(base / "secrets"),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    sys.modules.pop("apps.tenant_admin.main", None)
    try:
        module = importlib.import_module("apps.tenant_admin.main")
        yield module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture()
def client(admin_app):
    from fastapi.testclient import TestClient

    with TestClient(admin_app.app) as test_client:
        yield test_client


@pytest.fixture()
def tenant(client):
    response = client.post("/admin/tenants", json={"id": "role-tenant", "name": "Role Tenant"})
    assert response.status_code == 201, response.text
    client.post("/admin/tenants/role-tenant/users", json={"username": "alice", "role": "admin"})
    client.post("/admin/tenants/role-tenant/users", json={"username": "bob"})
    try:
        yield "role-tenant"
    finally:
        client.delete("/admin/tenants/role-tenant")


def _token(client, tenant: str, user: str | None = None) -> str:
    response = client.post(f"/admin/tenants/{tenant}/token", json={"user": user} if user else {})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# One representative request per endpoint family (all 10 routes share the
# same gate; the gate runs before tenant/resource lookups).
_CONFIG_REQUESTS = [
    ("GET", "/admin/tenants/{t}/lead-agent", None),
    ("PUT", "/admin/tenants/{t}/lead-agent", {"soul_md": "# x"}),
    ("GET", "/admin/tenants/{t}/subagents", None),
    ("POST", "/admin/tenants/{t}/subagents", {"name": "helper", "description": "d", "system_prompt": "s"}),
    ("GET", "/admin/tenants/{t}/skills", None),
    ("POST", "/admin/tenants/{t}/skills", {"name": "tmp-skill", "description": "d", "content": "c"}),
    ("DELETE", "/admin/tenants/{t}/skills/missing", None),
]


def test_no_credential_is_unauthorized(client, tenant):
    for method, path, body in _CONFIG_REQUESTS:
        response = client.request(method, path.format(t=tenant), json=body)
        assert response.status_code == 401, (method, path, response.status_code)


def test_regular_user_forbidden_on_all_config_endpoints(client, tenant):
    headers = _bearer(_token(client, tenant, "bob"))
    for method, path, body in _CONFIG_REQUESTS:
        response = client.request(method, path.format(t=tenant), json=body, headers=headers)
        assert response.status_code == 403, (method, path, response.status_code)
        assert "admin" in response.json()["detail"].lower()


def test_admin_user_and_tenant_level_allowed(client, tenant):
    for headers in (_bearer(_token(client, tenant, "alice")), _bearer(_token(client, tenant))):
        response = client.get(f"/admin/tenants/{tenant}/lead-agent", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["soul_md"] is None

    # A write goes through and is visible afterwards (operator key for setup
    # teardown is fine — this test only asserts the Bearer path accepts it).
    headers = _bearer(_token(client, tenant, "alice"))
    assert client.put(f"/admin/tenants/{tenant}/lead-agent", json={"soul_md": "# 管理员写入"}, headers=headers).status_code == 200
    assert client.get(f"/admin/tenants/{tenant}/lead-agent", headers=_bearer(_token(client, tenant))).json()["soul_md"] == "# 管理员写入"


def test_cross_tenant_token_forbidden(client, tenant):
    client.post("/admin/tenants", json={"id": "other-tenant", "name": "Other"})
    try:
        foreign = _bearer(_token(client, "other-tenant"))  # tenant-level = admin, wrong tenant
        response = client.get(f"/admin/tenants/{tenant}/lead-agent", headers=foreign)
        assert response.status_code == 403
        assert "tenant" in response.json()["detail"].lower()
    finally:
        client.delete("/admin/tenants/other-tenant")


def test_garbage_token_unauthorized(client, tenant):
    response = client.get(f"/admin/tenants/{tenant}/lead-agent", headers=_bearer("not-a-jwt"))
    assert response.status_code == 401


def test_operator_key_still_accepted(client, tenant, monkeypatch):
    # Mint while no operator key is configured (the token endpoint itself
    # stays operator-gated).
    bob_token = _token(client, tenant, "bob")
    monkeypatch.setenv("TENANT_ADMIN_API_KEY", "op-key")
    # Right key → allowed without any Bearer.
    response = client.get(f"/admin/tenants/{tenant}/lead-agent", headers={"x-admin-key": "op-key"})
    assert response.status_code == 200, response.text
    # Wrong key → falls through to the Bearer check → 401.
    assert client.get(f"/admin/tenants/{tenant}/lead-agent", headers={"x-admin-key": "nope"}).status_code == 401
    # Right key outranks a regular-user Bearer on the same request.
    both = {"x-admin-key": "op-key", **_bearer(bob_token)}
    assert client.get(f"/admin/tenants/{tenant}/lead-agent", headers=both).status_code == 200


def test_role_claim_signature_is_verified(client, tenant, admin_app):
    """A self-asserted role claim without a valid signature never passes."""
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "bob", "tenant": tenant, "role": "admin", "exp": 9999999999},
        "wrong-private-key",
        algorithm="HS256",
    )
    response = client.get(f"/admin/tenants/{tenant}/lead-agent", headers=_bearer(forged))
    assert response.status_code == 401


def test_alg_confusion_token_unauthorized(client, tenant):
    """Tampered payloads re-signed as HS256 fail the RS256-only check → 401."""
    import jwt as pyjwt

    token = _token(client, tenant, "alice")
    claims = pyjwt.decode(token, options={"verify_signature": False})
    tampered = pyjwt.encode(claims, "anything", algorithm="HS256")
    response = client.get(f"/admin/tenants/{tenant}/lead-agent", headers=_bearer(tampered))
    assert response.status_code == 401


# ── sqlite role-column mini-migration ──────────────────────────────────────


def test_schema_upgrade_adds_role_to_legacy_database(tmp_path):
    from tenant_db.schema_upgrade import ensure_schema

    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE tenant_users (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            username VARCHAR(32) NOT NULL,
            display_name VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL
        )
        """
    )
    con.execute(
        "INSERT INTO tenant_users (id, tenant_id, username, display_name, created_at)"
        " VALUES ('u1', 't1', 'alice', 'Alice', '2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()

    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{db}")
    ensure_schema(engine)  # idempotent
    ensure_schema(engine)

    with engine.connect() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(tenant_users)"))]
        assert "role" in columns
        # Pre-existing rows get the column default, not NULL.
        role = conn.execute(text("SELECT role FROM tenant_users WHERE username = 'alice'")).scalar()
    assert role == "user"
