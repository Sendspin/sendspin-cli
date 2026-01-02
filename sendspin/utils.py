"""Utility functions for the Sendspin CLI."""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Coroutine
from typing import TypeVar

_T = TypeVar("_T")

# Check if eager_start is supported (Python 3.12+)
_SUPPORTS_EAGER_START = sys.version_info >= (3, 12) and "eager_start" in inspect.signature(
    asyncio.create_task
).parameters


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
    kwargs = {"name": name} if name is not None else {}

    if _SUPPORTS_EAGER_START:
        kwargs["eager_start"] = eager_start

    if loop is not None:
        return loop.create_task(coro, **kwargs)
    return asyncio.create_task(coro, **kwargs)
