"""Patch targets must exist in the installed deerflow (design doc §10).

Every ``PatchSpec`` is checked against the live deerflow/app modules. A
missing target means the pinned upstream ref drifted — the patch layer
refuses to pretend it still works.
"""

import importlib

import pytest

pytest.importorskip("deerflow", reason="deerflow not importable in this environment")

import deerflow_patches.patches  # noqa: F401,E402  (registers every PatchSpec)
from deerflow_patches.registry import all_patches

_SPECS = all_patches()
assert _SPECS, "no patches registered — deerflow_patches.patches failed to import?"


@pytest.mark.parametrize("spec", _SPECS, ids=lambda s: f"{s.module}.{s.attr}")
def test_patch_target_exists(spec):
    owner = importlib.import_module(spec.module)
    path = spec.attr.split(".")
    walked = owner
    for part in path:
        assert hasattr(walked, part), (
            f"{spec.module}.{spec.attr} does not exist — deerflow version does not match the patch manifest (see upstream/PIN.md)"
        )
        walked = getattr(walked, part)


def test_patch_phases_are_valid():
    for spec in _SPECS:
        assert spec.phase in ("import", "runtime"), f"{spec.module}.{spec.attr} has invalid phase {spec.phase!r}"


def test_patches_apply_and_verify():
    """Full Phase A + Phase B application and integrity check (happy path).

    Runs the real bootstrap against the installed deerflow. If the gateway
    app module is importable, the module-level app instance must come out
    decorated (tenant middleware + wrapped lifespan).
    """
    from deerflow_patches.bootstrap import create_tenant_app, verify_patch_integrity
    from deerflow_patches.registry import apply_import_critical, apply_runtime_patches

    apply_import_critical()
    apply_runtime_patches()
    verify_patch_integrity()

    app = create_tenant_app()
    assert getattr(app.state, "_tenant_lifespan_wrapped", False)
