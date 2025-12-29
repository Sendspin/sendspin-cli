"""Keyboard input handling for the Sendspin CLI."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import readchar
from aiosendspin.models.types import MediaCommand, PlaybackStateType, PlayerStateType

from sendspin.ui import DiscoveredServerInfo

if TYPE_CHECKING:
    from aiosendspin.client import SendspinClient

    from sendspin.app import AppState
    from sendspin.audio_connector import AudioStreamHandler
    from sendspin.ui import SendspinUI

logger = logging.getLogger(__name__)


class CommandHandler:
    """Handles keyboard commands."""

    def __init__(
        self,
        client: SendspinClient,
        state: AppState,
        audio_handler: AudioStreamHandler,
        ui: SendspinUI | None = None,
        print_event: Callable[[str], None] | None = None,
        get_servers: Callable[[], list[tuple[str, str, str, int]]] | None = None,
        on_server_selected: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize the command handler."""
        self._client = client
        self._state = state
        self._audio_handler = audio_handler
        self._ui = ui
        self._print_event = print_event or (lambda _: None)
        self._get_servers = get_servers
        self._on_server_selected = on_server_selected

    async def send_media_command(self, command: MediaCommand) -> None:
        """Send a media command with validation."""
        if command not in self._state.supported_commands:
            self._print_event(f"Server does not support {command.value}")
            return
        await self._client.send_group_command(command)

    async def toggle_play_pause(self) -> None:
        """Toggle between play and pause."""
        if self._state.playback_state == PlaybackStateType.PLAYING:
            await self.send_media_command(MediaCommand.PAUSE)
        else:
            await self.send_media_command(MediaCommand.PLAY)

    async def change_player_volume(self, delta: int) -> None:
        """Adjust player (local) volume by delta."""
        target = max(0, min(100, self._state.player_volume + delta))
        self._state.player_volume = target
        # Apply volume to audio player
        if self._audio_handler.audio_player is not None:
            self._audio_handler.audio_player.set_volume(
                self._state.player_volume, muted=self._state.player_muted
            )
        if self._ui is not None:
            self._ui.set_player_volume(self._state.player_volume, muted=self._state.player_muted)
        await self._client.send_player_state(
            state=PlayerStateType.SYNCHRONIZED,
            volume=self._state.player_volume,
            muted=self._state.player_muted,
        )
        self._print_event(f"Player volume: {target}%")

    async def toggle_player_mute(self) -> None:
        """Toggle player (local) mute state."""
        self._state.player_muted = not self._state.player_muted
        # Apply mute to audio player
        if self._audio_handler.audio_player is not None:
            self._audio_handler.audio_player.set_volume(
                self._state.player_volume, muted=self._state.player_muted
            )
        if self._ui is not None:
            self._ui.set_player_volume(self._state.player_volume, muted=self._state.player_muted)
        await self._client.send_player_state(
            state=PlayerStateType.SYNCHRONIZED,
            volume=self._state.player_volume,
            muted=self._state.player_muted,
        )
        self._print_event("Player muted" if self._state.player_muted else "Player unmuted")

    async def adjust_delay(self, delta: float) -> None:
        """Adjust static delay by delta milliseconds."""
        self._client.set_static_delay_ms(self._client.static_delay_ms + delta)
        if self._ui is not None:
            self._ui.set_delay(self._client.static_delay_ms)

    def open_server_selector(self) -> None:
        """Open the server selector panel."""
        if self._ui is None or self._get_servers is None:
            return
        server_tuples = self._get_servers()
        servers = [
            DiscoveredServerInfo(name=name, url=url, host=host, port=port)
            for name, url, host, port in server_tuples
        ]
        self._ui.show_server_selector(servers)

    def close_server_selector(self) -> None:
        """Close the server selector panel."""
        if self._ui is not None:
            self._ui.hide_server_selector()

    async def select_server(self) -> None:
        """Select the highlighted server and connect to it."""
        if self._ui is None or self._on_server_selected is None:
            return
        server = self._ui.get_selected_server()
        if server is not None:
            self._ui.hide_server_selector()
            # Skip reconnection if already connected to this server
            if server.url != self._ui.state.server_url:
                await self._on_server_selected(server.url)


