"""P1 path patches against the installed deerflow (design doc §3.3, §10).

These tests apply the real Phase A patches and assert per-tenant resolution
of the foundation root through deerflow's own path chokepoints. They skip
when deerflow is not importable (tenant-admin-only environments).
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
        # best-effort: leave resolution consistent for other test modules


def test_runtime_home_per_tenant(applied_patches):
    from deerflow.config.runtime_paths import runtime_home

    token = set_tenant_id("tenant-a")
    try:
        assert runtime_home() == applied_patches / "tenant-a"
    finally:
        reset_tenant_id(token)

    token = set_tenant_id("tenant-b")
    try:
        assert runtime_home() == applied_patches / "tenant-b"
    finally:
        reset_tenant_id(token)


def test_paths_base_dir_per_tenant(applied_patches):
    from deerflow.config.paths import get_paths

    paths = get_paths()
    token = set_tenant_id("tenant-a")
    try:
        assert paths.base_dir == applied_patches / "tenant-a"
        assert paths.user_dir("u1") == applied_patches / "tenant-a" / "users" / "u1"
        assert paths.memory_file == applied_patches / "tenant-a" / "memory.json"
    finally:
        reset_tenant_id(token)


def test_explicit_base_dir_pin_still_honored(applied_patches):
    from pathlib import Path

    from deerflow.config.paths import Paths

    # The constructor resolves(); /tmp is a symlink on macOS, so compare
    # against the resolved form.
    pinned_root = str(Path("/tmp/pinned-root").resolve())
    pinned = Paths(base_dir=pinned_root)
    token = set_tenant_id("tenant-a")
    try:
        assert str(pinned.base_dir) == pinned_root
    finally:
        reset_tenant_id(token)


def test_platform_scope_root(applied_patches):
    from deerflow.config.paths import get_paths

    token = set_tenant_id(PLATFORM_TENANT_ID)
    try:
        assert get_paths().base_dir == applied_patches / PLATFORM_TENANT_ID
    finally:
        reset_tenant_id(token)


def test_extensions_config_resolution_per_tenant(applied_patches):
    """P3: tenant extensions_config.json wins; absent → platform fallback."""
    from deerflow.config.extensions_config import ExtensionsConfig

    tenant_file = applied_patches / "tenant-a" / "extensions" / "extensions_config.json"
    tenant_file.parent.mkdir(parents=True, exist_ok=True)
    tenant_file.write_text('{"mcpServers": {}, "skills": {}}', encoding="utf-8")
    try:
        token = set_tenant_id("tenant-a")
        try:
            assert ExtensionsConfig.resolve_config_path() == tenant_file
        finally:
            reset_tenant_id(token)

        token = set_tenant_id("tenant-b")
        try:
            assert ExtensionsConfig.resolve_config_path() != applied_patches / "tenant-b" / "extensions" / "extensions_config.json"
        finally:
            reset_tenant_id(token)
    finally:
        tenant_file.unlink()


def test_auth_disabled_user_per_tenant(applied_patches):
    """P10: synthetic user id is tenant-scoped under auth-disabled mode."""
    from deerflow_patches.patches.auth_user import tenant_user_id

    assert tenant_user_id("tenant-a") == "tenant-tenant-a"
    assert len(tenant_user_id("a" * 63)) <= 64  # String(64) column safety
    assert tenant_user_id("a" * 63) != tenant_user_id("a" * 62 + "b")


def test_auth_disabled_user_per_user_within_tenant(applied_patches):
    """P10 user axis: (tenant, user) pairs map to distinct, bounded ids."""
    from deerflow_patches.patches.auth_user import tenant_user_id

    assert tenant_user_id("tenant-a", "alice") == "tenant-tenant-a-alice"
    assert tenant_user_id("tenant-a", "alice") != tenant_user_id("tenant-a", "bob")
    assert tenant_user_id("tenant-a", "alice") != tenant_user_id("tenant-b", "alice")
    assert tenant_user_id("tenant-a") == tenant_user_id("tenant-a", None)
    # String(64) column safety, including the digest fallback path.
    long_id = tenant_user_id("a" * 63, "b" * 32)
    assert len(long_id) <= 64
    assert long_id != tenant_user_id("a" * 63, "b" * 31)


def test_auth_disabled_user_wrapper(applied_patches):
    """P10 end-to-end: the patched factory returns a tenant-scoped identity."""
    from app.gateway.auth_disabled import AUTH_DISABLED_USER_ID, get_auth_disabled_user
    from deerflow_patches.patches.auth_user import tenant_user_id

    token = set_tenant_id("tenant-a")
    try:
        user = get_auth_disabled_user()
        assert user.id == tenant_user_id("tenant-a")
        assert user.email == "admin@tenant-a.tenant.local"
        assert user.system_role == "admin"  # base attributes survive
    finally:
        reset_tenant_id(token)

    token = set_tenant_id(PLATFORM_TENANT_ID)
    try:
        assert get_auth_disabled_user().id == AUTH_DISABLED_USER_ID
    finally:
        reset_tenant_id(token)

    assert get_auth_disabled_user().id == AUTH_DISABLED_USER_ID  # no context


def test_auth_disabled_user_wrapper_user_scoped(applied_patches):
    """P10 end-to-end: a bound user claim yields a per-user identity."""
    from app.gateway.auth_disabled import get_auth_disabled_user
    from deerflow_patches.patches.auth_user import tenant_user_id
    from tenancy.context import reset_current_user, set_current_user

    tenant_token = set_tenant_id("tenant-a")
    user_token = set_current_user("alice")
    try:
        user = get_auth_disabled_user()
        assert user.id == tenant_user_id("tenant-a", "alice")
        assert user.email == "alice@tenant-a.tenant.local"
        assert user.system_role == "admin"  # base attributes survive
    finally:
        reset_current_user(user_token)
        reset_tenant_id(tenant_token)

    # Tenant level again: no residual user.
    tenant_token = set_tenant_id("tenant-a")
    try:
        assert get_auth_disabled_user().id == tenant_user_id("tenant-a")
    finally:
        reset_tenant_id(tenant_token)


def test_auth_disabled_user_role_axis(applied_patches):
    """P10 role axis: regular users lose the blanket auth-disabled admin role,
    so every upstream ``require_admin_user`` gate (skill/subagent/MCP/channel
    mutations) 403s them; tenant-level and admin-role identities keep it."""
    from app.gateway.auth_disabled import get_auth_disabled_user
    from tenancy.context import reset_current_role, reset_current_user, set_current_role, set_current_user

    tenant_token = set_tenant_id("tenant-a")

    # Tenant-level (no role): admin — unchanged upstream behavior.
    assert get_auth_disabled_user().system_role == "admin"

    # Admin-role user: still admin.
    user_token = set_current_user("alice")
    role_token = set_current_role("admin")
    try:
        assert get_auth_disabled_user().system_role == "admin"
    finally:
        reset_current_role(role_token)
        reset_current_user(user_token)

    # Regular user: demoted — the data-plane permission boundary.
    user_token = set_current_user("bob")
    role_token = set_current_role("user")
    try:
        user = get_auth_disabled_user()
        assert user.id == "tenant-tenant-a-bob"
        assert user.system_role == "user"
    finally:
        reset_current_role(role_token)
        reset_current_user(user_token)

    # No residual role after reset.
    assert get_auth_disabled_user().system_role == "admin"
    reset_tenant_id(tenant_token)


def test_upstream_admin_gate_respects_demoted_role(applied_patches):
    """The demotion flows into deerflow's own ``is_admin_user`` predicate —
    the exact check every admin-gated router uses."""
    from types import SimpleNamespace

    from app.gateway.deps import is_admin_user
    from app.gateway.auth_disabled import get_auth_disabled_user
    from tenancy.context import reset_current_role, reset_current_user, set_current_role, set_current_user

    tenant_token = set_tenant_id("tenant-a")

    async def _check() -> bool:
        request = SimpleNamespace(state=SimpleNamespace(user=get_auth_disabled_user(), auth_source=None))
        return await is_admin_user(request)

    import asyncio

    user_token = set_current_user("bob")
    role_token = set_current_role("user")
    try:
        assert asyncio.run(_check()) is False
    finally:
        reset_current_role(role_token)
        reset_current_user(user_token)

    role_token = set_current_role("admin")
    try:
        assert asyncio.run(_check()) is True
    finally:
        reset_current_role(role_token)

    reset_tenant_id(tenant_token)


def test_patch_integrity_after_application(applied_patches):
    from deerflow_patches.bootstrap import verify_patch_integrity

    verify_patch_integrity()  # raises on any dead patch
