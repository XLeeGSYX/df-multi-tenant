"""Tenant agent-config materialization (lead agent / subagents / skills).

Management-plane DB rows are the editable source of truth; this module
materializes them into the tenant's foundation root in the formats the
pinned deerflow (f9f3127d) reads natively. The admin never imports deerflow,
so the shapes are re-declared here and pinned by
``tests/cross_tenant/test_agent_config_patches.py``, which round-trips them
through the real deerflow models in the backend-venv suite:

- lead-agent soul → ``{root}/SOUL.md``
  (``deerflow.config.agents_config.load_agent_soul``: default agent reads
  ``{base_dir}/SOUL.md`` on every agent build; blank content = no soul)
- managed subagent → ``{root}/managed-subagents/{name}.json``
  (``deerflow.persistence.managed_subagents.base.ManagedSubagentDefinition``,
  ``extra="forbid"`` — write exactly the known fields; the registry skips
  definitions with ``enabled=false``)
- custom skill → ``{root}/skills/custom/{name}/SKILL.md``
  (YAML frontmatter ``name`` + ``description`` required by the skill parser)
  plus enabled state in ``{root}/skills/_skill_states.json``
  (``{name: {"enabled": bool}}`` — read by the tenant-scoped skill storage
  patched in deerflow-patches)

Frontmatter values are written as JSON double-quoted scalars (a YAML subset)
so multi-line descriptions round-trip without a YAML dependency in this
package; the reader parses only the format this module writes.

All writes are UTF-8 with temp-file + ``os.replace`` so a crash never leaves
a truncated file (same discipline as the upstream stores), and every path
join re-validates the name (traversal defense in depth, D6).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

# ── deerflow-native names (pinned against f9f3127d) ────────────────────────

SOUL_FILENAME = "SOUL.md"
MANAGED_SUBAGENTS_DIRNAME = "managed-subagents"
SKILLS_DIRNAME = "skills"
SKILL_CUSTOM_DIRNAME = "custom"  # SkillCategory.CUSTOM.value
SKILL_MD_FILENAME = "SKILL.md"
SKILL_STATES_FILENAME = "_skill_states.json"

# Upstream builtins that a managed definition may never shadow
# (deerflow.subagents.builtins.BUILTIN_SUBAGENTS — asserted by the patch test).
BUILTIN_SUBAGENT_NAMES = frozenset({"general-purpose", "bash"})

# deerflow.persistence.managed_subagents.base: ^[A-Za-z0-9-]+$ → lowercased.
_SUBAGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# deerflow.skills.storage.skill_storage: ^[a-z0-9]+(?:-[a-z0-9]+)*$
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# ManagedSubagentDefinition field contract (extra="forbid"): omit a field to
# take the model default. disallowed_tools intentionally omitted — pydantic
# fills the REQUIRED_DISALLOWED_TOOLS default (task/ask_clarification/present_files).
SUBAGENT_DEFINITION_FIELDS = (
    "name",
    "display_name",
    "description",
    "system_prompt",
    "tools",
    "skills",
    "model",
    "max_turns",
    "timeout_seconds",
    "enabled",
)


def require_subagent_name(name: str) -> str:
    if not _SUBAGENT_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid subagent name: {name!r}")
    return name


def require_skill_name(name: str) -> str:
    if not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid skill name: {name!r}")
    return name


# ── atomic write helper ────────────────────────────────────────────────────


def _atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_json(target: Path, payload: dict) -> None:
    _atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_json(target: Path) -> dict | None:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ── lead-agent soul ────────────────────────────────────────────────────────


def soul_file(root: Path) -> Path:
    return root / SOUL_FILENAME


def write_soul(root: Path, soul_md: str | None) -> None:
    """Materialize (or clear) the tenant's SOUL.md.

    Blank/None removes the file so deerflow's default prompt applies
    (``load_agent_soul`` treats missing and blank identically).
    """
    target = soul_file(root)
    if soul_md is None or not soul_md.strip():
        target.unlink(missing_ok=True)
        return
    _atomic_write_text(target, soul_md)


# ── managed subagents ──────────────────────────────────────────────────────


def subagent_file(root: Path, name: str) -> Path:
    return root / MANAGED_SUBAGENTS_DIRNAME / f"{require_subagent_name(name)}.json"


def read_subagent(root: Path, name: str) -> dict | None:
    return _read_json(subagent_file(root, name))


def write_subagent(root: Path, definition: dict) -> None:
    """Write one managed-subagent definition JSON (full replacement)."""
    unknown = set(definition) - set(SUBAGENT_DEFINITION_FIELDS)
    if unknown:
        raise ValueError(f"unknown subagent fields: {sorted(unknown)}")
    _atomic_write_json(subagent_file(root, str(definition["name"])), definition)


def remove_subagent(root: Path, name: str) -> bool:
    target = subagent_file(root, name)
    if target.exists():
        target.unlink()
        return True
    return False


# ── custom skills ──────────────────────────────────────────────────────────


def skill_dir(root: Path, name: str) -> Path:
    return root / SKILLS_DIRNAME / SKILL_CUSTOM_DIRNAME / require_skill_name(name)


def skill_file(root: Path, name: str) -> Path:
    return skill_dir(root, name) / SKILL_MD_FILENAME


def skill_states_file(root: Path) -> Path:
    return root / SKILLS_DIRNAME / SKILL_STATES_FILENAME


def _frontmatter_value(key: str, text: str) -> str | None:
    """Extract one value from this module's single-line ``key: "json"`` format."""
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$",
                      text,
                      re.MULTILINE)
    if match is None:
        return None
    raw = match.group(1).strip()
    if raw.startswith('"'):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, str) else raw
        except json.JSONDecodeError:
            return raw
    return raw


def read_skill(root: Path, name: str) -> dict | None:
    """Read one custom skill: ``{name, description, content}`` (body only)."""
    target = skill_file(root, name)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if match is None:
        return {"name": name, "description": "", "content": text}
    frontmatter, body = match.group(1), text[match.end():]
    return {
        "name": _frontmatter_value("name", frontmatter) or name,
        "description": _frontmatter_value("description", frontmatter) or "",
        "content": body,
    }


def write_skill(root: Path, name: str, description: str, content: str) -> None:
    """Write one custom skill's SKILL.md (full replacement)."""
    require_skill_name(name)
    frontmatter = (
        "---\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n"
    )
    _atomic_write_text(skill_file(root, name), frontmatter + content)


def remove_skill(root: Path, name: str) -> bool:
    existed = False
    directory = skill_dir(root, name)
    if directory.exists():
        shutil.rmtree(directory)
        existed = True
    # Drop the enabled-state entry too so a re-created skill starts clean.
    states = _read_json(skill_states_file(root)) or {}
    if name in states:
        states.pop(name)
        _atomic_write_json(skill_states_file(root), states)
    return existed


def read_skill_states(root: Path) -> dict:
    return _read_json(skill_states_file(root)) or {}


def write_skill_state(root: Path, name: str, enabled: bool) -> None:
    """Set one skill's enabled flag in ``_skill_states.json`` (read-modify-write)."""
    require_skill_name(name)
    states = read_skill_states(root)
    states[name] = {"enabled": bool(enabled)}
    _atomic_write_json(skill_states_file(root), states)
