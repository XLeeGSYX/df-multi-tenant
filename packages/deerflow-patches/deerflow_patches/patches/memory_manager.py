"""P4: per-tenant memory manager (design doc §5, patch #4).

Target version: deer-flow @ f9f3127d (see upstream/PIN.md).
Original signature (deerflow/agents/memory/manager.py:940):

    def get_memory_manager() -> MemoryManager: ...

Recon result (design doc §13 R4): memory storage lives under the runtime
home (``{runtime_home}/users/{uid}/memory.json``, FTS5 index at
``{storage_path}/.retrieval/memory-fts5.sqlite3``) — inside the foundation
root, so the P1 path patch already scopes the *default* per tenant. BUT the
manager is a process-global singleton that EAGERLY freezes
``backend_config["storage_path"] = str(runtime_home())`` at construction
(manager.py:973-976, via a call-time function-local import of the patched
``runtime_home``). Built once, every tenant would share whichever root was
current at first call — a cross-tenant leak.

Patch: per-tenant manager cache keyed by tenant id. Two traps handled:

- **Tainted scratch slot**: ``original()`` returns the module-global
  ``_memory_manager`` verbatim when non-None, and that global holds whatever
  was built *last* — possibly another tenant's manager. The wrapper therefore
  ALWAYS resets before building; the global becomes a transient handoff slot
  that nothing reads twice.
- **No-context callers** map to the ``_platform`` key and build inside an
  explicit platform scope, so deployment-level memory never lands on another
  tenant's root (or the deerflow default outside ``.foundation``).

``reset_memory_manager()`` also clears the backend registry cache; the
re-scan on each first-build-per-tenant is accepted deliberately — correctness
over micro-perf.
"""

from __future__ import annotations

import threading

from deerflow_patches.registry import patch
from tenancy.context import PLATFORM_TENANT_ID, get_tenant_id_optional
from tenancy.propagation import call_in_tenant

_TENANT_MANAGERS: dict[str, object] = {}
_TENANT_MANAGERS_LOCK = threading.Lock()


def clear_all() -> None:
    """Drop every cached per-tenant manager (lifespan teardown hook)."""
    from deerflow.agents.memory.manager import reset_memory_manager

    with _TENANT_MANAGERS_LOCK:
        _TENANT_MANAGERS.clear()
    reset_memory_manager()


@patch("deerflow.agents.memory.manager", "get_memory_manager", phase="runtime")
def patched_get_memory_manager(original):
    def wrapper():
        tid = get_tenant_id_optional()
        # No context → platform scope (the wrapped lifespan already runs
        # startup/shutdown under _platform; unscoped request-time code gets
        # the platform manager, never another tenant's).
        key = tid if tid is not None else PLATFORM_TENANT_ID

        manager = _TENANT_MANAGERS.get(key)
        if manager is not None:
            return manager
        with _TENANT_MANAGERS_LOCK:
            manager = _TENANT_MANAGERS.get(key)
            if manager is not None:
                return manager
            from deerflow.agents.memory.manager import reset_memory_manager

            # Always reset first: the global scratch slot may hold another
            # tenant's manager and original() would hand it back verbatim.
            reset_memory_manager()
            if key == PLATFORM_TENANT_ID:
                # Build under an explicit _platform scope so the frozen
                # storage_path is the platform foundation root even when the
                # caller had no tenant context.
                manager = call_in_tenant(original, PLATFORM_TENANT_ID)
            else:
                # Current context is already `key` — build in place.
                manager = original()
            _TENANT_MANAGERS[key] = manager
            return manager

    return wrapper
