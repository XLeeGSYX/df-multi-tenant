"""Patch modules — import order is the Phase-A application order.

``foundation_paths`` must register before ``create_app``: applying the
create_app patch imports the whole gateway graph, and every module-level
``from deerflow.config.runtime_paths import runtime_home`` binding created
during that import must capture the ALREADY-patched function.

Runtime-phase patches (registries, memory, extensions config) register
afterwards; their targets are imported lazily when Phase B applies.
"""

# P1 — foundation root path resolution (import phase).
import deerflow_patches.patches.foundation_paths  # noqa: F401

# D2 — create_app replacement + module-level app decoration (import phase).
import deerflow_patches.patches.create_app  # noqa: F401

# P10 — per-tenant synthetic user under deerflow's auth-disabled mode, so
# deployment-DB rows (threads_meta/runs/...) stay separated on the owner
# axis without touching deerflow's schema.
import deerflow_patches.patches.auth_user  # noqa: F401

# P4 — per-tenant memory manager (the original eagerly freezes its storage
# root into a process-global singleton).
import deerflow_patches.patches.memory_manager  # noqa: F401

# P3 — per-tenant extensions_config.json resolution (single read/write
# chokepoint on ExtensionsConfig.resolve_config_path).
import deerflow_patches.patches.extensions_config  # noqa: F401

# P11 — tenant-shared custom skills + enabled-skills cache freshness for the
# per-tenant agent config materialized by tenant-admin (SOUL.md and managed
# subagents need no patch: both resolve under the tenant base_dir and are
# re-read per agent build).
import deerflow_patches.patches.tenant_agent_config  # noqa: F401

# Teardown aggregation (design doc §4.4): per-tenant caches registered for
# lifespan-shutdown clearing.
from deerflow_patches.bootstrap import register_teardown
from deerflow_patches.patches import memory_manager

register_teardown(memory_manager.clear_all)
