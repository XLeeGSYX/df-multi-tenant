"""tenant-admin user registry + user-claim tokens (management plane).

Imports the real FastAPI app against throwaway env (tmp DB / foundation /
secrets), so no deerflow import is needed — runs in both test environments.

The app module reads its env at import time, so it is imported inside the
fixture after the env is pointed at tmp paths (no other test imports it).
"""

from __future__ import annotations

import importlib
import os
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


def _create_tenant(client, tenant_id="tenant-a"):
    response = client.post("/admin/tenants", json={"id": tenant_id, "name": "A"})
    assert response.status_code == 201, response.text


def test_user_lifecycle(client):
    _create_tenant(client)
    created = client.post("/admin/tenants/tenant-a/users", json={"username": "alice", "display_name": "Alice"})
    assert created.status_code == 201, created.text
    assert created.json() == {"username": "alice", "display_name": "Alice", "role": "user"}

    # explicit admin role round-trips
    assert client.post("/admin/tenants/tenant-a/users", json={"username": "root", "role": "admin"}).json()["role"] == "admin"
    # invalid role → 422 (pydantic pattern)
    assert client.post("/admin/tenants/tenant-a/users", json={"username": "eve", "role": "root"}).status_code == 422

    # duplicate username → 409
    assert client.post("/admin/tenants/tenant-a/users", json={"username": "alice"}).status_code == 409
    # invalid username → 422 (pydantic pattern)
    assert client.post("/admin/tenants/tenant-a/users", json={"username": "Bad_User!"}).status_code == 422
    # unknown tenant → 404
    assert client.post("/admin/tenants/nope/users", json={"username": "x"}).status_code == 404

    listed = client.get("/admin/tenants/tenant-a/users")
    assert listed.status_code == 200
    assert [(u["username"], u["role"]) for u in listed.json()["users"]] == [("alice", "user"), ("root", "admin")]

    # role toggle + unknown user
    assert client.put("/admin/tenants/tenant-a/users/alice", json={"role": "admin"}).json()["role"] == "admin"
    assert client.put("/admin/tenants/tenant-a/users/alice", json={"role": "user"}).json()["role"] == "user"
    assert client.put("/admin/tenants/tenant-a/users/ghost", json={"role": "admin"}).status_code == 404
    assert client.put("/admin/tenants/tenant-a/users/alice", json={"role": "owner"}).status_code == 422

    assert client.delete("/admin/tenants/tenant-a/users/alice").status_code == 200
    assert client.delete("/admin/tenants/tenant-a/users/alice").status_code == 404
    remaining = client.get("/admin/tenants/tenant-a/users").json()["users"]
    assert [(u["username"], u["role"]) for u in remaining] == [("root", "admin")]


def test_token_user_claim_requires_registered_user(client, admin_app):
    import jwt as pyjwt

    _create_tenant(client, "tenant-d")
    client.post("/admin/tenants/tenant-d/users", json={"username": "alice"})

    # Unregistered user → 404, never minted.
    assert client.post("/admin/tenants/tenant-d/token", json={"user": "mallory"}).status_code == 404

    ok = client.post("/admin/tenants/tenant-d/token", json={"user": "alice"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["role"] == "user"
    claims = pyjwt.decode(ok.json()["access_token"], options={"verify_signature": False})
    assert claims["tenant"] == "tenant-d"
    assert claims["user"] == "alice"
    assert claims["role"] == "user"

    # Promote → the next token carries the admin role.
    client.put("/admin/tenants/tenant-d/users/alice", json={"role": "admin"})
    promoted = client.post("/admin/tenants/tenant-d/token", json={"user": "alice"})
    assert promoted.json()["role"] == "admin"
    assert pyjwt.decode(promoted.json()["access_token"], options={"verify_signature": False})["role"] == "admin"

    # Tenant-level token carries no user/role claim.
    plain = client.post("/admin/tenants/tenant-d/token", json={})
    assert plain.json()["role"] == "admin"
    claims = pyjwt.decode(plain.json()["access_token"], options={"verify_signature": False})
    assert claims["tenant"] == "tenant-d"
    assert "user" not in claims
    assert "role" not in claims


def test_deleting_tenant_removes_users(client):
    _create_tenant(client, "tenant-b")
    client.post("/admin/tenants/tenant-b/users", json={"username": "bob"})
    assert client.delete("/admin/tenants/tenant-b").status_code == 200
    # Tenant gone → its user endpoints 404.
    assert client.get("/admin/tenants/tenant-b/users").status_code == 404


def test_disabled_tenant_cannot_mint(client):
    _create_tenant(client, "tenant-c")
    client.post("/admin/tenants/tenant-c/users", json={"username": "carol"})
    client.post("/admin/tenants/tenant-c/disable")
    assert client.post("/admin/tenants/tenant-c/token", json={"user": "carol"}).status_code == 409
