"""Utility functions for the Sendspin CLI."""

from __future__ import annotations

import asyncio
import platform
import sys
from collections.abc import Coroutine
from importlib.metadata import version
from pathlib import Path
from typing import Any, TypeVar

from aiosendspin.models.core import DeviceInfo

_T = TypeVar("_T")

# Check if eager_start is supported (Python 3.12+)
_SUPPORTS_EAGER_START = sys.version_info >= (3, 12)

TASKS: set[asyncio.Task[Any]] = set()


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
        task = asyncio.Task(coro, loop=loop, name=name, eager_start=True)
    else:
        task = loop.create_task(coro, name=name)

    if task.done():
        return task

    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    return task


def get_device_info() -> DeviceInfo:
    """Get device information for the client hello message."""
    # Get OS/platform information
    system = platform.system()
    product_name = f"{system}"

    # Try to get more specific product info
    if system == "Linux":
        # Try reading /etc/os-release for distribution info
        try:
            os_release = Path("/etc/os-release")
            if os_release.exists():
                with os_release.open() as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            product_name = line.split("=", 1)[1].strip().strip('"')
                            break
        except (OSError, IndexError):
            pass
    elif system == "Darwin":
        mac_version = platform.mac_ver()[0]
        product_name = f"macOS {mac_version}" if mac_version else "macOS"
    elif system == "Windows":
        try:
            win_ver = platform.win32_ver()
            # Check build number to distinguish Windows 11 (build 22000+) from Windows 10
            if win_ver[0] == "10" and win_ver[1] and int(win_ver[1].split(".")[2]) >= 22000:
                product_name = "Windows 11"
            else:
                product_name = f"Windows {win_ver[0]}"
        except (ValueError, IndexError, AttributeError):
            product_name = f"Windows {platform.release()}"

    # Get software version
    try:
        software_version = f"aiosendspin {version('aiosendspin')}"
    except Exception:  # noqa: BLE001
        software_version = "aiosendspin (unknown version)"

    return DeviceInfo(
        product_name=product_name,
        manufacturer=None,  # Could add manufacturer detection if needed
        software_version=software_version,
    )
