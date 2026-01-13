"""Daemon mode for running a Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass

from aiohttp import ClientError
from aiosendspin.client import SendspinClient
from aiosendspin_mpris import MPRIS_AVAILABLE, SendspinMpris
from aiohttp import web
from aiosendspin.client import ClientListener
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles

from sendspin.audio import AudioDevice
from sendspin.audio_connector import AudioStreamHandler
from sendspin.utils import get_device_info

logger = logging.getLogger(__name__)


@dataclass
class DaemonConfig:
    """Configuration for the Sendspin daemon."""

    audio_device: AudioDevice
    client_id: str
    client_name: str
    url: str | None = None
    static_delay_ms: float = 0.0
    listen_port: int = 8927


class SendspinDaemon:
    """Sendspin daemon - headless audio player mode.

    When a URL is provided, the daemon connects to that server (client-initiated).
    When no URL is provided, the daemon listens for incoming server connections
    and advertises itself via mDNS (server-initiated connections).
    """

    def __init__(self, config: DaemonConfig) -> None:
        """Initialize the daemon."""
        self._config = config
        self._audio_handler = AudioStreamHandler(audio_device=config.audio_device)
        self._client: SendspinClient | None = None
        self._listener: ClientListener | None = None

    def _create_client(self) -> SendspinClient:
        """Create a new SendspinClient instance."""
        client_roles = [Roles.PLAYER]
        if MPRIS_AVAILABLE:
            client_roles.extend([Roles.METADATA, Roles.CONTROLLER])

        return SendspinClient(
            client_id=self._config.client_id,
            client_name=self._config.client_name,
            roles=client_roles,
            device_info=get_device_info(),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM, channels=2, sample_rate=44_100, bit_depth=16
                    ),
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM, channels=1, sample_rate=44_100, bit_depth=16
                    ),
                ],
                buffer_capacity=32_000_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
            static_delay_ms=self._config.static_delay_ms,
        )

    async def run(self) -> int:
        """Run the daemon."""
        logger.info("Starting Sendspin daemon: %s", self._config.client_id)
        loop = asyncio.get_running_loop()

        # Store reference to current task so it can be cancelled on shutdown
        main_task = asyncio.current_task()
        assert main_task is not None

        def signal_handler() -> None:
            logger.debug("Received interrupt signal, shutting down...")
            main_task.cancel()

        # Register signal handlers
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

        try:
            if self._config.url is not None:
                # Client-initiated connection mode
                await self._run_client_initiated()
            else:
                # Server-initiated connection mode (listen for incoming connections)
                await self._run_server_initiated()
        except asyncio.CancelledError:
            logger.debug("Daemon cancelled")
        finally:
            self._mpris.stop()
            await self._audio_handler.cleanup()
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            if self._listener is not None:
                await self._listener.stop()
                self._listener = None
            logger.info("Daemon stopped")

        return 0

    async def _run_client_initiated(self) -> None:
        """Run in client-initiated mode, connecting to a specific URL."""
        assert self._config.url is not None
        self._client = self._create_client()
        self._mpris = SendspinMpris(self._client)
        self._audio_handler.attach_client(self._client)
        await self._connection_loop(self._config.url)

    async def _run_server_initiated(self) -> None:
        """Run in server-initiated mode, listening for incoming connections."""
        logger.info(
            "Listening for server connections on port %d (mDNS: _sendspin._tcp.local.)",
            self._config.listen_port,
        )

        self._listener = ClientListener(
            client_id=self._config.client_id,
            on_connection=self._handle_server_connection,
            port=self._config.listen_port,
        )
        await self._listener.start()

        # Keep running until cancelled
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    async def _handle_server_connection(self, ws: web.WebSocketResponse) -> None:
        """Handle an incoming server connection."""
        logger.info("Server connected")

        # Clean up any previous client
        if self._client is not None:
            logger.info("Disconnecting from previous server")
            await self._audio_handler.cleanup()
            await self._client.disconnect()

        # Create a new client for this connection
        self._client = self._create_client()
        self._audio_handler.attach_client(self._client)
        self._mpris = SendspinMpris(self._client)

        try:
            await self._client.attach_websocket(ws)

            # Wait for disconnect
            disconnect_event = asyncio.Event()
            unsubscribe = self._client.add_disconnect_listener(disconnect_event.set)
            await disconnect_event.wait()
            unsubscribe()

            logger.info("Server disconnected")
        except TimeoutError:
            logger.warning("Handshake with server timed out")
        except Exception:
            logger.exception("Error during server connection")
        finally:
            await self._audio_handler.cleanup()

    async def _connection_loop(self, url: str) -> None:
        """Run the connection loop with automatic reconnection (client-initiated mode)."""
        assert self._client is not None
        error_backoff = 1.0
        max_backoff = 300.0

        while True:
            try:
                await self._client.connect(url)
                error_backoff = 1.0

                # Wait for disconnect
                disconnect_event: asyncio.Event = asyncio.Event()
                unsubscribe = self._client.add_disconnect_listener(disconnect_event.set)
                await disconnect_event.wait()
                unsubscribe()

                # Connection dropped
                logger.info("Disconnected from server")
                await self._audio_handler.cleanup()

                logger.info("Reconnecting to %s", url)

            except (TimeoutError, OSError, ClientError) as e:
                logger.warning(
                    "Connection error (%s), retrying in %.0fs",
                    type(e).__name__,
                    error_backoff,
                )

                await asyncio.sleep(error_backoff)
                error_backoff = min(error_backoff * 2, max_backoff)

            except Exception:
                logger.exception("Unexpected error during connection")
                break
