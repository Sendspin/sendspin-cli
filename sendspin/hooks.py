"""Hook execution for external script integration."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


async def run_hook(
    command: str,
    *,
    event: str,
    server_url: str | None = None,
    client_id: str | None = None,
    client_name: str | None = None,
) -> None:
    """Execute a hook command with environment variables.

    Args:
        command: Shell command to execute.
        event: Event type (e.g., "start", "stop").
        server_url: Connected server URL.
        client_id: Client identifier.
        client_name: Client friendly name.
    """
    # Build environment with SENDSPIN_ prefixed variables
    env = os.environ.copy()
    env["SENDSPIN_EVENT"] = event
    if server_url:
        env["SENDSPIN_SERVER_URL"] = server_url
    if client_id:
        env["SENDSPIN_CLIENT_ID"] = client_id
    if client_name:
        env["SENDSPIN_CLIENT_NAME"] = client_name

    logger.debug("Running hook for %s event: %s", event, command)

    try:
        # Use shell=True to allow complex commands like "amixer set Master unmute"
        proc = await asyncio.create_subprocess_shell(
            command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                "Hook command failed (exit %d): %s\nstderr: %s",
                proc.returncode,
                command,
                stderr.decode().strip() if stderr else "(empty)",
            )
        elif stdout or stderr:
            logger.debug(
                "Hook output: stdout=%s stderr=%s",
                stdout.decode().strip() if stdout else "(empty)",
                stderr.decode().strip() if stderr else "(empty)",
            )
    except Exception:
        logger.exception("Failed to execute hook command: %s", command)
