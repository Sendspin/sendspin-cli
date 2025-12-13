"""Command-line interface for running a Sendspin client."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import platform
import signal
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from zeroconf import ServiceListener

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
from aiosendspin.models.metadata import SessionUpdateMetadata
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
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from sendspin.audio import AudioPlayer
from sendspin.keyboard import keyboard_loop
from sendspin.ui import SendspinUI

logger = logging.getLogger(__name__)


SERVICE_TYPE = "_sendspin-server._tcp.local."
DEFAULT_PATH = "/sendspin"


@dataclass
class CLIState:
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the Sendspin client."""
    parser = argparse.ArgumentParser(description="Run a Sendspin CLI client")
    parser.add_argument(
        "--url",
        default=None,
        help=("WebSocket URL of the Sendspin server. If omitted, discover via mDNS."),
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Friendly name for this client (defaults to hostname)",
    )
    parser.add_argument(
        "--id",
        default=None,
        help="Unique identifier for this client (defaults to sendspin-cli-<hostname>)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use",
    )
    parser.add_argument(
        "--static-delay-ms",
        type=float,
        default=0.0,
        help="Extra playback delay in milliseconds applied after clock sync",
    )
    parser.add_argument(
        "--audio-device",
        type=int,
        default=None,
        help=(
            "Audio output device ID (e.g., 0, 1, 2). "
            "Use --list-audio-devices to see available devices."
        ),
    )
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="List available audio output devices and exit",
    )
    return parser.parse_args(argv)


def _build_service_url(host: str, port: int, properties: dict[bytes, bytes | None]) -> str:
    """Construct WebSocket URL from mDNS service info."""
    path_raw = properties.get(b"path")
    path = path_raw.decode("utf-8", "ignore") if isinstance(path_raw, bytes) else DEFAULT_PATH
    if not path:
        path = DEFAULT_PATH
    if not path.startswith("/"):
        path = "/" + path
    host_fmt = f"[{host}]" if ":" in host else host
    return f"ws://{host_fmt}:{port}{path}"


def _get_device_info() -> DeviceInfo:
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


def list_audio_devices() -> None:
    """List all available audio output devices."""
    try:
        devices = sounddevice.query_devices()
        default_device = sounddevice.default.device[1]  # Output device index

        print("Available audio output devices:")  # noqa: T201
        print("-" * 80)  # noqa: T201
        for i, device in enumerate(devices):
            if device["max_output_channels"] > 0:
                default_marker = " (default)" if i == default_device else ""
                print(  # noqa: T201
                    f"  [{i}] {device['name']}{default_marker}\n"
                    f"       Channels: {device['max_output_channels']}, "
                    f"Sample rate: {device['default_samplerate']} Hz"
                )
    except Exception as e:  # noqa: BLE001
        print(f"Error listing audio devices: {e}")  # noqa: T201
        sys.exit(1)


def resolve_audio_device(device_id: int | None) -> int | None:
    """Validate audio device ID.

    Args:
        device_id: Device ID to validate.

    Returns:
        Device ID if valid, None for default device.

    Raises:
        ValueError: If device_id is invalid.
    """
    if device_id is None:
        return None

    devices = sounddevice.query_devices()
    if 0 <= device_id < len(devices):
        if devices[device_id]["max_output_channels"] > 0:
            return device_id
        raise ValueError(f"Device {device_id} has no output channels")
    raise ValueError(f"Device ID {device_id} out of range (0-{len(devices) - 1})")


