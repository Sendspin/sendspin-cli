"""Utility functions for the Sendspin CLI."""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Coroutine
from typing import TypeVar

_T = TypeVar("_T")

# Check if eager_start is supported (Python 3.12+)
_SUPPORTS_EAGER_START = sys.version_info >= (3, 12)


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
    and reducing latency (when supported by the Python version).

    Note: eager_start is only supported in Python 3.12+. On older versions,
    this parameter is ignored and tasks behave normally.

    Args:
        coro: The coroutine to run as a task.
        loop: Optional event loop to use. If None, uses the running loop.
        name: Optional name for the task (for debugging).
        eager_start: Whether to start the task eagerly (default: True).
                     Only used if Python version supports it.

    Returns:
        The created asyncio Task.
    """
    if loop is None:
        loop = asyncio.get_running_loop()

    if _SUPPORTS_EAGER_START and eager_start:
        # Use Task constructor directly - it supports eager_start and schedules automatically
        return asyncio.Task(coro, loop=loop, name=name, eager_start=True)

    return loop.create_task(coro, name=name)
