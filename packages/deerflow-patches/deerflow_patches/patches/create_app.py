"""D2: create_app replacement + module-level app decoration.

Target version: deer-flow @ f9f3127d (see upstream/PIN.md).
Original signature (app/gateway/app.py:698):

    def create_app() -> FastAPI: ...

Recon result (design doc §13 R1): the gateway factory lives in the
unpublished app layer ``app.gateway.app`` — NOT ``deerflow.gateway.app``
as the design doc assumed — and the module body ends with
``app = create_app()`` (line 1048). Two consequences:

1. ``create_app`` is patched for any future caller (design doc §4.2).
2. The already-created module-level instance is decorated in place — the
   agent-runtime entrypoint serves THAT instance, so the gateway is never
   constructed twice.
"""

from deerflow_patches.lifespan import decorate_app
from deerflow_patches.registry import patch


@patch("app.gateway.app", "create_app", phase="import")
def patched_create_app(original):
    def wrapper(*args, **kwargs):
        app = original(*args, **kwargs)
        return decorate_app(app)

    return wrapper


@patch("app.gateway.app", "app", phase="import")
def patched_module_app(original_app):
    # Mutate-and-return: every ``from app.gateway.app import app`` binding
    # keeps pointing at this single decorated instance.
    return decorate_app(original_app)
