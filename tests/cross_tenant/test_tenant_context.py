"""Tenant id validity and ContextVar semantics (design doc §2.1, §10)."""

import pytest

from tenancy.context import (
    TenantContextError,
    get_tenant_id,
    get_tenant_id_optional,
    is_valid_tenant_id,
    reset_tenant_id,
    set_tenant_id,
)


def test_tenant_id_path_traversal_rejected():
    assert not is_valid_tenant_id("../../etc")
    assert not is_valid_tenant_id("Tenant_A")  # uppercase/underscore rejected
    assert not is_valid_tenant_id("skills")  # reserved name rejected
    assert not is_valid_tenant_id("_platform")
    assert not is_valid_tenant_id("")
    assert not is_valid_tenant_id(None)
    assert not is_valid_tenant_id("a")  # too short (min 2)
    assert not is_valid_tenant_id("a" * 64)  # too long (max 63)
    assert not is_valid_tenant_id("-abc")  # must start with a letter
    assert not is_valid_tenant_id("1abc")  # must start with a letter
    assert not is_valid_tenant_id("a_b")  # underscore rejected
    assert not is_valid_tenant_id("a.b")  # dot rejected


def test_valid_tenant_ids():
    assert is_valid_tenant_id("ab")
    assert is_valid_tenant_id("tenant-a")
    assert is_valid_tenant_id("a0-9z")
    assert is_valid_tenant_id("x" * 63)


def test_get_tenant_id_fails_closed():
    with pytest.raises(TenantContextError):
        get_tenant_id()
    assert get_tenant_id_optional() is None


def test_set_reset_restores_outer_context():
    outer = set_tenant_id("tenant-a")
    try:
        inner = set_tenant_id("tenant-b")
        assert get_tenant_id() == "tenant-b"
        reset_tenant_id(inner)
        # reset (not set(None)) restores the OUTER value — no accidental wipe
        assert get_tenant_id() == "tenant-a"
    finally:
        reset_tenant_id(outer)
    assert get_tenant_id_optional() is None


def test_set_rejects_invalid_tenant():
    with pytest.raises(TenantContextError):
        set_tenant_id("skills")
    with pytest.raises(TenantContextError):
        set_tenant_id("../escape")
    assert get_tenant_id_optional() is None


def test_platform_scope_is_settable_but_not_a_valid_tenant():
    assert not is_valid_tenant_id("_platform")
    token = set_tenant_id("_platform")  # tenancy layer internal scope only
    try:
        assert get_tenant_id() == "_platform"
    finally:
        reset_tenant_id(token)
