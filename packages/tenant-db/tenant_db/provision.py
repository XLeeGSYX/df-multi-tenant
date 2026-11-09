"""Tenant directory provisioning (design doc §3.2).

Executed by the tenant-admin service when a tenant is created. The tenant
root is the patch-layer "foundation root" — the per-tenant DEER_FLOW_HOME
under which DeerFlow's own state layout (skills_view/, users/, threads/,
managed-subagents/, memory.json, ...) lands once the path patches are
active.

Two independent traversal defenses (design doc §12, trap #10):
1. ``is_valid_tenant_id`` — charset + reserved-name rejection.
2. ``resolve()`` + ``is_relative_to`` confinement against the foundation base.
"""

from __future__ import annotations

import time
from pathlib import Path

from tenancy.context import is_valid_tenant_id

# Subdirectories created for a fresh tenant. The names mirror the design
# doc's D1 resource set; DeerFlow creates the rest of its layout lazily.
TENANT_RESOURCE_DIRS = ("skills", "subagents", "extensions", "prompts")


def provision_tenant(tenant_id: str, foundation_base: Path) -> Path:
    """Create the tenant's directory skeleton. Returns the tenant root.

    Raises ValueError on an invalid/reserved id, FileExistsError when the
    tenant directory already exists, PermissionError on path escape.
    """
    if not is_valid_tenant_id(tenant_id):
        raise ValueError(f"invalid or reserved tenant id: {tenant_id!r}")
    root = foundation_base / tenant_id
    if root.exists():
        raise FileExistsError(f"tenant dir exists: {root}")
    for d in TENANT_RESOURCE_DIRS:
        (root / d).mkdir(parents=True, exist_ok=False)
    # Second traversal defense: the resolved root must stay inside the base.
    if not root.resolve().is_relative_to(foundation_base.resolve()):
        raise PermissionError("tenant path escape")
    return root


def deprovision_tenant(tenant_id: str, foundation_base: Path, trash_dir: Path) -> Path:
    """Disable a tenant's directory without physical deletion.

    Renames ``{foundation_base}/{tenant_id}`` into
    ``{trash_dir}/{tenant_id}_{unix-ts}`` so recovery stays possible. The
    tenant row's status change (active → disabled) is the DB-side half and
    belongs to the caller's transaction. Re-deprovisioning the same id within
    one second (delete → recreate → delete, disable → enable → disable) appends
    a counter so ``rename`` never targets an existing non-empty directory.
    """
    if not is_valid_tenant_id(tenant_id):
        raise ValueError(f"invalid or reserved tenant id: {tenant_id!r}")
    src = foundation_base / tenant_id
    if not src.exists():
        raise FileNotFoundError(f"tenant dir missing: {src}")
    trash_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    dst = trash_dir / f"{tenant_id}_{stamp}"
    suffix = 1
    while dst.exists():
        dst = trash_dir / f"{tenant_id}_{stamp}_{suffix}"
        suffix += 1
    src.rename(dst)
    return dst