class _ServiceDiscoveryListener:
    """Listens for Sendspin server advertisements via mDNS."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._current_url: str | None = None
        self._first_result: asyncio.Future[str] = loop.create_future()
        self.tasks: set[asyncio.Task[None]] = set()

    @property
    def current_url(self) -> str | None:
        """Get the current discovered server URL, or None if no servers."""
        return self._current_url

    async def wait_for_first(self) -> str:
        """Wait for the first server to be discovered."""
        return await self._first_result

    async def _process_service_info(
        self, zeroconf: AsyncZeroconf, service_type: str, name: str
    ) -> None:
        """Extract and construct WebSocket URL from service info."""
        info = await zeroconf.async_get_service_info(service_type, name)
        if info is None or info.port is None:
            return
        addresses = info.parsed_addresses()
        if not addresses:
            return
        host = addresses[0]
        url = _build_service_url(host, info.port, info.properties)
        self._current_url = url

        # Signal first server discovery
        if not self._first_result.done():
            self._first_result.set_result(url)

    def _schedule(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        task = self._loop.create_task(self._process_service_info(zeroconf, service_type, name))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def add_service(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        self._schedule(zeroconf, service_type, name)

    def update_service(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        self._schedule(zeroconf, service_type, name)

    def remove_service(self, _zeroconf: AsyncZeroconf, _service_type: str, _name: str) -> None:
        """Handle service removal (server offline)."""
        self._current_url = None


class ServiceDiscovery:
    """Manages continuous discovery of Sendspin servers via mDNS."""

    def __init__(self) -> None:
        """Initialize the service discovery manager."""
        self._listener: _ServiceDiscoveryListener | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._zeroconf: AsyncZeroconf | None = None

    async def start(self) -> None:
        """Start continuous discovery (keeps running until stop() is called)."""
        loop = asyncio.get_running_loop()
        self._listener = _ServiceDiscoveryListener(loop)
        self._zeroconf = AsyncZeroconf()
        await self._zeroconf.__aenter__()

        try:
            self._browser = AsyncServiceBrowser(
                self._zeroconf.zeroconf, SERVICE_TYPE, cast("ServiceListener", self._listener)
            )
        except Exception:
            await self.stop()
            raise

    async def wait_for_first_server(self) -> str:
        """Wait indefinitely for the first server to be discovered."""
        if self._listener is None:
            raise RuntimeError("Discovery not started. Call start() first.")
        return await self._listener.wait_for_first()

    def current_url(self) -> str | None:
        """Get the current discovered server URL, or None if no servers."""
        return self._listener.current_url if self._listener else None

    async def stop(self) -> None:
        """Stop discovery and clean up resources."""
        if self._browser:
            await self._browser.async_cancel()
            self._browser = None
        if self._zeroconf:
            await self._zeroconf.__aexit__(None, None, None)
            self._zeroconf = None
        self._listener = None


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

    async def handle_error_backoff(self) -> bool:
        """Sleep for error backoff with keyboard interrupt support.

        Returns True if interrupted by keyboard, False if completed normally.
        """
        _print_event(f"Connection error, retrying in {self._error_backoff:.0f}s...")
        return await self.sleep_interruptible(self._error_backoff)

    async def wait_for_server_reappear(self) -> str | None:
        """Wait for server to reappear on the network.

        Returns the new URL if server reappears, None if interrupted.
        """
        logger.info("Server offline, waiting for rediscovery...")
        _print_event("Waiting for server...")

        # Poll for discovery or keyboard exit
        while not self._keyboard_task.done():
            new_url = self._discovery.current_url()
            if new_url:
                return new_url
            await asyncio.sleep(1.0)

        return None


async def _connection_loop(
    client: SendspinClient,
    discovery: ServiceDiscovery,
    audio_handler: AudioStreamHandler,
    initial_url: str,
    keyboard_task: asyncio.Task[None],
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
        ui: Optional UI instance.
    """
    manager = ConnectionManager(discovery, keyboard_task)
    url = initial_url
    manager.set_last_attempted_url(url)

    while not keyboard_task.done():
        try:
            await client.connect(url)
            logger.info("Connected to %s", url)
            _print_event(f"Connected to {url}")
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
            _print_event("Connection lost")
            if ui is not None:
                ui.set_disconnected("Connection lost")

            # Clean up audio state
            await audio_handler.cleanup()

            # Update URL from discovery
            new_url = discovery.current_url()

            # Wait for server to reappear if it's gone
            if not new_url:
                if ui is not None:
                    ui.set_disconnected("Waiting for server...")
                new_url = await manager.wait_for_server_reappear()
                if keyboard_task.done():
                    break

            # Use the discovered URL
            if new_url:
                url = new_url
            _print_event(f"Reconnecting to {url}...")
            if ui is not None:
                ui.set_disconnected(f"Reconnecting to {url}...")

        except (TimeoutError, OSError, ClientError) as e:
            # Network-related errors - log cleanly
            logger.debug(
                "Connection error (%s), retrying in %.0fs",
                type(e).__name__,
                manager.get_error_backoff(),
            )

            if await manager.handle_error_backoff():
                break

            # Check if URL changed while sleeping
            current_url = discovery.current_url()
            new_url, _ = manager.update_backoff_and_url(current_url)
            if new_url:
                url = new_url
        except Exception:
            # Unexpected errors - log with full traceback
            logger.exception("Unexpected error during connection")
            _print_event("Unexpected error occurred")
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

    def on_stream_start(self, _message: StreamStartMessage) -> None:
        """Handle stream start by clearing stale audio chunks."""
        if self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream start")
        _print_event("Stream started")

    def on_stream_end(self, roles: list[Roles] | None) -> None:
        """Handle stream end by clearing audio queue to prevent desync on resume."""
        # For the CLI player, we only care about the player role
        if (roles is None or Roles.PLAYER in roles) and self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream end")
            _print_event("Stream ended")

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


