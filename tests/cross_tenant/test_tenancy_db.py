"""tenant-db: ORM enforcement + provisioning (design doc §3.2, §6.2, §10)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tenancy.context import TenantContextError, get_tenant_id_optional, reset_tenant_id, set_tenant_id
from tenant_db.models import AuditLog, Base, Skill, Tenant
from tenant_db.provision import deprovision_tenant, provision_tenant
from tenant_db.tenancy_events import install_tenancy_events


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tenant_db.sqlite3'}")
    Base.metadata.create_all(engine)
    install_tenancy_events()
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _in_tenant(tenant_id: str):
    token = set_tenant_id(tenant_id)
    return token


def test_insert_stamps_tenant_and_select_filters(session_factory):
    token_a = _in_tenant("tenant-a")
    try:
        with session_factory() as s:
            s.add(Skill(name="web-research", rel_path="skills/web-research", tenant_id=None))
            s.commit()
    finally:
        reset_tenant_id(token_a)

    token_b = _in_tenant("tenant-b")
    try:
        with session_factory() as s:
            s.add(Skill(name="other-skill", rel_path="skills/other", tenant_id=None))
            s.commit()
            names_b = [row.name for row in s.execute(select(Skill)).scalars()]
            assert names_b == ["other-skill"], "tenant-b must not see tenant-a rows"
    finally:
        reset_tenant_id(token_b)

    token_a = _in_tenant("tenant-a")
    try:
        with session_factory() as s:
            names_a = [row.name for row in s.execute(select(Skill)).scalars()]
            assert names_a == ["web-research"]
    finally:
        reset_tenant_id(token_a)


def test_forged_cross_tenant_write_raises(session_factory):
    token = _in_tenant("tenant-a")
    try:
        with session_factory() as s:
            s.add(Skill(name="smuggled", rel_path="x", tenant_id="tenant-b"))
            with pytest.raises(PermissionError):
                s.commit()
    finally:
        reset_tenant_id(token)


def test_write_without_context_fails_closed(session_factory):
    assert get_tenant_id_optional() is None  # no leaked context from earlier tests
    with session_factory() as s:
        s.add(Skill(name="orphan", rel_path="x", tenant_id=None))
        with pytest.raises(TenantContextError):
            s.commit()


def test_include_all_tenants_bypass(session_factory):
    for tenant in ("tenant-a", "tenant-b"):
        token = _in_tenant(tenant)
        try:
            with session_factory() as s:
                s.add(Skill(name=f"skill-{tenant}", rel_path="x", tenant_id=None))
                s.commit()
        finally:
            reset_tenant_id(token)

    # Management plane: explicit, auditable bypass sees every tenant.
    with session_factory() as s:
        rows = s.execute(select(Skill).execution_options(include_all_tenants=True)).scalars().all()
        assert {r.tenant_id for r in rows} == {"tenant-a", "tenant-b"}


def test_audit_log_unscoped_and_tenant_table_unfiltered(session_factory):
    with session_factory() as s:
        s.add(Tenant(id="tenant-a", name="A"))
        s.add(AuditLog(tenant_id="tenant-a", actor="op", action="tenant.create"))
        s.commit()
        tenants = s.execute(select(Tenant)).scalars().all()
        audits = s.execute(select(AuditLog)).scalars().all()
        assert [t.id for t in tenants] == ["tenant-a"]
        assert len(audits) == 1


def test_provision_and_deprovision(tmp_path):
    base = tmp_path / ".foundation"
    root = provision_tenant("tenant-a", base)
    assert root == base / "tenant-a"
    for sub in ("skills", "subagents", "extensions", "prompts"):
        assert (root / sub).is_dir()

    with pytest.raises(ValueError):
        provision_tenant("skills", base)  # reserved
    with pytest.raises(ValueError):
        provision_tenant("../escape", base)
    with pytest.raises(FileExistsError):
        provision_tenant("tenant-a", base)

    trash = base / "_trash"
    moved = deprovision_tenant("tenant-a", base, trash)
    assert moved.exists()
    assert not (base / "tenant-a").exists()
    # Recoverable, not deleted:
    assert (moved / "skills").is_dir()
