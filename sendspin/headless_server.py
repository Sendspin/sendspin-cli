"""WebSocket server for headless mode where servers connect to the client."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web
from aiosendspin.client import SendspinClient
from aiosendspin.models.types import Roles

if TYPE_CHECKING:
    from aiosendspin.models.core import DeviceInfo
    from aiosendspin.models.player import ClientHelloPlayerSupport

logger = logging.getLogger(__name__)


class HeadlessClient(SendspinClient):
    """A SendspinClient that accepts incoming WebSocket connections from servers.

    This is used in headless mode where the server discovers and connects to the client,
    rather than the client discovering and connecting to the server.
    """

    def __init__(
        self,
        client_id: str,
        client_name: str,
        roles: list[Roles],
        device_info: DeviceInfo | None = None,
        player_support: ClientHelloPlayerSupport | None = None,
        static_delay_ms: float = 0.0,
    ) -> None:
        """Initialize the headless client."""
        super().__init__(
            client_id=client_id,
            client_name=client_name,
            roles=roles,
            device_info=device_info,
            player_support=player_support,
            static_delay_ms=static_delay_ms,
        )
        self._incoming_ws: web.WebSocketResponse | None = None

    async def handle_incoming_connection(self, ws: web.WebSocketResponse) -> None:
        """Handle an incoming WebSocket connection from a server.

        Args:
            ws: The WebSocket connection from the server.
        """
        logger.info("Received incoming connection from server")

        # Store the WebSocket
        self._incoming_ws = ws
        self._ws = ws  # Set the internal WebSocket
        self._connected = True

        # Create server hello event
        self._server_hello_event = asyncio.Event()

        # Start reader task
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
            from aiosendspin.models.types import PlayerStateType

            await self.send_player_state(
                state=PlayerStateType.SYNCHRONIZED,
                volume=self._initial_volume,
                muted=self._initial_muted,
            )

        # Start time synchronization
        await self._send_time_message()
        self._time_task = self._loop.create_task(self._time_sync_loop())

        logger.info("Handshake with server complete")

        # Wait for the connection to close
        await self._reader_task


class HeadlessWebSocketServer:
    """WebSocket server for accepting connections from Sendspin servers."""

    def __init__(
        self,
        client: HeadlessClient,
        port: int = 8928,
        path: str = "/sendspin",
    ) -> None:
        """Initialize the WebSocket server.

        Args:
            client: The headless client instance.
            port: Port to listen on.
            path: WebSocket endpoint path.
        """
        self._client = client
        self._port = port
        self._path = path
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._connection_task: asyncio.Task[None] | None = None

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle incoming WebSocket connections.

        Args:
            request: The incoming request.

        Returns:
            WebSocket response.
        """
        logger.debug("Received WebSocket connection request from %s", request.remote)

        # Create WebSocket response
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        # Cancel any existing connection task
        if self._connection_task and not self._connection_task.done():
            logger.info("Cancelling existing connection to accept new one")
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass

        # Handle the connection in a task
        self._connection_task = asyncio.create_task(self._client.handle_incoming_connection(ws))

        try:
            await self._connection_task
        except asyncio.CancelledError:
            logger.debug("Connection task cancelled")
        except Exception:
            logger.exception("Error handling connection")

        return ws

    async def start(self) -> None:
        """Start the WebSocket server."""
        logger.info("Starting headless WebSocket server on port %d", self._port)

        # Create web application
        self._app = web.Application()
        self._app.router.add_get(self._path, self.websocket_handler)

        # Start the server
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)
        await self._site.start()

        logger.info("Headless WebSocket server started on 0.0.0.0:%d%s", self._port, self._path)

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        logger.info("Stopping headless WebSocket server")

        # Cancel connection task
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass

        # Stop the server
        if self._site:
            await self._site.stop()
            self._site = None

        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        self._app = None