async def main_async(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0915
    """Entry point executing the asynchronous CLI workflow."""
    args = parse_args(list(argv) if argv is not None else None)

    # In interactive mode with UI, suppress logs to avoid interfering with display
    # Only show WARNING and above unless explicitly set to DEBUG
    if sys.stdin.isatty() and args.log_level != "DEBUG":
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(level=getattr(logging, args.log_level))

    # Get hostname for defaults if needed
    if args.id is None or args.name is None:
        hostname = socket.gethostname()
        if not hostname:
            logger.error("Unable to determine hostname. Please specify --id and/or --name")
            return 1
        # Auto-generate client ID and name from hostname
        client_id = args.id if args.id is not None else f"sendspin-cli-{hostname}"
        client_name = args.name if args.name is not None else hostname
    else:
        # Both explicitly provided
        client_id = args.id
        client_name = args.name

    _print_event(f"Using client ID: {client_id}")

    state = CLIState()
    client = SendspinClient(
        client_id=client_id,
        client_name=client_name,
        roles=[Roles.CONTROLLER, Roles.PLAYER, Roles.METADATA],
        device_info=_get_device_info(),
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
        static_delay_ms=args.static_delay_ms,
    )

    # Start service discovery
    discovery = ServiceDiscovery()
    await discovery.start()

    try:
        # Get initial server URL
        url = args.url
        if url is None:
            logger.info("Waiting for mDNS discovery of Sendspin server...")
            _print_event("Searching for Sendspin server...")
            try:
                url = await discovery.wait_for_first_server()
                logger.info("Discovered Sendspin server at %s", url)
                _print_event(f"Found server at {url}")
            except Exception:
                logger.exception("Failed to discover server")
                return 1

        # Resolve audio device if specified
        audio_device = None
        if args.audio_device is not None:
            try:
                audio_device = resolve_audio_device(args.audio_device)
                if audio_device is not None:
                    device_name = sounddevice.query_devices(audio_device)["name"]
                    logger.info("Using audio device %d: %s", audio_device, device_name)
            except ValueError as e:
                logger.error("Audio device error: %s", e)
                return 1

        # Create audio and stream handlers
        audio_handler = AudioStreamHandler(client, audio_device=audio_device)

        # Create UI for interactive mode
        ui: SendspinUI | None = None
        if sys.stdin.isatty():
            ui = SendspinUI()
            _set_ui(ui)
            ui.start()

        try:
            client.set_metadata_listener(
                lambda payload: _handle_metadata_update(state, ui, payload)
            )
            client.set_group_update_listener(
                lambda payload: _handle_group_update(state, ui, payload)
            )
            client.set_controller_state_listener(
                lambda payload: _handle_server_state(state, ui, payload)
            )
            client.set_stream_start_listener(audio_handler.on_stream_start)
            client.set_stream_end_listener(audio_handler.on_stream_end)
            client.set_stream_clear_listener(audio_handler.on_stream_clear)
            client.set_audio_chunk_listener(audio_handler.on_audio_chunk)
            client.set_server_command_listener(
                lambda payload: _handle_server_command(state, audio_handler, client, ui, payload)
            )

            # Audio player will be created when first audio chunk arrives

            # Create and start keyboard task
            keyboard_task = asyncio.create_task(
                keyboard_loop(client, state, audio_handler, ui, _print_event)
            )

            # Set up signal handler for graceful shutdown on Ctrl+C
            loop = asyncio.get_running_loop()

            def signal_handler() -> None:
                logger.debug("Received interrupt signal, shutting down...")
                keyboard_task.cancel()

            # Signal handlers aren't supported on this platform (e.g., Windows)
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal.SIGINT, signal_handler)
                loop.add_signal_handler(signal.SIGTERM, signal_handler)

            try:
                # Run connection loop with auto-reconnect
                await _connection_loop(
                    client, discovery, audio_handler, url, keyboard_task, ui
                )
            except asyncio.CancelledError:
                logger.debug("Connection loop cancelled")
            finally:
                # Remove signal handlers
                # Signal handlers aren't supported on this platform (e.g., Windows)
                with contextlib.suppress(NotImplementedError):
                    loop.remove_signal_handler(signal.SIGINT)
                    loop.remove_signal_handler(signal.SIGTERM)
                await audio_handler.cleanup()
                await client.disconnect()
        finally:
            # Stop UI
            if ui is not None:
                ui.stop()
                _set_ui(None)

    finally:
        # Stop discovery
        await discovery.stop()

    return 0


