"""deerflow-patches: the only invasive package in the multi-tenant stack.

Loaded exclusively by the agent-runtime service (design doc §1.2). Applying
the patches happens through :mod:`deerflow_patches.bootstrap`; importing
this package alone registers nothing and imports no deerflow code.
"""

from deerflow_patches.registry import PatchSpec, all_patches, patch

__all__ = ["PatchSpec", "all_patches", "patch"]
