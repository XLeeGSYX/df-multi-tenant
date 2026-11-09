"""Original-lifespan wrapping (design doc §4.2).

Recon adaptation: the real gateway module is ``app.gateway.app`` (not
``deerflow.gateway.app``) and its module body ends with ``app = create_app()``
— the instance exists the moment the module is imported, BEFORE any patch
can replace ``create_app``. The create_app patch therefore also decorates
the module-level instance in place.

The wrapper can only bracket the original lifespan as a whole (design doc
§12, trap #11): fine-grained interception happens through the function-level
patches themselves.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from tenancy.context import PLATFORM_TENANT_ID, reset_tenant_id, set_tenant_id

log = logging.getLogger("deerflow_patches")

_LIFESPAN_WRAPPED_FLAG = "_tenant_lifespan_wrapped"


def wrap_lifespan(app):
    """Wrap ``app``'s lifespan with tenant bootstrap/teardown (idempotent)."""
    if getattr(app.state, _LIFESPAN_WRAPPED_FLAG, False):
        return app

    from deerflow_patches.bootstrap import apply_runtime_patches, teardown_tenant_infra, verify_patch_integrity
    from deerflow_patches.foundation import ensure_platform_root

    original = app.router.lifespan_context

    @asynccontextmanager
    async def tenant_lifespan(a):
        from tenancy.middleware import AUTH_MODE_ENV, DEV_MODE
        import os

        if os.environ.get(AUTH_MODE_ENV, "jwt") == DEV_MODE:
            log.warning(
                "TENANT_AUTH_MODE=dev is active: x-tenant-id headers are trusted without JWT "
                "verification. Never enable this in shared or production deployments."
            )

        ensure_platform_root()
        apply_runtime_patches()  # Phase B: before the original startup
        verify_patch_integrity()  # fail-fast: any dead patch refuses to boot

        # Startup AND shutdown of the original lifespan run inside the
        # platform scope so deployment-level state (shared DB bootstrap,
        # memory warm-up, public skill projection) lands under
        # .foundation/_platform instead of the deerflow default directory.
        token = set_tenant_id(PLATFORM_TENANT_ID)
        try:
            async with original(a) as state:
                # Pass the original lifespan state through unchanged; request
                # handling happens in other tasks which carry their own
                # tenant contexts from the middleware.
                yield state
            teardown_tenant_infra()
        finally:
            reset_tenant_id(token)

    app.router.lifespan_context = tenant_lifespan
    setattr(app.state, _LIFESPAN_WRAPPED_FLAG, True)
    return app


def decorate_app(app):
    """Mount the tenant middleware (outermost) and wrap the lifespan."""
    from tenancy.middleware import TenantContextMiddleware

    # add_middleware inserts at the front of the stack: the LAST added is
    # the OUTERMOST — tenant resolution must wrap CORS/auth/CSRF (design
    # doc §2.2). The middleware tolerates OPTIONS preflight and public paths.
    app.add_middleware(TenantContextMiddleware)
    return wrap_lifespan(app)
