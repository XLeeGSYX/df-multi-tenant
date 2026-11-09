# Upstream Pin

The `deerflow` runtime consumed by this repo is **not** a PyPI dependency. It is
the sibling checkout referenced by `DEER_FLOW_ROOT` (default
`../deer-flow-demo`), imported through its backend venv and `PYTHONPATH`.
That checkout is **read-only** for this repo: no commits, no working-tree
edits, no vendored copies (design doc §9 prohibitions ① and ②).

## Locked ref

| Field    | Value |
| -------- | ----- |
| commit   | `f9f3127dc144f231acae9a522edd36ffbc96e439` |
| short    | `f9f3127d` |
| describe | `v2.1.0-rc0-25-gf9f3127d` (25 commits after `v2.1.0-rc0`) |
| date     | 2026-09-18 |
| venv     | `$(DEER_FLOW_ROOT)/backend/.venv` (uv workspace: `packages/harness`, `packages/extension-api`) |

Every patch module header in `packages/deerflow-patches` repeats this short
hash. `tests/patch_integrity/` fails loudly if any patch target drifts from
this ref.

## Why not `v2.2.0` exactly (deviation from the design doc)

The design doc assumed a published DeerFlow **2.2** base. Reality:

- The pinned checkout's `backend/pyproject.toml` declares `2.1.0-rc0` and is
  25 commits past that tag — there is no local `v2.2.0` ref.
- A network fetch of `refs/tags/v2.2.0` from origin was attempted
  (twice, including a `--depth=1` retry) and failed with
  `fatal: early EOF / fetch-pack: invalid index-pack output`.

Rather than pin a tag we cannot verify, the patches pin the code that actually
executes in the user's backend venv — which is what "no guessing patches"
(§9 prohibition ③) demands. All target symbols were read from this ref, not
from memory or from a different version.

**To re-pin to a newer ref:** check it out in `DEER_FLOW_ROOT`, re-run
`make test-patches`, fix any target drift it reports, and update the hash in
this file plus the patch headers.

## Patch-target files recon came from (all at this ref)

| Patch | File | Line |
| ----- | ---- | ---- |
| P1 `runtime_home` | `backend/packages/harness/deerflow/config/runtime_paths.py` | ~19 |
| P1 `Paths.base_dir` | `backend/packages/harness/deerflow/config/paths.py` | ~160 |
| P3 `ExtensionsConfig.resolve_config_path` | `backend/packages/harness/deerflow/config/extensions_config.py` | — |
| P4 `get_memory_manager` | `backend/packages/harness/deerflow/memory/manager.py` (module scratch slot) | — |
| P10 `get_auth_disabled_user` | `backend/app/gateway/auth_disabled.py` | — |
| D2 `create_app` + module `app` | `backend/app/gateway/app.py` | ~698 / ~1048 |
