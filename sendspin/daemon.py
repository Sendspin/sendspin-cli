"""Daemon mode for running Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from aiosendspin.client import SendspinClient
from aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    PlayerCommand,
    SupportedAudioFormat,
)
from aiosendspin.models.types import (
    AudioCodec,
    Roles,
)

from sendspin.app import (
    AppState,
    ConnectionManager,
    connection_loop,
    get_device_info,
)
from sendspin.audio import AudioDevice
from sendspin.audio_connector import AudioStreamHandler
from sendspin.client_listeners import ClientListenerManager
from sendspin.discovery import ServiceDiscovery
from sendspin.utils import create_task

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
    """Sendspin daemon - headless mode without UI."""

    def __init__(self, config: DaemonConfig) -> None:
        """Initialize the daemon."""
        self._config = config
        self._state = AppState()
        self._client: SendspinClient | None = None
        self._audio_handler: AudioStreamHandler | None = None
        self._discovery: ServiceDiscovery | None = None

    def _print_event(self, message: str) -> None:
        """Print an event message."""
        print(message, flush=True)  # noqa: T201

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

        self._print_event(f"Using client ID: {client_id}")

        self._client = SendspinClient(
            client_id=client_id,
            client_name=client_name,
            roles=[Roles.CONTROLLER, Roles.PLAYER, Roles.METADATA],
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
                self._print_event("Searching for Sendspin server...")
                try:
                    url = await self._discovery.wait_for_first_server()
                    logger.info("Discovered Sendspin server at %s", url)
                    self._print_event(f"Found server at {url}")
                except asyncio.CancelledError:
                    # When KeyboardInterrupt occurs during discovery
                    return 1
                except Exception:
                    logger.exception("Failed to discover server")
                    return 1

            # Log audio device being used
            logger.info(
                "Using audio device %d: %s",
                config.audio_device.index,
                config.audio_device.name,
            )
            self._print_event(f"Using audio device: {config.audio_device.name}")

            listeners = ClientListenerManager()

            self._audio_handler = AudioStreamHandler(audio_device=config.audio_device)
            self._audio_handler.attach_client(self._client, listeners)

            self._setup_listeners(listeners)
            listeners.attach(self._client)
            loop = asyncio.get_running_loop()

            try:
                # Wait forever task for daemon mode - just wait for cancellation
                async def wait_forever() -> None:
                    await asyncio.Event().wait()

                daemon_task = create_task(wait_forever())
                connection_manager = ConnectionManager(self._discovery, daemon_task)

                def signal_handler() -> None:
                    logger.debug("Received interrupt signal, shutting down...")
                    daemon_task.cancel()

                # Signal handlers aren't supported on this platform (e.g., Windows)
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(signal.SIGINT, signal_handler)
                    loop.add_signal_handler(signal.SIGTERM, signal_handler)

                try:
                    # Run connection loop with auto-reconnect
                    await connection_loop(
                        self._client,
                        self._discovery,
                        self._audio_handler,
                        url,
                        daemon_task,
                        self._print_event,
                        connection_manager,
                        ui=None,  # No UI in daemon mode
                    )
                except asyncio.CancelledError:
                    logger.debug("Connection loop cancelled")
                finally:
                    # Remove signal handlers
                    # Signal handlers aren't supported on this platform (e.g., Windows)
                    with contextlib.suppress(NotImplementedError):
                        loop.remove_signal_handler(signal.SIGINT)
                        loop.remove_signal_handler(signal.SIGTERM)
                    await self._audio_handler.cleanup()
                    await self._client.disconnect()

            finally:
                pass  # No additional cleanup needed for inner try block

        finally:
            # Stop discovery
            await self._discovery.stop()

        return 0

    def _setup_listeners(self, listeners: ClientListenerManager) -> None:
        """Set up client event listeners."""
        from sendspin.app import (
            _handle_group_update,
            _handle_metadata_update,
            _handle_server_command,
            _handle_server_state,
        )

        assert self._client is not None
        client = self._client
        loop = asyncio.get_running_loop()

        # Reuse the same listener handlers from app.py, but pass ui=None
        listeners.add_metadata_listener(
            lambda payload: _handle_metadata_update(
                self._state, None, self._print_event, payload
            )
        )
        listeners.add_group_update_listener(
            lambda payload: _handle_group_update(self._state, None, self._print_event, payload)
        )
        listeners.add_controller_state_listener(
            lambda payload: _handle_server_state(self._state, None, self._print_event, payload)
        )
        listeners.add_server_command_listener(
            lambda payload: _handle_server_command(
                self._state, client, None, self._print_event, payload, loop
            )
        )