async def keyboard_loop(
    client: SendspinClient,
    state: AppState,
    audio_handler: AudioStreamHandler,
    ui: SendspinUI | None = None,
    print_event: Callable[[str], None] | None = None,
    get_servers: Callable[[], list[tuple[str, str, str, int]]] | None = None,
    on_server_selected: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Run the keyboard input loop.

    Args:
        client: Sendspin client instance.
        state: Application state.
        audio_handler: Audio stream handler.
        ui: Optional UI instance.
        print_event: Function to print events.
        get_servers: Function that returns list of (name, url, host, port) tuples.
        on_server_selected: Async callback when a server is selected (receives URL).
    """
    handler = CommandHandler(
        client, state, audio_handler, ui, print_event, get_servers, on_server_selected
    )

    # Key dispatch table: key -> (highlight_name | None, async action)
    # For keys that need case-insensitive matching, use lowercase
    shortcuts: dict[str, tuple[str | None, Callable[[], Awaitable[None]]]] = {
        # Letter keys
        " ": ("space", handler.toggle_play_pause),
        "m": ("mute", handler.toggle_player_mute),
        "g": ("switch", lambda: handler.send_media_command(MediaCommand.SWITCH)),
        # Delay adjustment
        "[": ("delay-", lambda: handler.adjust_delay(-10)),
        "]": ("delay+", lambda: handler.adjust_delay(10)),
        # Arrow keys
        readchar.key.LEFT: (
            "prev",
            lambda: handler.send_media_command(MediaCommand.PREVIOUS),
        ),
        readchar.key.RIGHT: (
            "next",
            lambda: handler.send_media_command(MediaCommand.NEXT),
        ),
        readchar.key.UP: ("up", lambda: handler.change_player_volume(5)),
        readchar.key.DOWN: ("down", lambda: handler.change_player_volume(-5)),
    }

    if not sys.stdin.isatty():
        logger.info("Running as daemon without interactive input")
        await asyncio.Event().wait()
        return

    # Interactive mode with single keypress input using readchar
    loop = asyncio.get_running_loop()

    while True:
        try:
            # Run blocking readkey in executor to not block the event loop
            key = await loop.run_in_executor(None, readchar.readkey)
        except (asyncio.CancelledError, KeyboardInterrupt):
            break

        # Handle Ctrl+C
        if key == "\x03":
            break

        # Handle server selector mode
        if ui is not None and ui.is_server_selector_visible():
            if key == readchar.key.UP:
                ui.highlight_shortcut("selector-up")
                ui.move_server_selection(-1)
                continue
            if key == readchar.key.DOWN:
                ui.highlight_shortcut("selector-down")
                ui.move_server_selection(1)
                continue
            if key in ("\r", "\n", readchar.key.ENTER):
                ui.highlight_shortcut("selector-enter")
                await handler.select_server()
                continue
            # Ignore other keys when selector is open
            continue

        # Handle quit
        if key in "q":
            if ui:
                ui.highlight_shortcut("quit")
            break

        # Handle 's' to open server selector
        if key in "sS":
            if ui:
                ui.highlight_shortcut("server")
            handler.open_server_selector()
            continue

        # Handle shortcuts via dispatch table (case-insensitive for letter keys)
        action = shortcuts.get(key) or shortcuts.get(key.lower())
        if action:
            highlight_name, action_handler = action
            if highlight_name and ui:
                ui.highlight_shortcut(highlight_name)
            await action_handler()
            continue

        # Ignore unhandled escape sequences
        if key.startswith("\x1b"):
            continue
