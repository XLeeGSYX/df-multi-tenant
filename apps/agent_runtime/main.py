"""agent-runtime entrypoint (design doc §1.2 service 2, §4.3).

Serves the tenant-patched DeerFlow gateway. Startup order matters:

    import this module
      └─ create_tenant_app()
           ├─ Phase A patches (import-critical: path chokepoints, create_app)
           ├─ import app.gateway.app  (module-level app decorated in place)
           └─ app                     ← uvicorn serves THIS instance
    lifespan
      └─ Phase B patches → integrity check → original lifespan (platform scope)

Run (see the repo Makefile; cwd must be this repo so config.yaml and the
foundation root resolve here):

    DEER_FLOW_AUTH_DISABLED=1 PYTHONPATH=".:<deer-flow-checkout>/backend" \
    uv run --project <deer-flow-checkout>/backend \
        uvicorn apps.agent_runtime.main:app --port 8001

Environment:
    DF_FOUNDATION_ROOT   multi-tenant root (default {cwd}/.foundation)
    TENANT_AUTH_MODE     jwt (default) | dev (trust x-tenant-id; local only)
    TENANT_JWKS_URL      JWKS endpoint of tenant-admin (rs256 mode)
    TENANT_JWT_SECRET    shared secret (hs256 mode)
"""

from deerflow_patches.bootstrap import create_tenant_app

app = create_tenant_app()
