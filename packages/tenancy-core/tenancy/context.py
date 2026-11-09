"""Tenant identity context (design doc §2.1).

The tenant id travels in a ContextVar so that monkey-patched DeerFlow
internals can resolve per-tenant paths and registries without threading an
explicit parameter through the whole call stack.

Rules (decision D6):
- ``^[a-z][a-z0-9-]{1,62}$`` — lowercase alphanumeric + hyphen, 2..63 chars.
- Reserved names (resource-directory siblings such as ``skills``) are rejected
  so a tenant can never shadow a resource directory inside the tenant root.
"""

import re
from contextvars import ContextVar, Token

TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")

# Usernames within a tenant (management-plane user registry, JWT ``user``
# claim). Same charset as tenant ids, shorter budget.
USER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# D6: names that would collide with resource-level directories (or platform
# semantics) inside the tenant root. ``_platform`` is reserved for the shared
# platform scope (D7) — startup/shutdown and deployment-level services run
# under it; it can never be a real tenant.
RESERVED_TENANT_IDS = {
    "skills",
    "subagents",
    "agents",
    "extensions",
    "prompts",
    "memory",
    "config",
    "_platform",
    "_trash",
}

# The platform scope used by the lifespan wrapper and deployment-level
# background services. It is reserved (never a tenant) but is a valid
# *context value* for path resolution.
PLATFORM_TENANT_ID = "_platform"

_CURRENT_TENANT: ContextVar[str | None] = ContextVar("current_tenant", default=None)

# Optional user WITHIN the current tenant (management-plane registry; absent
# for tenant-level requests). Bound by the same middleware that binds the
# tenant, reset in the same finally block.
_CURRENT_USER: ContextVar[str | None] = ContextVar("current_user", default=None)

# Role of the current user WITHIN the tenant ("admin" | "user"), from the
# signed JWT ``role`` claim. Tenant-level tokens carry no role — the
# tenant-level identity is the tenant's master identity and is admin-capable
# by definition (permission isolation scopes what *users* may do).
ROLES = frozenset({"admin", "user"})
_CURRENT_ROLE: ContextVar[str | None] = ContextVar("current_role", default=None)


class TenantContextError(RuntimeError):
    """Raised when business code requires a tenant context but none is set."""


def is_valid_tenant_id(t: str | None) -> bool:
    """True iff *t* is a syntactically valid, non-reserved tenant id."""
    return bool(t) and bool(TENANT_ID_RE.fullmatch(t)) and t not in RESERVED_TENANT_IDS


def is_valid_user_name(u: str | None) -> bool:
    """True iff *u* is a syntactically valid user name within a tenant."""
    return bool(u) and bool(USER_NAME_RE.fullmatch(u))


def is_valid_context_tenant_id(t: str | None) -> bool:
    """Like :func:`is_valid_tenant_id` but also accepts the platform scope.

    Only the tenancy layer itself (lifespan wrapper, background dispatch)
    may set the platform scope; request paths always carry a real tenant.
    """
    return t == PLATFORM_TENANT_ID or is_valid_tenant_id(t)


def get_tenant_id() -> str:
    """Business-code entry point: fail-closed when no tenant context is set."""
    t = _CURRENT_TENANT.get()
    if t is None:
        raise TenantContextError("no tenant context")
    return t


def get_tenant_id_optional() -> str | None:
    """Context probe for optional-scope code (startup, whitelisted paths)."""
    return _CURRENT_TENANT.get()


def set_tenant_id(t: str) -> Token:
    """Set the current tenant and return the reset token.

    Callers MUST ``reset_tenant_id(token)`` in a ``finally`` block, in the
    same task that performed the set — a leaked context pollutes the next
    request served by a reused worker (design doc §12).
    """
    if not is_valid_context_tenant_id(t):
        raise TenantContextError(f"invalid tenant id for context: {t!r}")
    return _CURRENT_TENANT.set(t)


def reset_tenant_id(token: Token) -> None:
    """Reset to the pre-set state (restores outer context instead of clearing)."""
    _CURRENT_TENANT.reset(token)


def get_current_user_optional() -> str | None:
    """User within the current tenant, or ``None`` for tenant-level requests."""
    return _CURRENT_USER.get()


def set_current_user(user: str | None) -> Token | None:
    """Bind the current user. ``None`` (tenant level) is a no-op returning ``None``.

    The reset token must be applied in the same task that performed the set —
    same discipline as :func:`set_tenant_id`.
    """
    if user is None:
        return None
    if not is_valid_user_name(user):
        raise TenantContextError(f"invalid user name for context: {user!r}")
    return _CURRENT_USER.set(user)


def reset_current_user(token: Token | None) -> None:
    """Reset the user context (no-op when nothing was set)."""
    if token is not None:
        _CURRENT_USER.reset(token)


def get_current_role() -> str | None:
    """Role of the current user, or ``None`` on tenant-level requests."""
    return _CURRENT_ROLE.get()


def set_current_role(role: str | None) -> Token | None:
    """Bind the current user's role. ``None`` (tenant level) is a no-op.

    The reset token must be applied in the same task that performed the set —
    same discipline as :func:`set_tenant_id`.
    """
    if role is None:
        return None
    if role not in ROLES:
        raise TenantContextError(f"invalid role for context: {role!r}")
    return _CURRENT_ROLE.set(role)


def reset_current_role(token: Token | None) -> None:
    """Reset the role context (no-op when nothing was set)."""
    if token is not None:
        _CURRENT_ROLE.reset(token)


def is_admin_role(role: str | None) -> bool:
    """The single admin-capability policy shared by both planes.

    Tenant-level requests (``role is None``) and explicit ``admin`` users may
    configure agents; ``user``-role requests may only run flows. Kept here so
    the management plane (tenant-admin endpoint gates) and the data plane (the
    P10 synthetic identity's ``system_role``) cannot drift apart.
    """
    return role is None or role == "admin"
