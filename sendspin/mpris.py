"""MPRIS D-Bus interface for desktop media control integration (Linux only)."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from sendspin.app import AppState

logger = logging.getLogger(__name__)

# MPRIS is only available on Linux with the optional mpris_server package
MPRIS_AVAILABLE = False

if sys.platform == "linux":
    try:
        from mpris_server.adapters import MprisAdapter
        from mpris_server.base import PlayState
        from mpris_server.events import EventAdapter
        from mpris_server.mpris.metadata import ValidMetadata
        from mpris_server.server import Server

        MPRIS_AVAILABLE = True
    except ImportError:
        pass

if not MPRIS_AVAILABLE:
    # Define a dummy base class when mpris_server is not available
    class _DummyMprisAdapter:
        """Dummy adapter base class when mpris_server is not installed."""

        def __init__(self) -> None:
            pass

    MprisAdapter = _DummyMprisAdapter


class SendspinMprisAdapter(MprisAdapter):  # type: ignore[misc]
    """Adapter bridging Sendspin state to MPRIS interface."""

    def __init__(self, state: AppState, loop: asyncio.AbstractEventLoop) -> None:
        """Initialize the MPRIS adapter.

        Args:
            state: Application state to read from.
            loop: Asyncio event loop for dispatching commands.
        """
        super().__init__()
        self._state = state
        self._loop = loop
        self._command_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None

    def set_command_callback(self, callback: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        """Set async callback for MPRIS commands."""
        self._command_callback = callback

    def get_uri_schemes(self) -> list[str]:
        """Return supported URI schemes."""
        return ["ws", "wss"]

    def get_mime_types(self) -> list[str]:
        """Return supported MIME types."""
        return ["audio/*"]

    def get_desktop_entry(self) -> str:
        """Return desktop entry name."""
        return "sendspin"

    def metadata(self) -> ValidMetadata:
        """Return current track metadata in MPRIS format."""
        duration_us = (self._state.track_duration or 0) * 1000  # ms to microseconds
        return {
            "mpris:trackid": "/org/sendspin/track/current",
            "mpris:length": duration_us,
            "xesam:title": self._state.title or "",
            "xesam:artist": [self._state.artist] if self._state.artist else [],
            "xesam:album": self._state.album or "",
        }

    def get_playstate(self) -> PlayState:
        """Return current playback state."""
        from aiosendspin.models.types import PlaybackStateType

        if self._state.playback_state == PlaybackStateType.PLAYING:
            return PlayState.PLAYING
        if self._state.playback_state == PlaybackStateType.PAUSED:
            return PlayState.PAUSED
        return PlayState.STOPPED

    def get_current_position(self) -> int:
        """Return current track position in microseconds."""
        return (self._state.track_progress or 0) * 1000  # ms to microseconds

    def get_rate(self) -> float:
        """Return playback rate."""
        return 1.0

    def set_rate(self, val: float) -> float:
        """Set playback rate (not supported, return current)."""
        return 1.0

    def get_minimum_rate(self) -> float:
        """Return minimum playback rate."""
        return 1.0

    def get_maximum_rate(self) -> float:
        """Return maximum playback rate."""
        return 1.0

    def get_volume(self) -> float:
        """Return current volume as 0.0-1.0."""
        if self._state.player_muted:
            return 0.0
        return self._state.player_volume / 100.0

    def set_volume(self, val: float) -> None:
        """Set volume from 0.0-1.0 value."""
        volume_int = max(0, min(100, int(val * 100)))
        self._dispatch_command(f"pvol:{volume_int}")

    def is_mute(self) -> bool:
        """Return whether player is muted."""
        return self._state.player_muted

    def set_mute(self, val: bool) -> None:
        """Set mute state."""
        if val != self._state.player_muted:
            self._dispatch_command("pmute")

    def can_control(self) -> bool:
        """Return whether the player can be controlled."""
        return True

    def can_play(self) -> bool:
        """Return whether play is supported."""
        from aiosendspin.models.types import MediaCommand

        return MediaCommand.PLAY in self._state.supported_commands

    def can_pause(self) -> bool:
        """Return whether pause is supported."""
        from aiosendspin.models.types import MediaCommand

        return MediaCommand.PAUSE in self._state.supported_commands

    def can_go_next(self) -> bool:
        """Return whether next track is supported."""
        from aiosendspin.models.types import MediaCommand

        return MediaCommand.NEXT in self._state.supported_commands

    def can_go_previous(self) -> bool:
        """Return whether previous track is supported."""
        from aiosendspin.models.types import MediaCommand

        return MediaCommand.PREVIOUS in self._state.supported_commands

    def can_seek(self) -> bool:
        """Return whether seeking is supported."""
        return False

    def can_quit(self) -> bool:
        """Return whether quit is supported."""
        return True

    def can_raise(self) -> bool:
        """Return whether raise window is supported."""
        return False

    def can_fullscreen(self) -> bool:
        """Return whether fullscreen is supported."""
        return False

    def set_raise(self, val: bool) -> None:
        """Raise the window (no-op for CLI app)."""

    def get_active_playlist(self) -> tuple[bool, tuple[str, str, str]]:
        """Return active playlist info. We don't support playlists."""
        return (False, ("/", "", ""))

    def get_playlist_count(self) -> int:
        """Return number of playlists."""
        return 0

    def get_playlists(
        self, index: int, max_count: int, order: str, reverse: bool
    ) -> list[tuple[str, str, str]]:
        """Return list of playlists."""
        return []

    def activate_playlist(self, playlist_id: str) -> None:
        """Activate a playlist (not supported)."""

    def play(self) -> None:
        """Start playback."""
        self._dispatch_command("play")

    def pause(self) -> None:
        """Pause playback."""
        self._dispatch_command("pause")

    def play_pause(self) -> None:
        """Toggle play/pause."""
        self._dispatch_command("toggle")

    def next(self) -> None:
        """Skip to next track."""
        self._dispatch_command("next")

    def previous(self) -> None:
        """Skip to previous track."""
        self._dispatch_command("previous")

    def stop(self) -> None:
        """Stop playback."""
        self._dispatch_command("stop")

    def seek(self, time: int, track_id: str | None = None) -> None:
        """Seek to position (not supported)."""

    def open_uri(self, uri: str) -> None:
        """Open URI (not supported)."""

    def quit(self) -> None:
        """Quit the player (not supported)."""

    def get_stream_title(self) -> str:
        """Return stream title."""
        return self._state.title or ""

    def _dispatch_command(self, cmd: str) -> None:
        """Dispatch command to async handler via thread-safe mechanism."""
        if self._command_callback is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._command_callback(cmd), self._loop)
        except RuntimeError:
            logger.debug("Failed to dispatch MPRIS command: event loop not available")


