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
from sendspin.discovery import ServiceDiscovery
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

    async def run(self) -> int:
        """Run the daemon."""
        logger.info("Starting Sendspin daemon: %s", self._client._client_id)
        url = self._config.url
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

        await self._discovery.start()

        try:
            if url is None:
                logger.info("Waiting for mDNS discovery of Sendspin server...")
                server = await self._discovery.wait_for_server()
                url = server.url

            self._audio_handler.attach_client(self._client)

            await self._connection_loop(url, use_discovery=self._config.url is None)
        except asyncio.CancelledError:
            logger.debug("Daemon cancelled")
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

                if use_discovery:
                    server = await self._discovery.wait_for_server()
                    url = server.url

                logger.info("Reconnecting to %s", url)

            except (TimeoutError, OSError, ClientError) as e:
                logger.warning(
                    "Connection error (%s), retrying in %.0fs",
                    type(e).__name__,
                    error_backoff,
                )

                await asyncio.sleep(error_backoff)

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
