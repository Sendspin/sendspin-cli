"""Core application logic for the Sendspin client."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import sys
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiosendspin.models.metadata import SessionUpdateMetadata

from aiohttp import ClientError
from aiosendspin.client import SendspinClient
from aiosendspin.models.core import (
    GroupUpdateServerPayload,
    ServerCommandPayload,
    ServerStatePayload,
)
from aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    PlayerCommandPayload,
    SupportedAudioFormat,
)
from aiosendspin.models.types import (
    AudioCodec,
    MediaCommand,
    PlaybackStateType,
    PlayerCommand,
    PlayerStateType,
    Roles,
    UndefinedField,
)

from sendspin.audio import AudioDevice
from sendspin.audio_connector import AudioStreamHandler
from sendspin.client_listeners import ClientListenerManager
from sendspin.discovery import ServiceDiscovery
from sendspin.tui.keyboard import keyboard_loop
from sendspin.tui.ui import SendspinUI
from sendspin.utils import create_task, get_device_info

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """Holds state mirrored from the server for CLI presentation."""

    playback_state: PlaybackStateType | None = None
    supported_commands: set[MediaCommand] = field(default_factory=set)
    volume: int | None = None
    muted: bool | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_progress: int | None = None
    track_duration: int | None = None
    player_volume: int = 100
    player_muted: bool = False
    group_id: str | None = None

    def update_metadata(self, metadata: SessionUpdateMetadata) -> bool:
        """Merge new metadata into the state and report if anything changed."""
        changed = False
        for attr in ("title", "artist", "album"):
            value = getattr(metadata, attr)
            if isinstance(value, UndefinedField):
                continue
            if getattr(self, attr) != value:
                setattr(self, attr, value)
                changed = True

        # Update progress fields from nested progress object
        if not isinstance(metadata.progress, UndefinedField):
            if metadata.progress is None:
                # Clear progress fields
                if self.track_progress is not None or self.track_duration is not None:
                    self.track_progress = None
                    self.track_duration = None
                    changed = True
            else:
                # Update from nested progress object
                if self.track_progress != metadata.progress.track_progress:
                    self.track_progress = metadata.progress.track_progress
                    changed = True
                if self.track_duration != metadata.progress.track_duration:
                    self.track_duration = metadata.progress.track_duration
                    changed = True

        return changed

    def describe(self) -> str:
        """Return a human-friendly description of the current state."""
        lines: list[str] = []
        if self.title:
            lines.append(f"Now playing: {self.title}")
        if self.artist:
            lines.append(f"Artist: {self.artist}")
        if self.album:
            lines.append(f"Album: {self.album}")
        if self.track_duration:
            progress_s = (self.track_progress or 0) / 1000
            duration_s = self.track_duration / 1000
            lines.append(f"Progress: {progress_s:>5.1f} / {duration_s:>5.1f} s")
        if self.volume is not None:
            vol_line = f"Volume: {self.volume}%"
            if self.muted:
                vol_line += " (muted)"
            lines.append(vol_line)
        if self.playback_state is not None:
            lines.append(f"State: {self.playback_state.value}")
        return "\n".join(lines)


class ConnectionManager:
    """Manages connection state and reconnection logic with exponential backoff."""

    def __init__(
        self,
        discovery: ServiceDiscovery,
        keyboard_task: asyncio.Task[None],
        max_backoff: float = 300.0,
    ) -> None:
        """Initialize the connection manager."""
        self._discovery = discovery
        self._keyboard_task = keyboard_task
        self._error_backoff = 1.0
        self._max_backoff = max_backoff
        self._last_attempted_url = ""
        self._pending_url: str | None = None  # URL set by user for server switch

    def set_pending_url(self, url: str) -> None:
        """Set a pending URL for server switch."""
        self._pending_url = url

    def consume_pending_url(self) -> str | None:
        """Get and clear the pending URL if set."""
        url = self._pending_url
        self._pending_url = None
        return url

    async def sleep_interruptible(self, duration: float) -> bool:
        """Sleep with keyboard interrupt support.

        Returns True if interrupted by keyboard, False if completed normally.
        """
        remaining = duration
        while remaining > 0 and not self._keyboard_task.done():
            await asyncio.sleep(min(0.5, remaining))
            remaining -= 0.5
        return self._keyboard_task.done()

    def set_last_attempted_url(self, url: str) -> None:
        """Record the URL that was last attempted."""
        self._last_attempted_url = url

    def reset_backoff(self) -> None:
        """Reset backoff to initial value after successful connection."""
        self._error_backoff = 1.0

    def should_reset_backoff(self, current_url: str | None) -> bool:
        """Check if URL changed, indicating server came back online."""
        return bool(current_url and current_url != self._last_attempted_url)

    def update_backoff_and_url(self, current_url: str | None) -> tuple[str | None, float]:
        """Update URL and backoff based on discovery.

        Returns (new_url, new_backoff).
        """
        if self.should_reset_backoff(current_url):
            logger.info("Server URL changed to %s, reconnecting immediately", current_url)
            assert current_url is not None
            self._last_attempted_url = current_url
            self._error_backoff = 1.0
            return current_url, 1.0
        self._error_backoff = min(self._error_backoff * 2, self._max_backoff)
        return None, self._error_backoff

    def get_error_backoff(self) -> float:
        """Get the current error backoff duration."""
        return self._error_backoff

    def increase_backoff(self) -> None:
        """Increase the backoff duration for the next retry."""
        self._error_backoff = min(self._error_backoff * 2, self._max_backoff)

    async def handle_error_backoff(self, ui: SendspinUI) -> bool:
        """Sleep for error backoff with keyboard interrupt support.

        Returns True if interrupted by keyboard, False if completed normally.
        """
        ui.add_event(f"Connection error, retrying in {self._error_backoff:.0f}s...")
        return await self.sleep_interruptible(self._error_backoff)

    async def wait_for_server_reappear(self, ui: SendspinUI) -> str | None:
        """Wait for server to reappear on the network.

        Returns the new URL if server reappears, None if interrupted.
        """
        logger.info("Server offline, waiting for rediscovery...")
        ui.add_event("Waiting for server...")

        # Poll for discovery or keyboard exit
        while not self._keyboard_task.done():
            new_url = self._discovery.current_url()
            if new_url:
                return new_url
            await asyncio.sleep(1.0)

        return None


async def connection_loop(  # noqa: PLR0915
    client: SendspinClient,
    discovery: ServiceDiscovery,
    audio_handler: AudioStreamHandler,
    initial_url: str,
    keyboard_task: asyncio.Task[None],
    connection_manager: ConnectionManager,
    ui: SendspinUI,
) -> None:
    """
    Run the connection loop with automatic reconnection on disconnect.

    Connects to the server, waits for disconnect, cleans up, then retries
    only if the server is visible via mDNS. Reconnects immediately when
    server reappears. Uses exponential backoff (up to 5 min) for errors.

    Args:
        client: Sendspin client instance.
        discovery: Service discovery manager.
        audio_handler: Audio stream handler.
        initial_url: Initial server URL.
        keyboard_task: Keyboard input task to monitor.
        connection_manager: Connection manager for reconnection logic.
        ui: UI instance.
    """
    manager = connection_manager
    url = initial_url
    manager.set_last_attempted_url(url)

    while not keyboard_task.done():
        try:
            await client.connect(url)
            logger.info("Connected to %s", url)
            ui.add_event(f"Connected to {url}")
            ui.set_connected(url)
            manager.reset_backoff()
            manager.set_last_attempted_url(url)

            # Wait for disconnect or keyboard exit
            disconnect_event: asyncio.Event = asyncio.Event()
            client.set_disconnect_listener(partial(asyncio.Event.set, disconnect_event))
            done, _ = await asyncio.wait(
                {keyboard_task, create_task(disconnect_event.wait())},
                return_when=asyncio.FIRST_COMPLETED,
            )

            client.set_disconnect_listener(None)
            if keyboard_task in done:
                break

            # Connection dropped
            logger.info("Connection lost")
            ui.add_event("Connection lost")
            ui.set_disconnected("Connection lost")

            # Clean up audio state
            await audio_handler.cleanup()

            # Check for pending URL from server selection first
            pending_url = manager.consume_pending_url()
            if pending_url:
                url = pending_url
                manager.reset_backoff()
                ui.add_event(f"Switching to {url}...")
                ui.set_disconnected(f"Switching to {url}...")
                continue

            # Update URL from discovery
            new_url = discovery.current_url()

            # Wait for server to reappear if it's gone
            if not new_url:
                ui.set_disconnected("Waiting for server...")
                new_url = await manager.wait_for_server_reappear(ui)
                if keyboard_task.done():
                    break

            # Use the discovered URL
            if new_url:
                url = new_url
            ui.add_event(f"Reconnecting to {url}...")
            ui.set_disconnected(f"Reconnecting to {url}...")

        except (TimeoutError, OSError, ClientError) as e:
            # Network-related errors - log cleanly
            logger.debug(
                "Connection error (%s), retrying in %.0fs",
                type(e).__name__,
                manager.get_error_backoff(),
            )

            if await manager.handle_error_backoff(ui):
                break

            # Check if URL changed while sleeping
            current_url = discovery.current_url()
            new_url, _ = manager.update_backoff_and_url(current_url)
            if new_url:
                url = new_url
        except Exception:
            # Unexpected errors - log with full traceback
            logger.exception("Unexpected error during connection")
            ui.add_event("Unexpected error occurred")
            await asyncio.sleep(manager.get_error_backoff())
            manager.increase_backoff()


@dataclass
class AppConfig:
    """Configuration for the Sendspin application."""

    audio_device: AudioDevice
    url: str | None = None
    client_id: str | None = None
    client_name: str | None = None
    static_delay_ms: float = 0.0


class SendspinApp:
    """Main Sendspin application."""

    def __init__(self, config: AppConfig) -> None:
        """Initialize the application."""
        self._config = config
        self._ui: SendspinUI
        self._state = AppState()
        self._client: SendspinClient | None = None
        self._audio_handler: AudioStreamHandler | None = None
        self._discovery: ServiceDiscovery | None = None

    async def run(self) -> int:  # noqa: PLR0915
        """Run the application."""
        config = self._config

        # TUI requires an interactive terminal
        if not sys.stdin.isatty():
            print(  # noqa: T201
                "Error: TUI mode requires an interactive terminal.\n"
                "Use 'sendspin daemon' for non-interactive/background operation."
            )
            return 1

        # In interactive mode with UI, suppress logs to avoid interfering with display
        # Only show WARNING and above unless explicitly set to DEBUG
        if logging.getLogger().level != logging.DEBUG:
            logging.getLogger().setLevel(logging.WARNING)

        # Create and start UI early so add_event works
        self._ui = SendspinUI()
        self._ui.start()

        try:
            return await self._run_with_ui(config)
        finally:
            self._ui.stop()

    async def _run_with_ui(self, config: AppConfig) -> int:
        """Run the application with UI already started."""
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

        self._ui.add_event(f"Using client ID: {client_id}")

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
                self._ui.add_event("Searching for Sendspin server...")
                try:
                    url = await self._discovery.wait_for_first_server()
                    logger.info("Discovered Sendspin server at %s", url)
                    self._ui.add_event(f"Found server at {url}")
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
            self._ui.add_event(f"Using audio device: {config.audio_device.name}")

            listeners = ClientListenerManager()

            self._audio_handler = AudioStreamHandler(audio_device=config.audio_device)
            self._audio_handler.attach_client(self._client, listeners)

            # Set delay now that client is created
            self._ui.set_delay(self._client.static_delay_ms)

            self._setup_listeners(listeners)
            listeners.attach(self._client)
            loop = asyncio.get_running_loop()

            # Forward declaration for on_server_selected closure
            connection_manager: ConnectionManager | None = None

            def get_servers() -> list[tuple[str, str, str, int]]:
                """Get available servers from discovery."""
                if self._discovery is None:
                    return []
                return [(s.name, s.url, s.host, s.port) for s in self._discovery.get_servers()]

            async def on_server_selected(new_url: str) -> None:
                """Handle server selection by triggering reconnect."""
                if connection_manager is None or self._client is None:
                    return
                connection_manager.set_pending_url(new_url)
                # Force disconnect to trigger reconnect with new URL
                await self._client.disconnect()

            # Start keyboard loop for interactive control
            keyboard_task = create_task(
                keyboard_loop(
                    self._client,
                    self._state,
                    self._audio_handler,
                    self._ui,
                    get_servers,
                    on_server_selected,
                )
            )

            connection_manager = ConnectionManager(self._discovery, keyboard_task)

            def signal_handler() -> None:
                logger.debug("Received interrupt signal, shutting down...")
                keyboard_task.cancel()

            # Signal handlers aren't supported on this platform (e.g., Windows)
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal.SIGINT, signal_handler)
                loop.add_signal_handler(signal.SIGTERM, signal_handler)

            # Run connection loop with auto-reconnect
            await connection_loop(
                self._client,
                self._discovery,
                self._audio_handler,
                url,
                keyboard_task,
                connection_manager,
                self._ui,
            )
        except asyncio.CancelledError:
            logger.debug("Connection loop cancelled")
        finally:
            # Remove signal handlers
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            if self._audio_handler is not None:
                await self._audio_handler.cleanup()
            await self._client.disconnect()
            await self._discovery.stop()

            # Show hint if delay was changed during session
            current_delay = self._client.static_delay_ms
            if current_delay != config.static_delay_ms:
                print(  # noqa: T201
                    f"\nDelay changed to {current_delay:.0f}ms. "
                    f"Use '--static-delay-ms {current_delay:.0f}' next time to persist."
                )

        return 0

    def _setup_listeners(self, listeners: ClientListenerManager) -> None:
        """Set up client event listeners."""
        assert self._client is not None
        client = self._client
        loop = asyncio.get_running_loop()

        listeners.add_metadata_listener(
            lambda payload: _handle_metadata_update(self._state, self._ui, payload)
        )
        listeners.add_group_update_listener(
            lambda payload: _handle_group_update(self._state, self._ui, payload)
        )
        listeners.add_controller_state_listener(
            lambda payload: _handle_server_state(self._state, self._ui, payload)
        )
        listeners.add_server_command_listener(
            lambda payload: _handle_server_command(self._state, client, self._ui, payload, loop)
        )


def _handle_metadata_update(
    state: AppState,
    ui: SendspinUI,
    payload: ServerStatePayload,
) -> None:
    """Handle server/state messages with metadata."""
    if payload.metadata is not None and state.update_metadata(payload.metadata):
        ui.set_metadata(
            title=state.title,
            artist=state.artist,
            album=state.album,
        )
        ui.set_progress(state.track_progress, state.track_duration)
        ui.add_event(state.describe())


def _handle_group_update(
    state: AppState,
    ui: SendspinUI,
    payload: GroupUpdateServerPayload,
) -> None:
    """Handle group update messages."""
    # Only clear metadata when actually switching to a different group
    group_changed = payload.group_id is not None and payload.group_id != state.group_id
    if group_changed:
        state.group_id = payload.group_id
        state.title = None
        state.artist = None
        state.album = None
        state.track_progress = None
        state.track_duration = None
        ui.set_metadata(title=None, artist=None, album=None)
        ui.clear_progress()
        ui.add_event(f"Group ID: {payload.group_id}")

    if payload.group_name:
        ui.add_event(f"Group name: {payload.group_name}")
    ui.set_group_name(payload.group_name)
    if payload.playback_state:
        state.playback_state = payload.playback_state
        ui.set_playback_state(payload.playback_state)
        ui.add_event(f"Playback state: {payload.playback_state.value}")


def _handle_server_state(
    state: AppState,
    ui: SendspinUI,
    payload: ServerStatePayload,
) -> None:
    """Handle server/state messages with controller state."""
    if payload.controller:
        controller = payload.controller
        state.supported_commands = set(controller.supported_commands)

        volume_changed = controller.volume != state.volume
        mute_changed = controller.muted != state.muted

        if volume_changed:
            state.volume = controller.volume
            ui.add_event(f"Volume: {controller.volume}%")
        if mute_changed:
            state.muted = controller.muted
            ui.add_event("Muted" if controller.muted else "Unmuted")

        if volume_changed or mute_changed:
            ui.set_volume(state.volume, muted=state.muted)


def _handle_server_command(
    state: AppState,
    client: SendspinClient,
    ui: SendspinUI,
    payload: ServerCommandPayload,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Handle server/command messages for player volume/mute control."""
    if payload.player is None:
        return

    player_cmd: PlayerCommandPayload = payload.player

    if player_cmd.command == PlayerCommand.VOLUME and player_cmd.volume is not None:
        state.player_volume = player_cmd.volume
        ui.set_player_volume(state.player_volume, muted=state.player_muted)
        ui.add_event(f"Server set player volume: {player_cmd.volume}%")
    elif player_cmd.command == PlayerCommand.MUTE and player_cmd.mute is not None:
        state.player_muted = player_cmd.mute
        ui.set_player_volume(state.player_volume, muted=state.player_muted)
        ui.add_event("Server muted player" if player_cmd.mute else "Server unmuted player")

    # Send state update back to server per spec
    create_task(
        client.send_player_state(
            state=PlayerStateType.SYNCHRONIZED,
            volume=state.player_volume,
            muted=state.player_muted,
        ),
        loop=loop,
    )
