"""tenant-admin agent-config endpoints (lead agent / subagents / skills).

Management-plane tests: CRUD over the DB rows AND the materialized files in
the tenant's foundation root. No deerflow import needed — runs in both test
environments. The file-format contracts themselves (JSON parses as
ManagedSubagentDefinition, SKILL.md parses with the real skill parser) are
pinned in test_agent_config_patches.py, which has deerflow available.

These tests call the endpoints with the OPERATOR key (the platform-operator
credential that outranks the tenant role model). The Bearer/role matrix —
tenant-level tokens, admin users, regular-user 403s — lives in
test_role_isolation.py.
"""

from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

_OPERATOR_KEY = "test-operator-key"


@pytest.fixture(scope="module")
def admin_app(tmp_path_factory):
    base = tmp_path_factory.mktemp("admin")
    env = {
        "TENANT_ADMIN_DB": str(base / "admin.db"),
        "DF_FOUNDATION_ROOT": str(base / "foundation"),
        "DF_TENANT_TRASH_DIR": str(base / "trash"),
        "TENANT_ADMIN_SECRETS_DIR": str(base / "secrets"),
        "TENANT_ADMIN_API_KEY": _OPERATOR_KEY,
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
        test_client.headers["x-admin-key"] = _OPERATOR_KEY
        yield test_client


@pytest.fixture()
def tenant(client, admin_app):
    response = client.post("/admin/tenants", json={"id": "cfg-tenant", "name": "Config Tenant"})
    assert response.status_code == 201, response.text
    yield "cfg-tenant"
    client.delete("/admin/tenants/cfg-tenant")


def _root(admin_app, tenant_id: str):
    return admin_app.FOUNDATION_ROOT / tenant_id


SOUL = "# 灵魂\n你是一名严谨的中文助理，回答保持简洁。"


def test_lead_agent_upsert_and_clear(client, admin_app, tenant):
    # 初始：无覆盖
    response = client.get(f"/admin/tenants/{tenant}/lead-agent")
    assert response.status_code == 200
    assert response.json() == {"soul_md": None, "updated_at": None}

    # 写入 → DB 行 + SOUL.md 物化
    response = client.put(f"/admin/tenants/{tenant}/lead-agent", json={"soul_md": SOUL})
    assert response.status_code == 200, response.text
    soul_file = _root(admin_app, tenant) / "SOUL.md"
    assert soul_file.read_text(encoding="utf-8") == SOUL

    response = client.get(f"/admin/tenants/{tenant}/lead-agent")
    assert response.json()["soul_md"] == SOUL
    assert response.json()["updated_at"] is not None

    # 清空 → 行删除 + 文件移除
    response = client.put(f"/admin/tenants/{tenant}/lead-agent", json={"soul_md": "   "})
    assert response.status_code == 200
    assert response.json()["soul_md"] is None
    assert not soul_file.exists()
    assert client.get(f"/admin/tenants/{tenant}/lead-agent").json()["soul_md"] is None


def test_lead_agent_missing_tenant_404(client):
    assert client.get("/admin/tenants/nope/lead-agent").status_code == 404
    response = client.put("/admin/tenants/nope/lead-agent", json={"soul_md": SOUL})
    assert response.status_code == 404


def test_lead_agent_disabled_tenant_409(client, admin_app):
    client.post("/admin/tenants", json={"id": "cfg-off", "name": "Off"})
    try:
        client.post("/admin/tenants/cfg-off/disable")
        response = client.put("/admin/tenants/cfg-off/lead-agent", json={"soul_md": SOUL})
        assert response.status_code == 409
        assert not (_root(admin_app, "cfg-off") / "SOUL.md").exists()
    finally:
        client.post("/admin/tenants/cfg-off/enable")
        client.delete("/admin/tenants/cfg-off")


def test_subagent_lifecycle(client, admin_app, tenant):
    payload = {
        "name": "researcher",
        "display_name": "研究员",
        "description": "负责检索与归纳资料",
        "system_prompt": "你是一名研究员，输出要点列表。",
    }
    response = client.post(f"/admin/tenants/{tenant}/subagents", json=payload)
    assert response.status_code == 201, response.text

    definition_file = _root(admin_app, tenant) / "managed-subagents" / "researcher.json"
    definition = json.loads(definition_file.read_text(encoding="utf-8"))
    assert definition["name"] == "researcher"
    assert definition["description"] == payload["description"]
    assert definition["enabled"] is True
    assert "disallowed_tools" not in definition  # 让上游 pydantic 填默认值

    # 列表：DB enabled + 文件内容合并
    response = client.get(f"/admin/tenants/{tenant}/subagents")
    assert response.status_code == 200
    listed = response.json()["subagents"]
    assert [s["name"] for s in listed] == ["researcher"]
    assert listed[0]["system_prompt"] == payload["system_prompt"]
    assert listed[0]["enabled"] is True

    # 重复创建 / 内置名 / 非法名
    assert client.post(f"/admin/tenants/{tenant}/subagents", json=payload).status_code == 409
    assert client.post(f"/admin/tenants/{tenant}/subagents", json={**payload, "name": "bash"}).status_code == 400
    assert client.post(f"/admin/tenants/{tenant}/subagents", json={**payload, "name": "Bad_Name"}).status_code == 422

    # 更新：改描述 + 关闭参与
    response = client.put(
        f"/admin/tenants/{tenant}/subagents/researcher",
        json={"description": "负责检索", "enabled": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    definition = json.loads(definition_file.read_text(encoding="utf-8"))
    assert definition["description"] == "负责检索"
    assert definition["enabled"] is False
    listed = client.get(f"/admin/tenants/{tenant}/subagents").json()["subagents"]
    assert listed[0]["enabled"] is False

    # 删除
    assert client.delete(f"/admin/tenants/{tenant}/subagents/researcher").status_code == 200
    assert not definition_file.exists()
    assert client.get(f"/admin/tenants/{tenant}/subagents").json()["subagents"] == []
    assert client.delete(f"/admin/tenants/{tenant}/subagents/researcher").status_code == 404


def test_skill_lifecycle(client, admin_app, tenant):
    payload = {
        "name": "meeting-notes",
        "description": "把讨论整理为会议纪要\n（含行动项）",
        "content": "## 步骤\n1. 提取决议\n2. 列出行动项",
    }
    response = client.post(f"/admin/tenants/{tenant}/skills", json=payload)
    assert response.status_code == 201, response.text

    skill_md = _root(admin_app, tenant) / "skills" / "custom" / "meeting-notes" / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: \"meeting-notes\"" in text

    response = client.get(f"/admin/tenants/{tenant}/skills")
    listed = response.json()["skills"]
    assert [s["name"] for s in listed] == ["meeting-notes"]
    assert listed[0]["description"] == payload["description"]
    assert listed[0]["content"] == payload["content"]
    assert listed[0]["enabled"] is True

    # 重复 / 非法名
    assert client.post(f"/admin/tenants/{tenant}/skills", json=payload).status_code == 409
    assert client.post(f"/admin/tenants/{tenant}/skills", json={**payload, "name": "Bad Name"}).status_code == 422

    # 更新内容 + 关闭参与
    response = client.put(
        f"/admin/tenants/{tenant}/skills/meeting-notes",
        json={"content": "新版内容", "enabled": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    states = json.loads((_root(admin_app, tenant) / "skills" / "_skill_states.json").read_text(encoding="utf-8"))
    assert states["meeting-notes"] == {"enabled": False}
    listed = client.get(f"/admin/tenants/{tenant}/skills").json()["skills"]
    assert listed[0]["content"] == "新版内容"
    assert listed[0]["enabled"] is False

    # 删除：目录 + 状态条目
    assert client.delete(f"/admin/tenants/{tenant}/skills/meeting-notes").status_code == 200
    assert not skill_md.exists()
    states = json.loads((_root(admin_app, tenant) / "skills" / "_skill_states.json").read_text(encoding="utf-8"))
    assert "meeting-notes" not in states
    assert client.delete(f"/admin/tenants/{tenant}/skills/meeting-notes").status_code == 404


def test_delete_tenant_cascades_agent_config(client, admin_app):
    client.post("/admin/tenants", json={"id": "cfg-cascade", "name": "Cascade"})
    client.put("/admin/tenants/cfg-cascade/lead-agent", json={"soul_md": SOUL})
    client.post(
        "/admin/tenants/cfg-cascade/subagents",
        json={"name": "helper", "description": "d", "system_prompt": "s"},
    )
    client.post(
        "/admin/tenants/cfg-cascade/skills",
        json={"name": "tmp-skill", "description": "d", "content": "c"},
    )
    assert client.delete("/admin/tenants/cfg-cascade").status_code == 200

    # 同 id 重建：配置为空（行已级联删除；文件随租户根目录进回收站）
    client.post("/admin/tenants", json={"id": "cfg-cascade", "name": "Cascade"})
    assert client.get("/admin/tenants/cfg-cascade/lead-agent").json()["soul_md"] is None
    assert client.get("/admin/tenants/cfg-cascade/subagents").json()["subagents"] == []
    assert client.get("/admin/tenants/cfg-cascade/skills").json()["skills"] == []
    client.delete("/admin/tenants/cfg-cascade")


def test_config_mutations_audited(client, tenant, admin_app):
    client.put(f"/admin/tenants/{tenant}/lead-agent", json={"soul_md": SOUL})
    with admin_app._SESSION_FACTORY() as session:
        actions = (
            session.execute(
                admin_app.select(admin_app.AuditLog.action).where(admin_app.AuditLog.tenant_id == tenant)
            )
            .scalars()
            .all()
        )
    assert "lead_agent.update" in actions
