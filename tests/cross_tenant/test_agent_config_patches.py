"""P11 agent-config patches + deerflow-native format round-trips (pin f9f3127d).

Two jobs:

1. Behavioral checks of the P11 patches (tenant-shared custom-skill paths,
   enabled-skills cache freshness) against the installed deerflow.
2. Drift guards: the files tenant-admin materializes must round-trip through
   the real deerflow models and parsers — ``ManagedSubagentDefinition``, the
   SKILL.md parser, ``load_agent_soul``, and the managed-subagent registry —
   so an upstream format change fails here, not in production.

Skips when deerflow is not importable (tenant-admin-only environments).
"""

from __future__ import annotations

import pytest

pytest.importorskip("deerflow", reason="deerflow not importable in this environment")

from tenancy.context import PLATFORM_TENANT_ID, reset_tenant_id, set_tenant_id  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def applied_patches(tmp_path_factory):
    """Apply Phase A against a throwaway foundation root."""
    import os

    base = tmp_path_factory.mktemp("foundation")
    (base / "tenant-a").mkdir()
    (base / "tenant-b").mkdir()
    (base / PLATFORM_TENANT_ID).mkdir()

    old = os.environ.get("DF_FOUNDATION_ROOT")
    os.environ["DF_FOUNDATION_ROOT"] = str(base)
    try:
        import deerflow_patches.bootstrap  # noqa: F401
        import deerflow_patches.foundation as foundation
        import deerflow_patches.patches  # noqa: F401  (registration side effect — same as create_tenant_app)

        foundation._FOUNDATION_BASE_CACHE.clear()

        from deerflow_patches.registry import apply_import_critical, apply_runtime_patches

        apply_import_critical()
        apply_runtime_patches()
        yield base
    finally:
        if old is None:
            os.environ.pop("DF_FOUNDATION_ROOT", None)
        else:
            os.environ["DF_FOUNDATION_ROOT"] = old


def test_p11_patches_installed(applied_patches):
    from deerflow.config import paths as paths_module
    from deerflow.agents.lead_agent import prompt as prompt_module
    from deerflow_patches.registry import is_patched

    assert is_patched(paths_module.Paths.user_skills_dir)
    assert is_patched(paths_module.Paths.user_custom_skills_dir)
    assert is_patched(prompt_module.get_enabled_skills_for_config)


def test_skill_paths_tenant_shared(applied_patches):
    """P11: custom skills live on the tenant root, shared across its users."""
    from deerflow.config.paths import get_paths

    paths = get_paths()
    token = set_tenant_id("tenant-a")
    try:
        assert paths.user_skills_dir("u1") == applied_patches / "tenant-a" / "skills"
        assert paths.user_custom_skills_dir("u1") == applied_patches / "tenant-a" / "skills" / "custom"
        # tenant-shared: the user segment no longer participates
        assert paths.user_skills_dir("u2") == paths.user_skills_dir("u1")
        assert paths.user_custom_skills_dir("u2") == paths.user_custom_skills_dir("u1")
        # everything else under users/{uid} keeps per-identity scope
        assert paths.user_dir("u1") == applied_patches / "tenant-a" / "users" / "u1"
    finally:
        reset_tenant_id(token)

    token = set_tenant_id("tenant-b")
    try:
        assert paths.user_custom_skills_dir("u9") == applied_patches / "tenant-b" / "skills" / "custom"
    finally:
        reset_tenant_id(token)


def test_skill_md_round_trips_through_deerflow_parser(applied_patches):
    """Drift guard: admin-written SKILL.md parses with the real parser.

    The multi-line description is the risky part of the JSON-quoted YAML
    frontmatter this package writes without a YAML dependency.
    """
    from deerflow.skills.parser import parse_skill_file
    from deerflow.skills.types import SkillCategory
    from tenant_db import agent_config

    root = applied_patches / "tenant-a"
    description = "把讨论整理为会议纪要\n（含行动项）"
    agent_config.write_skill(root, "meeting-notes", description, "## 步骤\n1. 提取决议")

    skill = parse_skill_file(agent_config.skill_file(root, "meeting-notes"), SkillCategory.CUSTOM)
    assert skill is not None
    assert skill.name == "meeting-notes"
    assert skill.description == description
    assert skill.category == SkillCategory.CUSTOM

    text = agent_config.skill_file(root, "meeting-notes").read_text(encoding="utf-8")
    assert text.endswith("## 步骤\n1. 提取决议")

    # read_skill mirrors what the admin API returns (body only)
    read_back = agent_config.read_skill(root, "meeting-notes")
    assert read_back["description"] == description
    assert read_back["content"] == "## 步骤\n1. 提取决议"


