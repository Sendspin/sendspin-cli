"""Daemon mode for running a Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
from dataclasses import dataclass

from aiosendspin.client import SendspinClient
from aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    PlayerCommand,
    SupportedAudioFormat,
)
from aiosendspin.models.types import AudioCodec, Roles

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
    url: str | None = None
    client_id: str | None = None
    client_name: str | None = None
    static_delay_ms: float = 0.0


class SendspinDaemon:
    """Sendspin daemon - headless audio player mode."""

    def __init__(self, config: DaemonConfig) -> None:
        """Initialize the daemon."""
        self._config = config
        self._client: SendspinClient | None = None
        self._audio_handler: AudioStreamHandler | None = None
        self._discovery: ServiceDiscovery | None = None

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

        logger.info("Starting Sendspin daemon: %s", client_id)

        # Create client with PLAYER role only - daemon just plays audio
        self._client = SendspinClient(
            client_id=client_id,
            client_name=client_name,
            roles=[Roles.PLAYER],  # Only PLAYER role - no metadata, no controller
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

        # Start service discovery
        self._discovery = ServiceDiscovery()
        await self._discovery.start()

        try:
            # Get initial server URL
            url = config.url
            if url is None:
                logger.info("Waiting for mDNS discovery of Sendspin server...")
                try:
                    url = await self._discovery.wait_for_first_server()
                    logger.info("Discovered Sendspin server at %s", url)
                except asyncio.CancelledError:
                    return 1
                except Exception:
                    logger.exception("Failed to discover server")
                    return 1

            logger.info(
                "Using audio device %d: %s",
                config.audio_device.index,
                config.audio_device.name,
            )

            listeners = ClientListenerManager()

            self._audio_handler = AudioStreamHandler(audio_device=config.audio_device)
            self._audio_handler.attach_client(self._client, listeners)

            # No listeners needed - daemon just plays audio, doesn't track state
            listeners.attach(self._client)

            loop = asyncio.get_running_loop()

            # Wait forever task for daemon mode - just wait for cancellation
            async def wait_forever() -> None:
                await asyncio.Event().wait()

            daemon_task = create_task(wait_forever())

            def signal_handler() -> None:
                logger.debug("Received interrupt signal, shutting down...")
                daemon_task.cancel()

            # Register signal handlers
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal.SIGINT, signal_handler)
                loop.add_signal_handler(signal.SIGTERM, signal_handler)

            # Simple connection loop - just connect and wait
            try:
                logger.info("Connecting to %s", url)
                await self._client.connect(url)
                logger.info("Connected successfully")

                # Wait for shutdown signal
                await daemon_task
            except asyncio.CancelledError:
                logger.debug("Daemon shutdown requested")
            except Exception:
                logger.exception("Error during daemon operation")
            finally:
                # Remove signal handlers
                with contextlib.suppress(NotImplementedError):
                    loop.remove_signal_handler(signal.SIGINT)
                    loop.remove_signal_handler(signal.SIGTERM)
                await self._audio_handler.cleanup()
                await self._client.disconnect()
                logger.info("Daemon stopped")

        finally:
            # Stop discovery
            await self._discovery.stop()

        return 0
