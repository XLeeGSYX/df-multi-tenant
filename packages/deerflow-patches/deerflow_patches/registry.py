"""Patch registry with phase declaration (design doc §4.1).

Every monkey-patch is declared as a ``PatchSpec``:

- ``module``  — target module path (e.g. ``deerflow.config.runtime_paths``)
- ``attr``    — target attribute; may be dotted (``Paths.base_dir``) to
                patch a class attribute such as a property
- ``factory`` — receives the ORIGINAL value, returns the replacement
- ``phase``   — ``"import"`` (applied before the gateway app module is
                imported) or ``"runtime"`` (applied inside the wrapped
                lifespan, before the original startup runs)

Design doc §12, trap #2: ``from x import attr`` binds the value into the
importing module's namespace, so patching only the defining module leaves
stale bindings behind. ``_patch_everywhere`` re-binds every deerflow/app
module that still holds the original object.

Every patch file MUST carry a header noting the target version and the
original signature (design doc §9, prohibition #3: never write patches from
memory).
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("deerflow_patches")

# Namespaces whose ``from x import attr`` bindings need re-binding. The
# deerflow harness plus the unpublished gateway ``app`` layer (where
# create_app lives) — nothing else may be touched.
_REBIND_PREFIXES = ("deerflow", "app")
_PATCH_MARKER = "__tenant_patched__"


@dataclass
class PatchSpec:
    module: str
    attr: str
    factory: Callable
    phase: str  # "import" | "runtime"


_PATCHES: list[PatchSpec] = []
_APPLIED_IMPORT = False
_APPLIED_RUNTIME = False


def patch(module_name: str, attr: str, *, phase: str = "runtime"):
    """Declare a patch; the decorated factory builds the replacement."""

    def deco(fn):
        _PATCHES.append(PatchSpec(module_name, attr, fn, phase))
        return fn

    return deco


def _resolve_owner(module_name: str, attr: str):
    """Resolve the object owning *attr*.

    A dotted attr (``Paths.base_dir``) walks from the module to the class so
    class attributes (properties) can be replaced in place.
    """
    obj = importlib.import_module(module_name)
    if "." not in attr:
        return obj, attr
    owner_path, _, name = attr.rpartition(".")
    for part in owner_path.split("."):
        obj = getattr(obj, part)
    return obj, name


def _in_rebind_namespace(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _REBIND_PREFIXES)


def _patch_everywhere(module_name: str, attr: str, original, new_value) -> None:
    """Re-bind ``from x import attr`` copies across the deerflow/app namespaces."""
    mod = importlib.import_module(module_name)
    if "." not in attr:
        setattr(mod, attr, new_value)
    # Dotted (class-attribute) patches need no rebind scan: attribute lookup
    # on the class is dynamic, and instances never bind properties locally.
    if "." in attr:
        return
    for name, m in list(sys.modules.items()):
        if m is None or m is mod:
            continue
        if not _in_rebind_namespace(name):
            continue
        if getattr(m, attr, None) is original:
            setattr(m, attr, new_value)
            log.info("rebind %s in %s", attr, name)


def _unwrap(value):
    """Normalize wrapper objects to the underlying callable.

    classmethod/staticmethod wrappers ACCEPT attribute assignment in 3.10+,
    but the attribute is invisible through class access (``getattr(cls, name)``
    returns the bound ``__func__``). The marker must therefore always live on
    the unwrapped function; verification unwraps the same way before checking.
    Bound methods delegate attribute lookup to ``__func__`` already, so they
    need no special case.
    """
    if isinstance(value, classmethod | staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _mark_patched(new_value) -> None:
    target = _unwrap(new_value)
    try:
        setattr(target, _PATCH_MARKER, True)
    except (AttributeError, TypeError):
        # Exotic callable without a writable __dict__; verification falls
        # back to no marker (apply-time failures are what matter there).
        pass


def is_patched(value) -> bool:
    """True iff *value* (or its unwrapped function) carries the tenant-patch marker."""
    return bool(getattr(_unwrap(value), _PATCH_MARKER, False))


def _apply_one(spec: PatchSpec) -> None:
    owner, name = _resolve_owner(spec.module, spec.attr)
    original = getattr(owner, name, None)
    if original is None:
        raise RuntimeError(f"patch target missing: {spec.module}.{spec.attr} — deerflow version mismatch")
    new_value = spec.factory(original)
    _mark_patched(new_value)
    setattr(owner, name, new_value)
    _patch_everywhere(spec.module, spec.attr, original, new_value)
    log.info("patched[%s] %s:%s", spec.phase, spec.module, spec.attr)


def apply_import_critical() -> None:
    """Phase A: import-time-sensitive patches (path constants, create_app)."""
    global _APPLIED_IMPORT
    if _APPLIED_IMPORT:
        return
    for s in [p for p in _PATCHES if p.phase == "import"]:
        _apply_one(s)
    _APPLIED_IMPORT = True


def apply_runtime_patches() -> None:
    """Phase B: patches applied inside the lifespan, before original startup."""
    global _APPLIED_RUNTIME
    if _APPLIED_RUNTIME:
        return
    for s in [p for p in _PATCHES if p.phase == "runtime"]:
        _apply_one(s)
    _APPLIED_RUNTIME = True


def all_patches() -> list[PatchSpec]:
    """All declared patches (for integrity verification and tests)."""
    return list(_PATCHES)
