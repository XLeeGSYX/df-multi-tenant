"""P11: per-tenant agent config — tenant-shared custom skills + cache freshness.

Target version: deer-flow @ f9f3127d (see upstream/PIN.md).
Original signatures (verified against that ref):

- ``deerflow/config/paths.py:257``
      def user_skills_dir(self, user_id: str) -> Path: ...
- ``deerflow/config/paths.py:261``
      def user_custom_skills_dir(self, user_id: str) -> Path: ...
- ``deerflow/agents/lead_agent/prompt.py:173``
      def get_enabled_skills_for_config(app_config=None, user_id=None) -> list[Skill]: ...

Tenant-admin materializes per-tenant agent config (lead-agent SOUL.md,
managed-subagent JSONs, custom-skill SKILL.md + ``_skill_states.json``) into
``{foundation}/{tenant}/`` — see ``tenant_db.agent_config``. Two upstream
behaviors need adjustment for that materialization to be tenant-scoped:

1. **Custom skills are user-scoped upstream.**
   ``UserScopedSkillStorage`` redirects custom skills to
   ``{base_dir}/users/{user_id}/skills/custom/`` and keeps the enabled state
   per user (``{base_dir}/users/{user_id}/skills/_skill_states.json``). In
   this product the tenant's users must SHARE the tenant's skill config, so
   both path helpers drop the user segment and resolve to
   ``{base_dir}/skills/…`` (the tenant root). Consumers: the skill storage
   itself plus the sandbox custom-skill mounts (``sandbox/tools.py:359,847``)
   — both call these two helpers, so patching them covers storage and mounts
   consistently. Everything else under ``users/{user_id}/`` (uploads,
   memory, custom agents) keeps its per-identity scope.

2. **The enabled-skills cache never sees out-of-band writes.**
   ``get_enabled_skills_for_config`` caches per ``(id(app_config), user_id)``
   and is only invalidated by in-process skill mutations
   (``_invalidate_enabled_skills_cache``). tenant-admin writes the files from
   another process, so the wrapper compares a cheap filesystem signature
   (stat sweep of the tenant's custom-skill SKILL.md files + states file)
   with a 1s guard — mirroring the managed-subagent registry's signature-TTL
   pattern — and calls the upstream invalidator when it changes. The lead
   agent factory re-imports and re-calls this symbol per agent build, so a
   change becomes visible on the next message without a restart.

The lead-agent soul and managed subagents need NO patch: SOUL.md and the
managed-subagents directory both resolve under ``{base_dir}`` (tenant root
after P1) and are re-read per agent build (subagents via the registry's own
~1s signature cache).
"""

from __future__ import annotations

import time
from pathlib import Path

from deerflow_patches.foundation import tenant_foundation_root
from deerflow_patches.registry import patch
from tenancy.context import PLATFORM_TENANT_ID, get_tenant_id_optional

# Same order of magnitude as the managed-subagent registry's signature TTL
# (deerflow/subagents/registry.py: _MANAGED_SIGNATURE_TTL_SECONDS).
_SIGNATURE_TTL_SECONDS = 1.0
_SIGNATURES: dict[str, tuple] = {}
_CHECKED_AT: dict[str, float] = {}


def tenant_skills_signature(tenant_id: str) -> tuple:
    """Cheap change-detector over the tenant's materialized skill config.

    Covers SKILL.md content changes (mtime/size), additions and removals
    (the custom dir's own mtime), and enabled toggles (states file).
    """
    skills_dir = tenant_foundation_root(tenant_id) / "skills"
    custom_dir = skills_dir / "custom"
    states_file = skills_dir / "_skill_states.json"
    parts: list[tuple] = []
    if states_file.exists():
        stat = states_file.stat()
        parts.append(("_skill_states.json", stat.st_mtime_ns, stat.st_size))
    if custom_dir.is_dir():
        stat = custom_dir.stat()
        parts.append(("_custom_dir", stat.st_mtime_ns))
        for skill_md in sorted(custom_dir.glob("*/SKILL.md")):
            stat = skill_md.stat()
            parts.append((skill_md.parent.name, stat.st_mtime_ns, stat.st_size))
    return tuple(parts)


def _maybe_invalidate_tenant_skills(invalidate) -> None:
    """Invalidate the enabled-skills caches when the tenant's files changed.

    No tenant context (or the platform scope) → nothing to check: those
    callers never read a tenant's custom skills.
    """
    tid = get_tenant_id_optional()
    if tid is None or tid == PLATFORM_TENANT_ID:
        return
    now = time.monotonic()
    if now - _CHECKED_AT.get(tid, 0.0) < _SIGNATURE_TTL_SECONDS:
        return
    _CHECKED_AT[tid] = now
    signature = tenant_skills_signature(tid)
    previous = _SIGNATURES.get(tid)
    if previous is not None and previous != signature:
        invalidate()
    _SIGNATURES[tid] = signature


@patch("deerflow.config.paths", "Paths.user_skills_dir", phase="import")
def patched_user_skills_dir(original):
    """``{base_dir}/skills/`` — tenant-shared (was ``users/{uid}/skills``)."""

    def wrapper(self, user_id: str) -> Path:
        return self.base_dir / "skills"

    return wrapper


@patch("deerflow.config.paths", "Paths.user_custom_skills_dir", phase="import")
def patched_user_custom_skills_dir(original):
    """``{base_dir}/skills/custom/`` — tenant-shared (was per-user)."""

    def wrapper(self, user_id: str) -> Path:
        return self.base_dir / "skills" / "custom"

    return wrapper


@patch("deerflow.agents.lead_agent.prompt", "get_enabled_skills_for_config", phase="runtime")
def patched_get_enabled_skills_for_config(original):
    import deerflow.agents.lead_agent.prompt as prompt_module

    invalidate = prompt_module._invalidate_enabled_skills_cache

    def wrapper(*args, **kwargs):
        _maybe_invalidate_tenant_skills(invalidate)
        return original(*args, **kwargs)

    return wrapper
