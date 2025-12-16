"""Core application logic for the Sendspin client."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import signal
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiosendspin.models.metadata import SessionUpdateMetadata

import sounddevice
from aiohttp import ClientError
from aiosendspin.client import PCMFormat, SendspinClient
from aiosendspin.models.core import (
    DeviceInfo,
    GroupUpdateServerPayload,
    ServerCommandPayload,
    ServerStatePayload,
    StreamStartMessage,
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

from sendspin.audio import AudioPlayer
from sendspin.discovery import ServiceDiscovery
from sendspin.keyboard import keyboard_loop
from sendspin.ui import SendspinUI

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


def get_device_info() -> DeviceInfo:
    """Get device information for the client hello message."""
    # Get OS/platform information
    system = platform.system()
    product_name = f"{system}"

    # Try to get more specific product info
    if system == "Linux":
        # Try reading /etc/os-release for distribution info
        try:
            os_release = Path("/etc/os-release")
            if os_release.exists():
                with os_release.open() as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            product_name = line.split("=", 1)[1].strip().strip('"')
                            break
        except (OSError, IndexError):
            pass
    elif system == "Darwin":
        mac_version = platform.mac_ver()[0]
        product_name = f"macOS {mac_version}" if mac_version else "macOS"
    elif system == "Windows":
        try:
            win_ver = platform.win32_ver()
            # Check build number to distinguish Windows 11 (build 22000+) from Windows 10
            if win_ver[0] == "10" and win_ver[1] and int(win_ver[1].split(".")[2]) >= 22000:
                product_name = "Windows 11"
            else:
                product_name = f"Windows {win_ver[0]}"
        except (ValueError, IndexError, AttributeError):
            product_name = f"Windows {platform.release()}"

    # Get software version
    try:
        software_version = f"aiosendspin {version('aiosendspin')}"
    except Exception:  # noqa: BLE001
        software_version = "aiosendspin (unknown version)"

    return DeviceInfo(
        product_name=product_name,
        manufacturer=None,  # Could add manufacturer detection if needed
        software_version=software_version,
    )


def resolve_audio_device(device: str | None) -> int | None:
    """Resolve audio device by index or name prefix.

    Args:
        device: Device index (numeric string) or name prefix to match.

    Returns:
        Device index if valid, None for default device.

    Raises:
        ValueError: If device is invalid or not found.
    """
    if device is None:
        return None

    devices = sounddevice.query_devices()

    # If numeric, treat as device index
    if device.isnumeric():
        device_id = int(device)
        if 0 <= device_id < len(devices):
            if devices[device_id]["max_output_channels"] > 0:
                return device_id
            raise ValueError(f"Device {device_id} has no output channels")
        raise ValueError(f"Device index {device_id} out of range (0-{len(devices) - 1})")

    # Otherwise, find first output device whose name starts with the prefix
    for i, dev in enumerate(devices):
        if dev["max_output_channels"] > 0 and dev["name"].startswith(device):
            return i

    raise ValueError(f"No audio output device found matching '{device}'")


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

    async def handle_error_backoff(self, print_event: Callable[[str], None]) -> bool:
        """Sleep for error backoff with keyboard interrupt support.

        Returns True if interrupted by keyboard, False if completed normally.
        """
        print_event(f"Connection error, retrying in {self._error_backoff:.0f}s...")
        return await self.sleep_interruptible(self._error_backoff)

    async def wait_for_server_reappear(self, print_event: Callable[[str], None]) -> str | None:
        """Wait for server to reappear on the network.

        Returns the new URL if server reappears, None if interrupted.
        """
        logger.info("Server offline, waiting for rediscovery...")
        print_event("Waiting for server...")

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
    print_event: Callable[[str], None],
    connection_manager: ConnectionManager,
    ui: SendspinUI | None = None,
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
        print_event: Function to print events.
        connection_manager: Connection manager for reconnection logic.
        ui: Optional UI instance.
    """
    manager = connection_manager
    url = initial_url
    manager.set_last_attempted_url(url)

    while not keyboard_task.done():
        try:
            await client.connect(url)
            logger.info("Connected to %s", url)
            print_event(f"Connected to {url}")
            if ui is not None:
                ui.set_connected(url)
            manager.reset_backoff()
            manager.set_last_attempted_url(url)

            # Wait for disconnect or keyboard exit
            disconnect_event: asyncio.Event = asyncio.Event()
            client.set_disconnect_listener(partial(asyncio.Event.set, disconnect_event))
            done, _ = await asyncio.wait(
                {keyboard_task, asyncio.create_task(disconnect_event.wait())},
                return_when=asyncio.FIRST_COMPLETED,
            )

            client.set_disconnect_listener(None)
            if keyboard_task in done:
                break

            # Connection dropped
            logger.info("Connection lost")
            print_event("Connection lost")
            if ui is not None:
                ui.set_disconnected("Connection lost")

            # Clean up audio state
            await audio_handler.cleanup()

            # Check for pending URL from server selection first
            pending_url = manager.consume_pending_url()
            if pending_url:
                url = pending_url
                manager.reset_backoff()
                print_event(f"Switching to {url}...")
                if ui is not None:
                    ui.set_disconnected(f"Switching to {url}...")
                continue

            # Update URL from discovery
            new_url = discovery.current_url()

            # Wait for server to reappear if it's gone
            if not new_url:
                if ui is not None:
                    ui.set_disconnected("Waiting for server...")
                new_url = await manager.wait_for_server_reappear(print_event)
                if keyboard_task.done():
                    break

            # Use the discovered URL
            if new_url:
                url = new_url
            print_event(f"Reconnecting to {url}...")
            if ui is not None:
                ui.set_disconnected(f"Reconnecting to {url}...")

        except (TimeoutError, OSError, ClientError) as e:
            # Network-related errors - log cleanly
            logger.debug(
                "Connection error (%s), retrying in %.0fs",
                type(e).__name__,
                manager.get_error_backoff(),
            )

            if await manager.handle_error_backoff(print_event):
                break

            # Check if URL changed while sleeping
            current_url = discovery.current_url()
            new_url, _ = manager.update_backoff_and_url(current_url)
            if new_url:
                url = new_url
        except Exception:
            # Unexpected errors - log with full traceback
            logger.exception("Unexpected error during connection")
            print_event("Unexpected error occurred")
            await asyncio.sleep(manager.get_error_backoff())
            manager.increase_backoff()


