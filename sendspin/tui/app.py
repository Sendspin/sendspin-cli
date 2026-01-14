"""Core application logic for the Sendspin client."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiosendspin.models.metadata import SessionUpdateMetadata

from aiohttp import ClientError
from aiosendspin.client import SendspinClient
from aiosendspin_mpris import MPRIS_AVAILABLE, SendspinMpris
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
from sendspin.discovery import ServiceDiscovery, DiscoveredServer
from sendspin.settings import SettingsManager
from sendspin.tui.keyboard import keyboard_loop
from sendspin.tui.ui import SendspinUI
from sendspin.utils import create_task, get_device_info

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """Holds state mirrored from the server for CLI presentation."""

    selected_server: DiscoveredServer | None = None
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

        # Update simple metadata fields
        for attr in ("title", "artist", "album"):
            value = getattr(metadata, attr)
            if not isinstance(value, UndefinedField) and getattr(self, attr) != value:
                setattr(self, attr, value)
                changed = True

        # Update progress fields from nested progress object
        if isinstance(metadata.progress, UndefinedField):
            return changed

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
        max_backoff: float = 300.0,
    ) -> None:
        """Initialize the connection manager."""
        self._discovery = discovery
        self._error_backoff = 1.0
        self._max_backoff = max_backoff
        self._last_attempted_url = ""
        self._pending_server: DiscoveredServer | None = None  # URL set by user for server switch

    def set_pending_server(self, server: DiscoveredServer) -> None:
        """Set a pending server for server switch."""
        self._pending_server = server

    def consume_pending_server(self) -> DiscoveredServer | None:
        """Get and clear the pending server if set."""
        server = self._pending_server
        self._pending_server = None
        return server

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

    async def handle_error_backoff(self, ui: SendspinUI) -> None:
        """Sleep for error backoff duration."""
        ui.add_event(f"Connection error, retrying in {self._error_backoff:.0f}s...")
        await asyncio.sleep(self._error_backoff)

    async def discover_server(self) -> DiscoveredServer:
        """Wait for server to reappear on the network."""
        return await self._discovery.wait_for_server()


@dataclass
class AppArgs:
    """Configuration for the Sendspin application."""

    audio_device: AudioDevice
    client_id: str
    client_name: str
    settings: SettingsManager
    url: str | None = None
    static_delay_ms: float | None = None
    use_mpris: bool = True


class SendspinApp:
    """Main Sendspin application."""

    def __init__(self, args: AppArgs) -> None:
        """Initialize the application."""
        self._args = args
        self._ui: SendspinUI | None = None

        server: DiscoveredServer | None = None
        if args.url:
            server = DiscoveredServer.from_url("Command-line argument", args.url)

        self._state = AppState(selected_server=server)
        self._client = SendspinClient(
            client_id=args.client_id,
            client_name=args.client_name,
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
            static_delay_ms=0.0,  # Will be set after loading settings
        )

        self._audio_handler: AudioStreamHandler | None = None
        self._settings: SettingsManager | None = None
        self._discovery = ServiceDiscovery()
        self._connection_manager = ConnectionManager(self._discovery)
        self._mpris: SendspinMpris | None = None
        if MPRIS_AVAILABLE and args.use_mpris:
            self._mpris = SendspinMpris(self._client)

    async def run(self) -> int:  # noqa: PLR0915
        """Run the application."""
        args = self._args

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

        # Store reference to current task so it can be cancelled on shutdown
        main_task = asyncio.current_task()
        assert main_task is not None

        def request_shutdown() -> None:
            main_task.cancel()

        try:
            self._settings = self._args.settings
            self._state.player_volume = self._settings.player_volume
            self._state.player_muted = self._settings.player_muted

            # CLI arg overrides settings for static delay
            delay = (
                args.static_delay_ms
                if args.static_delay_ms is not None
                else self._settings.static_delay_ms
            )
            self._client.set_static_delay_ms(delay)

            self._ui = SendspinUI(
                delay,
                player_volume=self._settings.player_volume,
                player_muted=self._settings.player_muted,
            )
            self._ui.start()
            self._ui.add_event(f"Using client ID: {args.client_id}")
            self._ui.add_event(f"Using audio device: {args.audio_device.name}")

            self._audio_handler = AudioStreamHandler(
                audio_device=args.audio_device,
                volume=self._settings.player_volume,
                muted=self._settings.player_muted,
            )

            await self._discovery.start()

            self._client.add_metadata_listener(self._handle_metadata_update)
            self._client.add_group_update_listener(self._handle_group_update)
            self._client.add_controller_state_listener(self._handle_server_state)
            self._client.add_server_command_listener(self._handle_server_command)
            self._audio_handler.attach_client(self._client)

            if self._mpris:
                self._mpris.start()

            # Start keyboard loop for interactive control
            create_task(
                keyboard_loop(
                    self._client,
                    self._state,
                    self._audio_handler,
                    self._ui,
                    self._settings,
                    self._show_server_selector,
                    self._on_server_selected,
                    request_shutdown,
                )
            )

            def signal_handler() -> None:
                logger.debug("Received interrupt signal, shutting down...")
                request_shutdown()

            # Signal handlers aren't supported on this platform (e.g., Windows)
            loop = asyncio.get_running_loop()
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal.SIGINT, signal_handler)
                loop.add_signal_handler(signal.SIGTERM, signal_handler)

            # Get initial server URL
            if args.url:
                pass  # URL provided via CLI - selected_server already set in __init__
            elif self._settings.last_server_url:
                # Try last known server first
                last_url = self._settings.last_server_url
                self._ui.add_event(f"Trying last server at {last_url}...")
                try:
                    await self._client.connect(last_url)
                    # Success - create server entry
                    self._state.selected_server = DiscoveredServer.from_url(
                        "Last connected", last_url
                    )
                    self._ui.add_event(f"Connected to {last_url}")
                    self._ui.set_connected(last_url)
                    self._settings.update(last_server_url=last_url)
                    # Run connection loop starting from wait-for-disconnect
                    await self._connection_loop(already_connected=True)
                    return 0
                except (TimeoutError, OSError, ClientError):
                    # Last server unavailable, fall through to discovery
                    self._ui.add_event("Last server unavailable, searching...")

            # No URL yet - do mDNS discovery
            if self._state.selected_server is None:
                logger.info("Waiting for mDNS discovery of Sendspin server...")
                self._ui.add_event("Searching for Sendspin server...")
                server = await self._connection_manager.discover_server()
                self._state.selected_server = server
                self._ui.add_event(f"Found server at {server.url}")

            # Run connection loop with auto-reconnect
            await self._connection_loop()
        except asyncio.CancelledError:
            logger.debug("Connection loop cancelled")
        finally:
            if self._mpris:
                self._mpris.stop()
            if self._ui:
                self._ui.stop()
            if self._audio_handler:
                await self._audio_handler.cleanup()
            await self._client.disconnect()
            await self._discovery.stop()
            if self._settings:
                await self._settings.flush()

        return 0

    async def _connection_loop(self, *, already_connected: bool = False) -> None:
        """
        Run the connection loop with automatic reconnection on disconnect.

        Connects to the server, waits for disconnect, cleans up, then retries
        only if the server is visible via mDNS. Reconnects immediately when
        server reappears. Uses exponential backoff (up to 5 min) for errors.

        Args:
            already_connected: If True, skip the first connection attempt
                (used when caller already established the connection).
        """
        assert self._state.selected_server
        assert self._audio_handler is not None
        assert self._ui is not None
        manager = self._connection_manager
        ui = self._ui
        client = self._client
        audio_handler = self._audio_handler
        discovery = self._discovery
        url = self._state.selected_server.url
        manager.set_last_attempted_url(url)
        skip_connect = already_connected

        while True:
            try:
                if skip_connect:
                    skip_connect = False
                else:
                    await self._client.connect(url)
                    ui.add_event(f"Connected to {url}")
                    ui.set_connected(url)
                    manager.reset_backoff()
                    manager.set_last_attempted_url(url)
                    if self._settings:
                        self._settings.update(last_server_url=url)

                # Wait for disconnect
                disconnect_event: asyncio.Event = asyncio.Event()
                unsubscribe = client.add_disconnect_listener(disconnect_event.set)
                await disconnect_event.wait()
                unsubscribe()

                # Connection dropped
                logger.info("Connection lost")
                ui.add_event("Connection lost")
                ui.set_disconnected("Connection lost")

                # Clean up audio state
                await audio_handler.cleanup()

                # Check for pending URL from server selection first
                pending_server = manager.consume_pending_server()
                if pending_server:
                    self._state.selected_server = pending_server
                    url = pending_server.url
                    manager.reset_backoff()
                    ui.add_event(f"Switching to {url}...")
                    ui.set_disconnected(f"Switching to {url}...")
                    continue

                # If URL was provided via --url, reconnect directly without mDNS
                if self._args.url:
                    ui.add_event(f"Reconnecting to {url}...")
                    ui.set_disconnected(f"Reconnecting to {url}...")
                    continue

                # Update URL from discovery
                server = servers[0] if (servers := discovery.get_servers()) else None

                # Wait for server to reappear if it's gone
                if not server:
                    ui.set_disconnected("Waiting for server...")
                    logger.info("Server offline, waiting for rediscovery...")
                    ui.add_event("Waiting for server...")

                    server = await manager.discover_server()

                self._state.selected_server = server
                url = server.url
                ui.add_event(f"Reconnecting to {url}...")
                ui.set_disconnected(f"Reconnecting to {url}...")

            except (TimeoutError, OSError, ClientError) as e:
                # Network-related errors - log cleanly
                logger.debug(
                    "Connection error (%s), retrying in %.0fs",
                    type(e).__name__,
                    manager.get_error_backoff(),
                )

                await manager.handle_error_backoff(ui)

                # Check if URL changed while sleeping
                if servers := discovery.get_servers():
                    current_url = servers[0].url
                    new_url, _ = manager.update_backoff_and_url(current_url)
                    if new_url:
                        url = new_url
            except Exception:
                # Unexpected errors - log with full traceback
                logger.exception("Unexpected error")
                break

    def _show_server_selector(self) -> None:
        assert self._ui is not None
        servers = self._discovery.get_servers()
        if self._state.selected_server and self._state.selected_server not in servers:
            servers.insert(0, self._state.selected_server)
        self._ui.show_server_selector(servers)

    async def _on_server_selected(self) -> None:
        """Handle server selection by triggering reconnect."""
        assert self._ui is not None
        server = self._ui.get_selected_server()
        if server is None:
            return

        self._ui.hide_server_selector()
        # Skip reconnection if already connected to this server
        if server == self._state.selected_server:
            return

        self._connection_manager.set_pending_server(server)
        # Force disconnect to trigger reconnect with new URL
        await self._client.disconnect()

    def _handle_metadata_update(self, payload: ServerStatePayload) -> None:
        """Handle server/state messages with metadata."""
        assert self._ui is not None
        state = self._state
        ui = self._ui
        if payload.metadata is not None and state.update_metadata(payload.metadata):
            ui.set_metadata(
                title=state.title,
                artist=state.artist,
                album=state.album,
            )
            ui.set_progress(state.track_progress, state.track_duration)
            ui.add_event(state.describe())

    def _handle_group_update(self, payload: GroupUpdateServerPayload) -> None:
        """Handle group update messages."""
        assert self._ui is not None
        state = self._state
        ui = self._ui
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

    def _handle_server_state(self, payload: ServerStatePayload) -> None:
        """Handle server/state messages with controller state."""
        assert self._ui is not None
        state = self._state
        ui = self._ui
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

    def _handle_server_command(self, payload: ServerCommandPayload) -> None:
        """Handle server/command messages for player volume/mute control."""
        if payload.player is None:
            return

        assert self._settings is not None
        assert self._audio_handler is not None
        assert self._ui is not None
        state = self._state
        ui = self._ui
        player_cmd: PlayerCommandPayload = payload.player

        if player_cmd.command == PlayerCommand.VOLUME and player_cmd.volume is not None:
            state.player_volume = player_cmd.volume
            self._settings.update(player_volume=player_cmd.volume)
            self._audio_handler.set_volume(state.player_volume, muted=state.player_muted)
            ui.set_player_volume(state.player_volume, muted=state.player_muted)
            ui.add_event(f"Server set player volume: {player_cmd.volume}%")
        elif player_cmd.command == PlayerCommand.MUTE and player_cmd.mute is not None:
            state.player_muted = player_cmd.mute
            self._settings.update(player_muted=player_cmd.mute)
            self._audio_handler.set_volume(state.player_volume, muted=state.player_muted)
            ui.set_player_volume(state.player_volume, muted=state.player_muted)
            ui.add_event("Server muted player" if player_cmd.mute else "Server unmuted player")

        # Send state update back to server per spec
        create_task(
            self._client.send_player_state(
                state=PlayerStateType.SYNCHRONIZED,
                volume=state.player_volume,
                muted=state.player_muted,
            )
        )
