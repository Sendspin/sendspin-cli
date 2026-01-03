"""Passive Sendspin client that accepts incoming WebSocket connections."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from aiohttp import web
from aiosendspin.client import SendspinClient
from aiosendspin.models.types import PlayerStateType, Roles

if TYPE_CHECKING:
    from aiohttp import ClientWebSocketResponse

logger = logging.getLogger(__name__)


class PassiveClient(SendspinClient):
    """SendspinClient that accepts incoming WebSocket connections.

    This is used in passive/daemon mode where the client advertises itself
    via mDNS and waits for servers to connect, rather than actively
    connecting to servers.
    """

    async def accept(self, ws: web.WebSocketResponse) -> None:
        """Accept an incoming WebSocket connection from a Sendspin server.

        This is the passive equivalent of connect() - instead of creating
        an outgoing connection, it accepts an already-established incoming
        WebSocket and runs the client protocol over it.

        Args:
            ws: The incoming WebSocket connection from a server.
        """
        if self.connected:
            logger.debug("Already connected")
            return

        # Use the incoming WebSocket
        # Note: web.WebSocketResponse has the same interface as ClientWebSocketResponse
        # for the operations we need (send_str, close, closed, async iteration)
        self._ws = cast("ClientWebSocketResponse", ws)
        self._connected = True
        self._server_hello_event = asyncio.Event()

        logger.info("Accepting connection from server")

        # Start the reader loop
        self._reader_task = self._loop.create_task(self._reader_loop())

        # Send client hello
        await self._send_client_hello()

        # Wait for server hello
        try:
            await asyncio.wait_for(self._server_hello_event.wait(), timeout=10)
        except TimeoutError as err:
            await self.disconnect()
            raise TimeoutError("Timed out waiting for server/hello response") from err

        # Send initial player state if player role is supported
        if Roles.PLAYER in self._roles:
            await self.send_player_state(
                state=PlayerStateType.SYNCHRONIZED,
                volume=self._initial_volume,
                muted=self._initial_muted,
            )

        # Start time synchronization
        await self._send_time_message()
        self._time_task = self._loop.create_task(self._time_sync_loop())

        logger.info("Handshake with server complete")

    async def wait_until_disconnected(self) -> None:
        """Wait until the connection is closed.

        This blocks until the server disconnects or the connection is
        otherwise terminated.
        """
        if self._reader_task is not None:
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
