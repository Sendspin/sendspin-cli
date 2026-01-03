"""WebSocket server for accepting incoming connections from Sendspin servers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# Type for connection handler callback
ConnectionHandler = Callable[[web.WebSocketResponse], Coroutine[Any, Any, None]]


class PassiveServer:
    """WebSocket server that accepts incoming connections from Sendspin servers.

    In passive mode, the daemon advertises itself via mDNS and waits for
    Sendspin servers to connect to it, rather than actively discovering
    and connecting to servers.
    """

    def __init__(
        self,
        port: int,
        path: str,
        on_connection: ConnectionHandler,
    ) -> None:
        """Initialize the passive server.

        Args:
            port: Port to listen on.
            path: WebSocket endpoint path (e.g., "/sendspin").
            on_connection: Async callback invoked when a server connects.
                Receives the WebSocket response object.
        """
        self._port = port
        self._path = path
        self._on_connection = on_connection
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """Start the WebSocket server."""
        self._app = web.Application()
        self._app.router.add_get(self._path, self._handle_websocket)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await self._site.start()

        logger.info("Passive server listening on port %d at %s", self._port, self._path)

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.debug("Passive server stopped")

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle incoming WebSocket connection."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        peer = request.remote or "unknown"
        logger.info("Server connected from %s", peer)

        try:
            await self._on_connection(ws)
        except asyncio.CancelledError:
            logger.debug("Connection handler cancelled")
        except Exception:
            logger.exception("Error in connection handler")
        finally:
            if not ws.closed:
                await ws.close()
            logger.info("Server disconnected from %s", peer)

        return ws

    async def __aenter__(self) -> PassiveServer:
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit."""
        await self.stop()