class AudioStreamHandler:
    """Manages audio playback state and stream lifecycle."""

    def __init__(self, client: SendspinClient, audio_device: int | None = None) -> None:
        """Initialize the audio stream handler.

        Args:
            client: The Sendspin client instance.
            audio_device: Audio device ID to use. None for default device.
        """
        self._client = client
        self._audio_device = audio_device
        self.audio_player: AudioPlayer | None = None
        self._current_format: PCMFormat | None = None

    def on_audio_chunk(self, server_timestamp_us: int, audio_data: bytes, fmt: PCMFormat) -> None:
        """Handle incoming audio chunks."""
        # Initialize or reconfigure audio player if format changed
        if self.audio_player is None or self._current_format != fmt:
            if self.audio_player is not None:
                self.audio_player.clear()

            loop = asyncio.get_running_loop()
            self.audio_player = AudioPlayer(
                loop, self._client.compute_play_time, self._client.compute_server_time
            )
            self.audio_player.set_format(fmt, device=self._audio_device)
            self._current_format = fmt

        # Submit audio chunk - AudioPlayer handles timing
        if self.audio_player is not None:
            self.audio_player.submit(server_timestamp_us, audio_data)

    def on_stream_start(
        self, _message: StreamStartMessage, print_event: Callable[[str], None]
    ) -> None:
        """Handle stream start by clearing stale audio chunks."""
        if self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream start")
        print_event("Stream started")

    def on_stream_end(self, roles: list[Roles] | None, print_event: Callable[[str], None]) -> None:
        """Handle stream end by clearing audio queue to prevent desync on resume."""
        # For the CLI player, we only care about the player role
        if (roles is None or Roles.PLAYER in roles) and self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream end")
            print_event("Stream ended")

    def on_stream_clear(self, roles: list[Roles] | None) -> None:
        """Handle stream clear by clearing audio queue (e.g., for seek operations)."""
        # For the CLI player, we only care about the player role
        if (roles is None or Roles.PLAYER in roles) and self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream clear")

    def clear_queue(self) -> None:
        """Clear the audio queue to prevent desync."""
        if self.audio_player is not None:
            self.audio_player.clear()

    async def cleanup(self) -> None:
        """Stop audio player and clear resources."""
        if self.audio_player is not None:
            await self.audio_player.stop()
            self.audio_player = None