def test_subagent_json_round_trips_managed_definition(applied_patches):
    """Drift guard: admin-written JSON satisfies ManagedSubagentDefinition
    (extra="forbid"), with model defaults filled by pydantic."""
    from deerflow.persistence.managed_subagents.base import ManagedSubagentDefinition
    from tenant_db import agent_config

    root = applied_patches / "tenant-a"
    definition = {
        "name": "researcher",
        "display_name": "研究员",
        "description": "负责检索与归纳资料",
        "system_prompt": "你是一名研究员，输出要点列表。",
    }
    agent_config.write_subagent(root, definition)

    parsed = ManagedSubagentDefinition.model_validate_json(
        agent_config.subagent_file(root, "researcher").read_text(encoding="utf-8")
    )
    assert parsed.name == "researcher"
    assert parsed.display_name == "研究员"
    assert parsed.system_prompt == definition["system_prompt"]
    assert parsed.enabled is True
    assert parsed.model == "inherit"
    assert parsed.max_turns == 50
    assert parsed.timeout_seconds == 900
    assert parsed.disallowed_tools  # REQUIRED_DISALLOWED_TOOLS default filled by pydantic

    # disabled definitions round-trip too (the registry skips them)
    agent_config.write_subagent(root, {**definition, "enabled": False})
    parsed = ManagedSubagentDefinition.model_validate_json(
        agent_config.subagent_file(root, "researcher").read_text(encoding="utf-8")
    )
    assert parsed.enabled is False


def test_builtin_subagent_names_match_upstream(applied_patches):
    """Drift guard: the reserved-name list tracks deerflow's builtins."""
    from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
    from tenant_db import agent_config

    assert agent_config.BUILTIN_SUBAGENT_NAMES == set(BUILTIN_SUBAGENTS)


def test_load_agent_soul_per_tenant(applied_patches):
    """SOUL.md resolves under the tenant base_dir — the zero-patch lead-agent
    persona path (default agent re-reads it on every agent build)."""
    from deerflow.config.agents_config import load_agent_soul
    from tenant_db import agent_config

    soul = "# 灵魂\n你是一名严谨的中文助理，回答保持简洁。"
    agent_config.write_soul(applied_patches / "tenant-a", soul)

    token = set_tenant_id("tenant-a")
    try:
        assert load_agent_soul(None) == soul
    finally:
        reset_tenant_id(token)

    token = set_tenant_id("tenant-b")
    try:
        assert load_agent_soul(None) is None
    finally:
        reset_tenant_id(token)

    # blank soul ≡ no soul (write_soul removes the file)
    agent_config.write_soul(applied_patches / "tenant-a", "   \n")
    token = set_tenant_id("tenant-a")
    try:
        assert load_agent_soul(None) is None
    finally:
        reset_tenant_id(token)


def test_managed_registry_enabled_toggle_and_isolation(applied_patches):
    """Managed subagents load from the tenant's file backend; ``enabled=false``
    removes them from runtime discovery; other tenants never see them."""
    from deerflow.subagents.registry import _clear_managed_definitions_cache, get_subagent_names
    from tenant_db import agent_config

    root = applied_patches / "tenant-a"
    definition = {
        "name": "catalog-writer",
        "description": "负责撰写目录",
        "system_prompt": "你是一名目录撰写员。",
    }
    agent_config.write_subagent(root, definition)

    token = set_tenant_id("tenant-a")
    try:
        _clear_managed_definitions_cache()
        assert "catalog-writer" in get_subagent_names()

        agent_config.write_subagent(root, {**definition, "enabled": False})
        _clear_managed_definitions_cache()
        assert "catalog-writer" not in get_subagent_names()
    finally:
        reset_tenant_id(token)

    token = set_tenant_id("tenant-b")
    try:
        _clear_managed_definitions_cache()
        assert "catalog-writer" not in get_subagent_names()
    finally:
        reset_tenant_id(token)


def test_enabled_skills_cache_invalidation_on_tenant_file_change(applied_patches):
    """P11: the signature sweep detects admin-side skill writes/toggles and
    calls the upstream cache invalidator; stable files and non-tenant scopes
    never invalidate."""
    from deerflow_patches.patches import tenant_agent_config as pac
    from tenant_db import agent_config

    root = applied_patches / "tenant-a"
    calls: list[int] = []
    pac._SIGNATURES.pop("tenant-a", None)
    pac._CHECKED_AT.pop("tenant-a", None)

    def invalidate() -> None:
        calls.append(1)

    def check() -> None:
        pac._CHECKED_AT.pop("tenant-a", None)  # bypass the 1s TTL guard
        pac._maybe_invalidate_tenant_skills(invalidate)

    # no tenant context → no-op
    pac._maybe_invalidate_tenant_skills(invalidate)
    assert calls == []

    token = set_tenant_id("tenant-a")
    try:
        check()
        assert calls == []  # first observation only records the signature

        check()
        assert calls == []  # unchanged files

        # admin materializes a skill → invalidate
        agent_config.write_skill(root, "demo-skill", "演示技能", "内容")
        agent_config.write_skill_state(root, "demo-skill", True)
        check()
        assert len(calls) == 1

        check()
        assert len(calls) == 1  # stable again

        # enabled toggle (states file rewrite) → invalidate
        agent_config.write_skill_state(root, "demo-skill", False)
        check()
        assert len(calls) == 2

        # skill removal → invalidate
        agent_config.remove_skill(root, "demo-skill")
        check()
        assert len(calls) == 3
    finally:
        reset_tenant_id(token)
        pac._SIGNATURES.pop("tenant-a", None)
        pac._CHECKED_AT.pop("tenant-a", None)

    # platform scope → no-op
    token = set_tenant_id(PLATFORM_TENANT_ID)
    try:
        pac._CHECKED_AT.pop(PLATFORM_TENANT_ID, None)
        pac._maybe_invalidate_tenant_skills(invalidate)
        assert len(calls) == 3
    finally:
        reset_tenant_id(token)
