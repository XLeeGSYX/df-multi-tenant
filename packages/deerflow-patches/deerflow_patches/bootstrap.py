"""Bootstrap: two-phase patch application and the tenant app entrypoint.

Startup sequence (design doc §4.3), adapted to the recon results:

    uvicorn apps.agent_runtime.main:app
      └─ create_tenant_app()
           ├─ apply_import_critical()     # Phase A: path chokepoints + create_app
           ├─ import app.gateway.app      # deferred import so Phase A wins
           └─ return gw.app               # the decorated module-level instance
    └─ [lifespan] tenant_lifespan
         ├─ apply_runtime_patches()       # Phase B: registries/memory/etc.
         ├─ verify_patch_integrity()      # fail-fast on any dead patch
         ├─ original lifespan (inside the _platform scope)
         └─ teardown_tenant_infra()       # after original shutdown
"""

from __future__ import annotations

import importlib
import logging

from deerflow_patches.registry import all_patches, apply_import_critical, apply_runtime_patches, is_patched

log = logging.getLogger("deerflow_patches")

# Per-tenant caches registered by runtime-phase patch modules; cleared on
# shutdown (and available for explicit invalidation).
_TEARDOWN_HOOKS: list = []


def register_teardown(fn) -> None:
    """Register a no-arg cleanup for per-tenant caches (idempotent)."""
    if fn not in _TEARDOWN_HOOKS:
        _TEARDOWN_HOOKS.append(fn)


def create_tenant_app():
    """Build the tenant-aware gateway app (call once per process)."""
    # Importing the patch modules registers every PatchSpec — must happen
    # before Phase A applies them.
    import deerflow_patches.patches  # noqa: F401  (registration side effect)

    apply_import_critical()

    # Deferred import (design doc §4.3): the gateway module import executes
    # ``app = create_app()`` with the ORIGINAL factory; the import-phase
    # patch on the module's ``app`` attribute has already decorated that
    # instance in place.
    import app.gateway.app as gw

    return gw.app


def verify_patch_integrity() -> None:
    """Fail-fast when any declared patch is not live (design doc §4.4).

    A silently dead patch is an isolation hole, not a degradation — the
    process refuses to serve.
    """
    failures = []
    for spec in all_patches():
        module = importlib.import_module(spec.module)
        owner = module
        if "." in spec.attr:
            path, _, leaf = spec.attr.rpartition(".")
            for part in path.split("."):
                owner = getattr(owner, part)
            current = getattr(owner, leaf, None)
        else:
            current = getattr(module, spec.attr, None)
        if not is_patched(current):
            failures.append(f"{spec.module}.{spec.attr}")
    if failures:
        raise RuntimeError(f"patch 未生效，拒绝启动: {failures}")


def teardown_tenant_infra() -> None:
    """Clear per-tenant caches after the original lifespan shut down."""
    for fn in list(_TEARDOWN_HOOKS):
        try:
            fn()
        except Exception:
            log.exception("teardown hook %r failed", fn)
    _TEARDOWN_HOOKS.clear()


__all__ = [
    "apply_runtime_patches",
    "create_tenant_app",
    "register_teardown",
    "teardown_tenant_infra",
    "verify_patch_integrity",
]
