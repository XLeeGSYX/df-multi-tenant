"""Background-task tenant context propagation helpers (design doc §2.3).

ContextVar values are copied into a task at creation time and are invisible
to raw executor threads. These helpers give every spawn style a uniform
"run this inside tenant X" wrapper. All wrappers set/reset in try/finally —
a leaked context pollutes the next request handled by a reused worker
(design doc §12, trap #7).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from tenancy.context import reset_tenant_id, set_tenant_id

T = TypeVar("T")


def tenant_wrapped(fn: Callable[..., T], tenant_id: str) -> Callable[..., T]:
    """Wrap a sync callable so it executes inside *tenant_id*'s context.

    Use for ``loop.run_in_executor`` / thread-pool offloads, which do NOT
    copy the current context (unlike ``asyncio.to_thread``).
    """

    def inner(*args: Any, **kwargs: Any) -> T:
        token = set_tenant_id(tenant_id)
        try:
            return fn(*args, **kwargs)
        finally:
            reset_tenant_id(token)

    return inner


def spawn_in_tenant(coro_factory: Callable[[], Awaitable[T]], tenant_id: str) -> "asyncio.Task[T]":
    """Create an asyncio task that runs *coro_factory* inside *tenant_id*.

    The tenant is bound inside the child task, so the parent's context is
    never mutated (design doc §7: child-task set/reset is naturally safe).
    """

    async def runner() -> T:
        token = set_tenant_id(tenant_id)
        try:
            return await coro_factory()
        finally:
            reset_tenant_id(token)

    return asyncio.create_task(runner())


def call_in_tenant(fn: Callable[..., T], tenant_id: str, *args: Any, **kwargs: Any) -> T:
    """Run a sync callable inline inside *tenant_id*'s context (set/reset)."""
    token = set_tenant_id(tenant_id)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_tenant_id(token)
