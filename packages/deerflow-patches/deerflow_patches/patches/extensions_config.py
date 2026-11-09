"""P3: per-tenant extensions config (design doc §5, patch #3).

Target version: deer-flow @ f9f3127d (see upstream/PIN.md).
Original signature (deerflow/config/extensions_config.py:419):

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path | None: ...

Recon result: extensions_config.json resolves against the PROJECT ROOT
(``DEER_FLOW_EXTENSIONS_CONFIG_PATH`` env → ``existing_project_file`` →
legacy backend/repo roots) — deployment-shared, independent of the
foundation root. Every reader AND writer funnels through this single
classmethod (gateway routers skills.py:722, mcp.py:1185/1241/1283/1324 call
it per request), so one patch scopes both directions.

Resolution after the patch (tenant context present):
1. explicit ``config_path`` argument — honored unchanged (caller's pin);
2. ``{tenant_root}/extensions/extensions_config.json`` when the tenant has
   one (created by provisioning or copied as a platform-config fork);
3. original chain (env pin → project root → legacy) as the platform-shared
   fallback, so a fresh tenant inherits the deployment's MCP servers.

Startup (platform scope) keeps reading the deployment-level file, matching
the mcp-task snapshot semantics that freeze at startup.
"""

from __future__ import annotations

from pathlib import Path

from deerflow_patches.foundation import tenant_foundation_root
from deerflow_patches.registry import patch
from tenancy.context import PLATFORM_TENANT_ID, get_tenant_id_optional

TENANT_EXTENSIONS_CONFIG_NAME = "extensions_config.json"


def tenant_extensions_config_path(tenant_id: str) -> Path:
    return tenant_foundation_root(tenant_id) / "extensions" / TENANT_EXTENSIONS_CONFIG_NAME


@patch("deerflow.config.extensions_config", "ExtensionsConfig.resolve_config_path", phase="runtime")
def patched_resolve_config_path(original):
    # ``original`` is the bound classmethod on ExtensionsConfig.
    def resolve(config_path: str | None = None) -> Path | None:
        if config_path is not None:
            return original(config_path)
        tid = get_tenant_id_optional()
        if tid is not None and tid != PLATFORM_TENANT_ID:
            tenant_file = tenant_extensions_config_path(tid)
            if tenant_file.exists():
                return tenant_file
        return original(config_path)

    return classmethod(lambda cls, config_path=None: resolve(config_path))
