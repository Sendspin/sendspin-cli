"""Daemon mode for running a Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
from dataclasses import dataclass
from functools import partial

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
        self._shutdown_event: asyncio.Event | None = None

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

        self._client = SendspinClient(
            client_id=client_id,
            client_name=client_name,
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

            listeners.attach(self._client)

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
                await self._connection_loop(url)
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

    async def _connection_loop(self, initial_url: str) -> None:
        """Run the connection loop with automatic reconnection."""
        assert self._client is not None
        assert self._discovery is not None
        assert self._audio_handler is not None
        assert self._shutdown_event is not None

        url = initial_url
        error_backoff = 1.0
        max_backoff = 300.0

        while not self._shutdown_event.is_set():
            try:
                logger.info("Connecting to %s", url)
                await self._client.connect(url)
                logger.info("Connected to %s", url)
                error_backoff = 1.0

                # Wait for disconnect or shutdown
                disconnect_event = asyncio.Event()
                self._client.set_disconnect_listener(partial(asyncio.Event.set, disconnect_event))

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

                self._client.set_disconnect_listener(None)

                if shutdown_task in done:
                    break

                # Connection dropped
                logger.info("Disconnected from server")
                await self._audio_handler.cleanup()

                # Try to get new URL from discovery, or use last known URL
                new_url = self._discovery.current_url()
                if new_url:
                    url = new_url

                # Wait for server to reappear if discovery shows nothing
                if not self._discovery.current_url():
                    logger.info("Server offline, waiting for rediscovery...")
                    while not self._shutdown_event.is_set():
                        new_url = self._discovery.current_url()
                        if new_url:
                            url = new_url
                            break
                        await asyncio.sleep(1.0)

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

                # Check if URL changed while sleeping
                new_url = self._discovery.current_url()
                if new_url and new_url != url:
                    logger.info("Server URL changed to %s", new_url)
                    url = new_url
                    error_backoff = 1.0
                else:
                    error_backoff = min(error_backoff * 2, max_backoff)

            except Exception:
                logger.exception("Unexpected error during connection")
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=error_backoff)
                    break
                except TimeoutError:
                    pass
                error_backoff = min(error_backoff * 2, max_backoff)
