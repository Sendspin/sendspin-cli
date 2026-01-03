"""Daemon mode for running a Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from aiohttp import ClientError, web
from aiosendspin.client import SendspinClient
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles

from sendspin.audio import AudioDevice
from sendspin.audio_connector import AudioStreamHandler
from sendspin.client_listeners import ClientListenerManager
from sendspin.utils import create_task, get_device_info

from .advertisement import AdvertisementConfig, ServiceAdvertisement
from .passive_client import PassiveClient
from .server import PassiveServer

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8927
DEFAULT_PATH = "/sendspin"


@dataclass
class DaemonConfig:
    """Configuration for the Sendspin daemon."""

    audio_device: AudioDevice
    url: str | None = None
    client_id: str | None = None
    client_name: str | None = None
    static_delay_ms: float = 0.0
    port: int = DEFAULT_PORT


def _create_player_support() -> ClientHelloPlayerSupport:
    """Create standard player support configuration."""
    return ClientHelloPlayerSupport(
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
    )


class SendspinDaemon:
    """Sendspin daemon - headless audio player mode.

    The daemon can operate in two modes:
    - Active mode (--url specified): Connects to a specific Sendspin server
    - Passive mode (no URL): Advertises via mDNS and waits for servers to connect
    """

    def __init__(self, config: DaemonConfig) -> None:
        """Initialize the daemon."""
        self._config = config
        self._shutdown_event: asyncio.Event | None = None
        self._client_id: str | None = None
        self._client_name: str | None = None

    async def run(self) -> int:
        """Run the daemon."""
        config = self._config

        # Get hostname for defaults if needed
        client_id = config.client_id
        client_name = config.client_name
        if client_id is None or client_name is None:
            hostname = socket.gethostname()
            if not hostname:
                logger.error("Unable to determine hostname. Please specify --id and/or --name")
                return 1
            # Auto-generate client ID and name from hostname
            if client_id is None:
                client_id = f"sendspin-cli-{hostname}"
            if client_name is None:
                client_name = hostname

        self._client_id = client_id
        self._client_name = client_name

        logger.info("Starting Sendspin daemon: %s", client_id)
        logger.info(
            "Using audio device %d: %s",
            config.audio_device.index,
            config.audio_device.name,
        )

        loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()

        def signal_handler() -> None:
            logger.debug("Received interrupt signal, shutting down...")
            if self._shutdown_event is not None:
                self._shutdown_event.set()

        # Register signal handlers
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

        try:
            if config.url is not None:
                # Active mode: connect to specified server
                return await self._run_active_mode(config.url)
            else:
                # Passive mode: advertise and wait for connections
                return await self._run_passive_mode()
        finally:
            # Remove signal handlers
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            logger.info("Daemon stopped")

    async def _run_active_mode(self, url: str) -> int:
        """Run in active mode - connect to a specific server."""
        assert self._shutdown_event is not None
        assert self._client_id is not None
        assert self._client_name is not None

        client = SendspinClient(
            client_id=self._client_id,
            client_name=self._client_name,
            roles=[Roles.PLAYER],
            device_info=get_device_info(),
            player_support=_create_player_support(),
            static_delay_ms=self._config.static_delay_ms,
        )

        audio_handler = AudioStreamHandler(audio_device=self._config.audio_device)
        listeners = ClientListenerManager()
        audio_handler.attach_client(client, listeners)
        listeners.attach(client)

        try:
            await self._active_connection_loop(client, audio_handler, url)
        finally:
            await audio_handler.cleanup()
            await client.disconnect()

        return 0

    async def _active_connection_loop(
        self,
        client: SendspinClient,
        audio_handler: AudioStreamHandler,
        url: str,
    ) -> None:
        """Run the active connection loop with automatic reconnection."""
        assert self._shutdown_event is not None

        error_backoff = 1.0
        max_backoff = 300.0

        while not self._shutdown_event.is_set():
            try:
                logger.info("Connecting to %s", url)
                await client.connect(url)
                logger.info("Connected to %s", url)
                error_backoff = 1.0

                # Wait for disconnect or shutdown
                disconnect_event = asyncio.Event()
                client.set_disconnect_listener(partial(asyncio.Event.set, disconnect_event))

                shutdown_task = create_task(self._shutdown_event.wait())
                disconnect_task = create_task(disconnect_event.wait())

                done, pending = await asyncio.wait(
                    {shutdown_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                client.set_disconnect_listener(None)

                if shutdown_task in done:
                    break

                # Connection dropped
                logger.info("Disconnected from server")
                await audio_handler.cleanup()
                logger.info("Reconnecting to %s", url)

            except (TimeoutError, OSError, ClientError) as e:
                logger.warning(
                    "Connection error (%s), retrying in %.0fs",
                    type(e).__name__,
                    error_backoff,
                )

                # Interruptible sleep
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=error_backoff)
                    break  # Shutdown requested
                except TimeoutError:
                    pass  # Sleep completed, continue loop

                error_backoff = min(error_backoff * 2, max_backoff)

            except Exception:
                logger.exception("Unexpected error during connection")
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=error_backoff)
                    break
                except TimeoutError:
                    pass
                error_backoff = min(error_backoff * 2, max_backoff)

    async def _run_passive_mode(self) -> int:
        """Run in passive mode - advertise and wait for server connections."""
        assert self._shutdown_event is not None
        assert self._client_id is not None
        assert self._client_name is not None

        config = self._config

        # Start service advertisement
        ad_config = AdvertisementConfig(
            port=config.port,
            path=DEFAULT_PATH,
            name=self._client_name,
        )
        advertisement = ServiceAdvertisement(ad_config)

        # Connection handler for incoming server connections
        async def handle_connection(ws: web.WebSocketResponse) -> None:
            assert self._client_id is not None
            assert self._client_name is not None

            # Create a new client for this connection
            client = PassiveClient(
                client_id=self._client_id,
                client_name=self._client_name,
                roles=[Roles.PLAYER],
                device_info=get_device_info(),
                player_support=_create_player_support(),
                static_delay_ms=config.static_delay_ms,
            )

            audio_handler = AudioStreamHandler(audio_device=config.audio_device)
            listeners = ClientListenerManager()
            audio_handler.attach_client(client, listeners)
            listeners.attach(client)

            try:
                await client.accept(ws)
                logger.info("Server connected and handshake complete")

                # Wait for disconnect or shutdown
                assert self._shutdown_event is not None
                disconnect_event = asyncio.Event()
                client.set_disconnect_listener(partial(asyncio.Event.set, disconnect_event))

                shutdown_task = create_task(self._shutdown_event.wait())
                disconnect_task = create_task(disconnect_event.wait())

                done, pending = await asyncio.wait(
                    {shutdown_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            except Exception:
                logger.exception("Error handling server connection")
            finally:
                await audio_handler.cleanup()
                await client.disconnect()

        # Create passive server
        server = PassiveServer(
            port=config.port,
            path=DEFAULT_PATH,
            on_connection=handle_connection,
        )

        try:
            await advertisement.start()
            await server.start()

            logger.info("Waiting for server connections on port %d...", config.port)

            # Wait for shutdown
            await self._shutdown_event.wait()
        finally:
            await server.stop()
            await advertisement.stop()

        return 0
