"""P1: foundation root path resolution (design doc §5, patch #1).

Target version: deer-flow @ f9f3127d (see upstream/PIN.md).
Original signatures (deerflow/config/runtime_paths.py:19 and
deerflow/config/paths.py:139/160):

    def runtime_home() -> Path: ...
    class Paths:
        @property
        def host_base_dir(self) -> Path: ...
        def _host_base_dir_str(self) -> str: ...   # plain method, not a property
        @property
        def base_dir(self) -> Path: ...

Recon result (design doc §13 R2): there is no ``get_foundation_root``
symbol. The real chokepoints are ``runtime_home()`` (used by
``Paths.base_dir``'s fallback) and the ``Paths`` class properties. Both
resolve per access (no caching in ``Paths`` itself), so a per-tenant
override propagates to every request-time consumer — threads, uploads,
artifacts, sandbox mounts, user skill directories, managed subagents —
which recon confirmed all resolve through ``get_paths()``.

``Paths.base_dir`` priority after the patch:
1. explicit constructor ``base_dir`` (tests pin it deliberately);
2. current tenant context → ``{DF_FOUNDATION_ROOT}/{tenant_id}``;
3. original fallback (DEER_FLOW_HOME env or ``{project_root}/.deer-flow``).

The no-context fallback (3) only fires during module import or genuinely
unscoped code paths; the wrapped lifespan runs startup/shutdown inside the
``_platform`` scope so deployment-level state lands under
``.foundation/_platform`` instead of the deerflow default.
"""

from __future__ import annotations

import logging
import os

from deerflow_patches.foundation import host_foundation_root, tenant_foundation_root
from deerflow_patches.registry import patch
from tenancy.context import get_tenant_id_optional

log = logging.getLogger("deerflow_patches")


@patch("deerflow.config.runtime_paths", "runtime_home", phase="import")
def patched_runtime_home(original):
    def wrapper():
        tid = get_tenant_id_optional()
        if tid is not None:
            return tenant_foundation_root(tid)
        log.debug("runtime_home resolved outside tenant context; using original resolution")
        return original()

    return wrapper


@patch("deerflow.config.paths", "Paths.base_dir", phase="import")
def patched_base_dir(original_property):
    original_fget = original_property.fget

    def fget(self):
        if self._base_dir is not None:
            # Explicit constructor pin (tests, tooling) is always honored.
            return self._base_dir
        tid = get_tenant_id_optional()
        if tid is not None:
            return tenant_foundation_root(tid)
        return original_fget(self)

    return property(fget)


@patch("deerflow.config.paths", "Paths.host_base_dir", phase="import")
def patched_host_base_dir(original_property):
    original_fget = original_property.fget

    def fget(self):
        # DEER_FLOW_HOST_BASE_DIR (DooD host-side mount source) must be
        # scoped per tenant, or sandbox bind mounts collide across tenants.
        if os.getenv("DEER_FLOW_HOST_BASE_DIR"):
            tid = get_tenant_id_optional()
            if tid is not None:
                from pathlib import Path

                return Path(host_foundation_root(tid))
        return original_fget(self)

    return property(fget)


@patch("deerflow.config.paths", "Paths._host_base_dir_str", phase="import")
def patched_host_base_dir_str(original_method):
    """``_host_base_dir_str`` is a plain method (not a property) upstream."""

    def wrapper(self):
        if os.getenv("DEER_FLOW_HOST_BASE_DIR"):
            tid = get_tenant_id_optional()
            if tid is not None:
                return host_foundation_root(tid)
        return original_method(self)

    return wrapper
