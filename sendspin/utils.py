"""Utility functions for the Sendspin CLI."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TypeVar

_T = TypeVar("_T")


def create_task(
    coro: Coroutine[None, None, _T],
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    name: str | None = None,
    eager_start: bool = True,
) -> asyncio.Task[_T]:
    """Create an asyncio task with eager_start=True by default.

    This wrapper ensures tasks begin executing immediately rather than
    waiting for the next event loop iteration, improving performance
    and reducing latency.

    Args:
        coro: The coroutine to run as a task.
        loop: Optional event loop to use. If None, uses the running loop.
        name: Optional name for the task (for debugging).
        eager_start: Whether to start the task eagerly (default: True).

    Returns:
        The created asyncio Task.
    """
    if loop is not None:
        return loop.create_task(coro, name=name, eager_start=eager_start)
    return asyncio.create_task(coro, name=name, eager_start=eager_start)