@dataclass
class AppConfig:
    """Configuration for the Sendspin application."""

    url: str | None = None
    client_id: str | None = None
    client_name: str | None = None
    static_delay_ms: float = 0.0
    audio_device: str | None = None
    log_level: str = "INFO"
    headless: bool = False


class SendspinApp:
    """Main Sendspin application."""

    def __init__(self, config: AppConfig) -> None:
        """Initialize the application."""
        self._config = config
        self._ui: SendspinUI | None = None
        self._state = AppState()
        self._client: SendspinClient | None = None
        self._audio_handler: AudioStreamHandler | None = None
        self._discovery: ServiceDiscovery | None = None

    def _print_event(self, message: str) -> None:
        """Print an event message."""
        if self._ui is not None:
            self._ui.add_event(message)
        else:
            print(message, flush=True)  # noqa: T201

    async def run(self) -> int:  # noqa: PLR0915
        """Run the application."""
        config = self._config

        # In interactive mode with UI, suppress logs to avoid interfering with display
        # Only show WARNING and above unless explicitly set to DEBUG
        if sys.stdin.isatty() and config.log_level != "DEBUG":
            logging.basicConfig(level=logging.WARNING)
        else:
            logging.basicConfig(level=getattr(logging, config.log_level))

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

            # Resolve audio device if specified
            audio_device = None
            if config.audio_device is not None:
                try:
                    audio_device = resolve_audio_device(config.audio_device)
                    if audio_device is not None:
                        device_name = sounddevice.query_devices(audio_device)["name"]
                        logger.info("Using audio device %d: %s", audio_device, device_name)
                        self._print_event(f"Using audio device: {device_name}")
                except ValueError as e:
                    logger.error("Audio device error: %s", e)
                    return 1
            else:
                # Print default device
                default_device = sounddevice.default.device[1]
                device_name = sounddevice.query_devices(default_device)["name"]
                self._print_event(f"Using audio device: {device_name}")

            # Create audio and stream handlers
            self._audio_handler = AudioStreamHandler(self._client, audio_device=audio_device)

            # Create UI for interactive mode (unless headless)
            if sys.stdin.isatty() and not config.headless:
                self._ui = SendspinUI()
                self._ui.start()
                self._ui.set_delay(self._client.static_delay_ms)

            try:
                self._setup_listeners()

                # Audio player will be created when first audio chunk arrives

                # Set up signal handler for graceful shutdown on Ctrl+C
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

                async def wait_forever() -> None:
                    await asyncio.Event().wait()

                if config.headless:
                    # In headless mode, just wait for cancellation
                    keyboard_task = asyncio.create_task(wait_forever())
                else:
                    keyboard_task = asyncio.create_task(
                        keyboard_loop(
                            self._client,
                            self._state,
                            self._audio_handler,
                            self._ui,
                            self._print_event,
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

                try:
                    # Run connection loop with auto-reconnect
                    await connection_loop(
                        self._client,
                        self._discovery,
                        self._audio_handler,
                        url,
                        keyboard_task,
                        self._print_event,
                        connection_manager,
                        self._ui,
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
                # Stop UI
                if self._ui is not None:
                    self._ui.stop()

                # Show hint if delay was changed during session
                current_delay = self._client.static_delay_ms
                if current_delay != config.static_delay_ms:
                    print(  # noqa: T201
                        f"\nDelay changed to {current_delay:.0f}ms. "
                        f"Use '--static-delay-ms {current_delay:.0f}' next time to persist."
                    )

        finally:
            # Stop discovery
            await self._discovery.stop()

        return 0

    def _setup_listeners(self) -> None:
        """Set up client event listeners."""
        assert self._client is not None
        assert self._audio_handler is not None

        # Capture references for use in lambdas (type narrowing)
        client = self._client
        audio_handler = self._audio_handler

        client.set_metadata_listener(
            lambda payload: _handle_metadata_update(
                self._state, self._ui, self._print_event, payload
            )
        )
        client.set_group_update_listener(
            lambda payload: _handle_group_update(self._state, self._ui, self._print_event, payload)
        )
        client.set_controller_state_listener(
            lambda payload: _handle_server_state(self._state, self._ui, self._print_event, payload)
        )
        client.set_stream_start_listener(
            lambda msg: audio_handler.on_stream_start(msg, self._print_event)
        )
        client.set_stream_end_listener(
            lambda roles: audio_handler.on_stream_end(roles, self._print_event)
        )
        client.set_stream_clear_listener(audio_handler.on_stream_clear)
        client.set_audio_chunk_listener(audio_handler.on_audio_chunk)
        client.set_server_command_listener(
            lambda payload: _handle_server_command(
                self._state, audio_handler, client, self._ui, self._print_event, payload
            )
        )


async def _handle_metadata_update(
    state: AppState,
    ui: SendspinUI | None,
    print_event: Callable[[str], None],
    payload: ServerStatePayload,
) -> None:
    """Handle server/state messages with metadata."""
    if payload.metadata is not None and state.update_metadata(payload.metadata):
        if ui is not None:
            ui.set_metadata(
                title=state.title,
                artist=state.artist,
                album=state.album,
            )
            ui.set_progress(state.track_progress, state.track_duration)
        print_event(state.describe())


async def _handle_group_update(
    state: AppState,
    ui: SendspinUI | None,
    print_event: Callable[[str], None],
    payload: GroupUpdateServerPayload,
) -> None:
    # Only clear metadata when actually switching to a different group
    group_changed = payload.group_id is not None and payload.group_id != state.group_id
    if group_changed:
        state.group_id = payload.group_id
        state.title = None
        state.artist = None
        state.album = None
        state.track_progress = None
        state.track_duration = None
        if ui is not None:
            ui.set_metadata(title=None, artist=None, album=None)
            ui.clear_progress()
        print_event(f"Group ID: {payload.group_id}")

    if payload.group_name:
        print_event(f"Group name: {payload.group_name}")
    if ui is not None:
        ui.set_group_name(payload.group_name)
    if payload.playback_state:
        state.playback_state = payload.playback_state
        if ui is not None:
            ui.set_playback_state(payload.playback_state)
        print_event(f"Playback state: {payload.playback_state.value}")


async def _handle_server_state(
    state: AppState,
    ui: SendspinUI | None,
    print_event: Callable[[str], None],
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
            print_event(f"Volume: {controller.volume}%")
        if mute_changed:
            state.muted = controller.muted
            print_event("Muted" if controller.muted else "Unmuted")

        if ui is not None and (volume_changed or mute_changed):
            ui.set_volume(state.volume, muted=state.muted)


async def _handle_server_command(
    state: AppState,
    audio_handler: AudioStreamHandler,
    client: SendspinClient,
    ui: SendspinUI | None,
    print_event: Callable[[str], None],
    payload: ServerCommandPayload,
) -> None:
    """Handle server/command messages for player volume/mute control."""
    if payload.player is None:
        return

    player_cmd: PlayerCommandPayload = payload.player

    if player_cmd.command == PlayerCommand.VOLUME and player_cmd.volume is not None:
        state.player_volume = player_cmd.volume
        if audio_handler.audio_player is not None:
            audio_handler.audio_player.set_volume(state.player_volume, muted=state.player_muted)
        if ui is not None:
            ui.set_player_volume(state.player_volume, muted=state.player_muted)
        print_event(f"Server set player volume: {player_cmd.volume}%")
    elif player_cmd.command == PlayerCommand.MUTE and player_cmd.mute is not None:
        state.player_muted = player_cmd.mute
        if audio_handler.audio_player is not None:
            audio_handler.audio_player.set_volume(state.player_volume, muted=state.player_muted)
        if ui is not None:
            ui.set_player_volume(state.player_volume, muted=state.player_muted)
        print_event("Server muted player" if player_cmd.mute else "Server unmuted player")

    # Send state update back to server per spec
    await client.send_player_state(
        state=PlayerStateType.SYNCHRONIZED,
        volume=state.player_volume,
        muted=state.player_muted,
    )
