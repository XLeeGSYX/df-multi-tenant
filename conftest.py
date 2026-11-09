"""Make the df-multitenant packages importable without installation.

Tests run in two environments (see Makefile):
- root workspace env (``make test``): unit tests; deerflow-dependent tests
  skip themselves via ``pytest.importorskip``.
- backend venv (``make test-patches``): the same suite with deerflow and
  the gateway app layer importable, so patch-integrity tests activate.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for pkg_dir in (
    ROOT / "packages" / "tenancy-core",
    ROOT / "packages" / "deerflow-patches",
    ROOT / "packages" / "tenant-db",
    ROOT,  # apps.*
):
    resolved = str(pkg_dir.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
