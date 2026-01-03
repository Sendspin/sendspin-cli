"""Daemon mode for running a Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass

from aiohttp import ClientError
from aiosendspin.client import SendspinClient
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles

from sendspin.audio import AudioDevice
from sendspin.audio_connector import AudioStreamHandler
from sendspin.client_listeners import ClientListenerManager
from sendspin.discovery import ServiceDiscovery
from sendspin.utils import create_task, get_device_info

logger = logging.getLogger(__name__)


@dataclass
class DaemonConfig:
    """Configuration for the Sendspin daemon."""

    audio_device: AudioDevice
    client_id: str
    client_name: str
    url: str | None = None
    static_delay_ms: float = 0.0


class SendspinDaemon:
    """Sendspin daemon - headless audio player mode."""

    def __init__(self, config: DaemonConfig) -> None:
        """Initialize the daemon."""
        self._config = config
        self._client = SendspinClient(
            client_id=config.client_id,
            client_name=config.client_name,
            roles=[Roles.PLAYER],
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
            static_delay_ms=config.static_delay_ms,
        )
        self._audio_handler = AudioStreamHandler(audio_device=config.audio_device)
        self._discovery = ServiceDiscovery()
        self._shutdown_event = asyncio.Event()

    async def run(self) -> int:
        """Run the daemon."""
        logger.info("Starting Sendspin daemon: %s", self._client._client_id)
        url = self._config.url
        loop = asyncio.get_running_loop()

        def signal_handler() -> None:
            logger.debug("Received interrupt signal, shutting down...")
            self._shutdown_event.set()

        # Register signal handlers
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

        await self._discovery.start()

        try:
            if url is None:
                logger.info("Waiting for mDNS discovery of Sendspin server...")
                url = await self._discover_server()
                if self._shutdown_event.is_set():
                    return 0

            listeners = ClientListenerManager()
            self._audio_handler.attach_client(self._client, listeners)
            listeners.attach(self._client)

            await self._connection_loop(url, use_discovery=self._config.url is None)

        finally:
            await self._audio_handler.cleanup()
            await self._client.disconnect()
            await self._discovery.stop()
            logger.info("Daemon stopped")

        return 0

    async def _connection_loop(self, initial_url: str, use_discovery: bool) -> None:
        """Run the connection loop with automatic reconnection."""
        url = initial_url
        error_backoff = 1.0
        max_backoff = 300.0
        disconnect_event = asyncio.Event()
        self._client.set_disconnect_listener(disconnect_event.set)

        while not self._shutdown_event.is_set():
            disconnect_event.clear()

            try:
                await self._client.connect(url)
                error_backoff = 1.0
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

                if shutdown_task in done:
                    break

                # Connection dropped
                logger.info("Disconnected from server")
                await self._audio_handler.cleanup()

                if use_discovery:
                    url = await self._discover_server()
                    if self._shutdown_event.is_set():
                        break

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

                # Check if URL changed while sleeping (only when using discovery)
                if use_discovery and (servers := self._discovery.get_servers()):
                    new_url = servers[0].url
                    if new_url and new_url != url:
                        logger.info("Server URL changed to %s", new_url)
                        url = new_url
                        error_backoff = 1.0
                        continue

                error_backoff = min(error_backoff * 2, max_backoff)

            except Exception:
                logger.exception("Unexpected error during connection")
                break

    async def _discover_server(self) -> str:
        """Discover next server to use.

        Returns empty string when shutdown is set.
        """
        next_server_task = create_task(self._discovery.wait_for_server())

        if not next_server_task.done():
            shutdown_task = create_task(self._shutdown_event.wait())
            await asyncio.wait(
                {shutdown_task, next_server_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

        if self._shutdown_event.is_set():
            return ""

        server = await next_server_task
        return server.url
