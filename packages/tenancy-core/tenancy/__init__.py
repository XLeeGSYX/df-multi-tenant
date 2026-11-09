"""tenancy-core: tenant identity and request-scoped context propagation.

Zero deerflow dependency by design (design doc §1.2): this package is shared
between the tenant-admin (management plane) and agent-runtime (data plane)
services, so it must stay importable without the DeerFlow stack.
"""

from tenancy.context import (
    RESERVED_TENANT_IDS,
    TENANT_ID_RE,
    TenantContextError,
    get_tenant_id,
    get_tenant_id_optional,
    is_valid_tenant_id,
    reset_tenant_id,
    set_tenant_id,
)

__all__ = [
    "RESERVED_TENANT_IDS",
    "TENANT_ID_RE",
    "TenantContextError",
    "get_tenant_id",
    "get_tenant_id_optional",
    "is_valid_tenant_id",
    "reset_tenant_id",
    "set_tenant_id",
]
