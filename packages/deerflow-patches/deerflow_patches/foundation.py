"""Foundation-root resolution shared by every path patch (design doc §3.3).

The design doc's ``.foundation/{tenant_id}`` layout maps onto DeerFlow's
real state-root mechanism: the per-tenant foundation root IS the tenant's
runtime home (DEER_FLOW_HOME equivalent). Once ``runtime_home()`` and
``Paths.base_dir`` resolve per tenant, DeerFlow's own layout
(users/, threads/, managed-subagents/, skills_view/, memory.json, ...)
lands inside the tenant directory without any further source changes.

- ``DF_FOUNDATION_ROOT`` — the multi-tenant root directory (D8: must live
  on shared storage for multi-replica deployments). Default: ``{cwd}/.foundation``.
- The platform scope ``_platform`` (D7) holds deployment-shared state; the
  wrapped lifespan runs the original startup/shutdown inside it.
"""

from __future__ import annotations

import os
from pathlib import Path

from tenancy.context import PLATFORM_TENANT_ID, get_tenant_id_optional

FOUNDATION_ROOT_ENV = "DF_FOUNDATION_ROOT"
# Host-side base for DooD bind mounts (DEER_FLOW_HOST_BASE_DIR equivalent,
# scoped per tenant under the same layout).
HOST_FOUNDATION_ROOT_ENV = "DF_HOST_FOUNDATION_ROOT"

_FOUNDATION_BASE_CACHE: dict[str, Path] = {}


def foundation_base() -> Path:
    """Resolve (and cache per env value) the multi-tenant root directory."""
    raw = os.environ.get(FOUNDATION_ROOT_ENV) or str(Path.cwd() / ".foundation")
    cached = _FOUNDATION_BASE_CACHE.get(raw)
    if cached is not None:
        return cached
    resolved = Path(raw).resolve()
    _FOUNDATION_BASE_CACHE.clear()  # single-entry cache keyed by current env
    _FOUNDATION_BASE_CACHE[raw] = resolved
    return resolved


def tenant_foundation_root(tenant_id: str) -> Path:
    """Resolve one tenant's foundation root with the second traversal defense.

    The charset/reserved-name check (D6) already ran wherever the tenant id
    entered the process; ``resolve()`` + ``is_relative_to`` here is the
    independent second gate (design doc §12, trap #10).
    """
    base = foundation_base()
    root = base / tenant_id
    resolved = root.resolve()
    if not resolved.is_relative_to(base):
        raise PermissionError("tenant path escape")
    return resolved


def host_foundation_root(tenant_id: str) -> str:
    """Host-visible foundation root for bind-mount sources (DooD setups)."""
    raw = os.environ.get(HOST_FOUNDATION_ROOT_ENV)
    if raw:
        return str(Path(raw) / tenant_id)
    return str(tenant_foundation_root(tenant_id))


def current_tenant_root() -> Path | None:
    """The foundation root for the current context, if any tenant is bound."""
    tid = get_tenant_id_optional()
    if tid is None:
        return None
    return tenant_foundation_root(tid)


def ensure_platform_root() -> Path:
    """Create the platform scope directory (idempotent; lifespan startup)."""
    root = tenant_foundation_root(PLATFORM_TENANT_ID)
    root.mkdir(parents=True, exist_ok=True)
    return root
