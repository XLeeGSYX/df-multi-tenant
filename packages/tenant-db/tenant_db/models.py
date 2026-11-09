"""Tenant-scoped business models (design doc §6.1).

Scope note (deviation from the doc's model list): DeerFlow's own runtime
tables — threads_meta / runs / run_events / checkpointer state — are owned
by the deerflow persistence layer and cannot inherit our mixin without
modifying its source. They stay in the deployment-level database configured
by ``config.yaml``; per-tenant separation of their rows is achieved on the
owner axis (each tenant's requests run as a tenant-scoped synthetic user id,
see the auth-disabled patch in deerflow-patches). The tables here are the
multi-tenant stack's OWN business records, which is what the ORM events in
``tenancy_events.py`` enforce.

All primary keys are global UUIDs (never autoincrement) so rows can be
merged or moved across tenants without key collisions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for the tenant-db business tables."""


class TenantScopedMixin:
    """Mixin stamping every tenant-scoped row with its owning tenant id."""

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class Tenant(Base):
    """A tenant record. ``id`` doubles as the tenant directory name (D6)."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class TenantUser(TenantScopedMixin, Base):
    """A user within a tenant (management-plane user registry).

    The row is the identity record tenant-admin mints ``user``-claim JWTs
    against; the data plane derives its per-user synthetic identity (and with
    it the owner axis) from the claim — see the P10 patch in deerflow-patches.
    ``role`` ("admin" | "user") is the permission axis: admin-role users may
    configure the tenant's agents, user-role ones may only run flows.
    """

    __tablename__ = "tenant_users"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_tenant_user_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class LeadAgent(TenantScopedMixin, Base):
    """The tenant's lead-agent soul (SOUL.md) — management-plane source of truth.

    One row per tenant: ``soul_md`` NULL means no override (deerflow's default
    lead-agent prompt applies and no SOUL.md is materialized). tenant-admin
    materializes the text to ``{tenant_root}/SOUL.md``, which deerflow's
    default-agent soul loader re-reads on every agent build, so edits apply to
    the next message without a restart.
    """

    __tablename__ = "lead_agents"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    soul_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class Skill(TenantScopedMixin, Base):
    """DB-side index of a skill package living in the tenant's directory.

    File bytes are the content; this row is the metadata index (design doc
    §8: reads go through DB metadata, writes are temp-file + DB tx + rename).
    """

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_skill_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class SubAgent(TenantScopedMixin, Base):
    """DB-side index of a tenant-managed subagent definition."""

    __tablename__ = "subagents"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_subagent_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class CustomAgent(TenantScopedMixin, Base):
    """DB-side index of a tenant's custom agent (config.yaml + SOUL.md pair)."""

    __tablename__ = "custom_agents"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_custom_agent_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ScheduledTask(TenantScopedMixin, Base):
    """Tenant ownership record for a scheduled task.

    The runnable definition stays in deerflow's scheduled_tasks table; this
    row carries the tenant binding used by the P5 dispatch patch to restore
    the tenant context when the scheduler fires (design doc §7).
    """

    __tablename__ = "scheduled_tasks"
    __table_args__ = (UniqueConstraint("tenant_id", "external_task_id", name="uq_scheduled_task_tenant_external"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ChannelConnection(TenantScopedMixin, Base):
    """IM channel connection bound to a tenant (design doc §7, P7).

    The bot ↔ tenant binding is established at provisioning time by the
    tenant-admin service; the P7 callback patch looks the tenant up here.
    """

    __tablename__ = "channel_connections"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", "external_id", name="uq_channel_tenant_provider_external"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AuditLog(Base):
    """Append-only audit trail (tenant-admin writes; never tenant-filtered)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