class MprisInterface:
    """High-level MPRIS interface for Sendspin.

    Manages the MPRIS server lifecycle and provides methods to update
    the D-Bus properties when application state changes.
    """

    def __init__(self, state: AppState, loop: asyncio.AbstractEventLoop) -> None:
        """Initialize the MPRIS interface.

        Args:
            state: Application state to expose via MPRIS.
            loop: Asyncio event loop for command dispatch.
        """
        self._adapter = SendspinMprisAdapter(state, loop)
        self._server: Server | None = None
        self._event_adapter: EventAdapter | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def set_command_callback(self, callback: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        """Set async callback for handling MPRIS commands.

        The callback receives command strings like:
        - "play", "pause", "toggle", "next", "previous", "stop"
        - "pvol:50" (set player volume to 50%)
        - "pmute" (toggle player mute)
        """
        self._adapter.set_command_callback(callback)

    def start(self) -> None:
        """Start MPRIS service in background thread."""
        if not MPRIS_AVAILABLE:
            logger.debug("MPRIS not available: mpris_server package not installed or not on Linux")
            return

        if self._running:
            return

        self._server = Server(name="Sendspin", adapter=self._adapter)

        self._event_adapter = EventAdapter(
            root=self._server.root,
            player=self._server.player,
            playlists=self._server.playlists,
            tracklist=self._server.tracklist,
        )

        server = self._server

        def run_loop() -> None:
            try:
                server.loop()
            except Exception:
                logger.exception("MPRIS server loop error")

        self._thread = threading.Thread(target=run_loop, daemon=True, name="mpris-server")
        self._thread.start()
        self._running = True
        logger.info("MPRIS interface started")

    def stop(self) -> None:
        """Stop MPRIS service."""
        if not self._running:
            return

        self._running = False
        self._event_adapter = None

        if self._server is not None:
            try:
                self._server.quit()
            except Exception:
                logger.debug("Error stopping MPRIS server", exc_info=True)
            self._server = None

        logger.info("MPRIS interface stopped")

    def update_metadata(self) -> None:
        """Notify MPRIS that track metadata has changed."""
        if self._event_adapter is not None:
            try:
                self._event_adapter.on_title()
            except Exception:
                logger.debug("Failed to emit MPRIS metadata change", exc_info=True)

    def update_playback_state(self) -> None:
        """Notify MPRIS that playback state has changed."""
        if self._event_adapter is not None:
            try:
                self._event_adapter.on_playpause()
            except Exception:
                logger.debug("Failed to emit MPRIS playback state change", exc_info=True)

    def update_volume(self) -> None:
        """Notify MPRIS that volume has changed."""
        if self._event_adapter is not None:
            try:
                self._event_adapter.on_volume()
            except Exception:
                logger.debug("Failed to emit MPRIS volume change", exc_info=True)