async def _handle_metadata_update(
    state: CLIState, ui: SendspinUI | None, payload: ServerStatePayload
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
        _print_event(state.describe())


async def _handle_group_update(
    state: CLIState, ui: SendspinUI | None, payload: GroupUpdateServerPayload
) -> None:
    if payload.playback_state:
        state.playback_state = payload.playback_state
        if ui is not None:
            ui.set_playback_state(payload.playback_state)
        _print_event(f"Playback state: {payload.playback_state.value}")
    if payload.group_id:
        _print_event(f"Group ID: {payload.group_id}")
    if payload.group_name:
        _print_event(f"Group name: {payload.group_name}")


async def _handle_server_state(
    state: CLIState, ui: SendspinUI | None, payload: ServerStatePayload
) -> None:
    """Handle server/state messages with controller state."""
    if payload.controller:
        controller = payload.controller
        state.supported_commands = set(controller.supported_commands)

        volume_changed = controller.volume != state.volume
        mute_changed = controller.muted != state.muted

        if volume_changed:
            state.volume = controller.volume
            _print_event(f"Volume: {controller.volume}%")
        if mute_changed:
            state.muted = controller.muted
            _print_event("Muted" if controller.muted else "Unmuted")

        if ui is not None and (volume_changed or mute_changed):
            ui.set_volume(state.volume, muted=state.muted)


async def _handle_server_command(
    state: CLIState,
    audio_handler: AudioStreamHandler,
    client: SendspinClient,
    ui: SendspinUI | None,
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
        _print_event(f"Server set player volume: {player_cmd.volume}%")
    elif player_cmd.command == PlayerCommand.MUTE and player_cmd.mute is not None:
        state.player_muted = player_cmd.mute
        if audio_handler.audio_player is not None:
            audio_handler.audio_player.set_volume(state.player_volume, muted=state.player_muted)
        if ui is not None:
            ui.set_player_volume(state.player_volume, muted=state.player_muted)
        _print_event("Server muted player" if player_cmd.mute else "Server unmuted player")

    # Send state update back to server per spec
    await client.send_player_state(
        state=PlayerStateType.SYNCHRONIZED,
        volume=state.player_volume,
        muted=state.player_muted,
    )


_ui_instance: SendspinUI | None = None


def _set_ui(ui: SendspinUI | None) -> None:
    """Set the global UI instance."""
    global _ui_instance  # noqa: PLW0603
    _ui_instance = ui


def _print_event(message: str) -> None:
    if _ui_instance is not None:
        _ui_instance.add_event(message)
    else:
        print(message, flush=True)  # noqa: T201


def main() -> int:
    """Run the CLI client."""
    # Handle --list-audio-devices before starting async runtime
    args = parse_args(sys.argv[1:])
    if args.list_audio_devices:
        list_audio_devices()
        return 0

    return asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
