"""ORM-level tenant enforcement (design doc §6.2).

Registered on the tenant-db ``Session`` class (or any session factory the
caller passes to :func:`install_tenancy_events`):

- SELECT: automatic ``tenant_id`` filter via ``with_loader_criteria`` unless
  the statement opts out with ``execution_options(include_all_tenants=True)``
  (management-plane bypass — explicit and auditable, never a leak).
- INSERT: ``tenant_id`` stamped from the current context when unset; a row
  already carrying a *different* tenant id raises (cross-tenant write).
- Writes without any tenant context fail closed for tenant-scoped rows.

DeerFlow's own persistence models are NOT covered by these events (they do
not carry the mixin and their source must stay untouched); see the module
docstring in ``models.py`` for the owner-axis isolation story.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from tenancy.context import TenantContextError, get_tenant_id_optional

from tenant_db.models import TenantScopedMixin

_INSTALLED: set[type] = set()


def install_tenancy_events(session_class: type[Session] = Session) -> None:
    """Attach the tenancy listeners to *session_class* (idempotent)."""
    if session_class in _INSTALLED:
        return

    @event.listens_for(session_class, "do_orm_execute")
    def _auto_tenant_filter(state: Any) -> None:
        if not state.is_select:
            return
        if state.execution_options.get("include_all_tenants"):
            return
        tid = get_tenant_id_optional()
        if tid is None:
            # Whitelisted/startup paths never touch tenant-scoped tables;
            # business routes fail closed via get_tenant_id() upstream.
            return
        state.statement = state.statement.options(
            with_loader_criteria(TenantScopedMixin, lambda cls: cls.tenant_id == tid, include_aliases=True),
        )

    @event.listens_for(session_class, "before_flush")
    def _stamp_tenant(session: Session, ctx: Any, instances: Any) -> None:
        tid = get_tenant_id_optional()
        for obj in session.new:
            if isinstance(obj, TenantScopedMixin):
                if obj.tenant_id is None:
                    if tid is None:
                        raise TenantContextError("write without tenant context")
                    obj.tenant_id = tid
                elif tid is not None and obj.tenant_id != tid:
                    raise PermissionError(f"cross-tenant write: {obj.tenant_id} != {tid}")

    _INSTALLED.add(session_class)
