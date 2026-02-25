"""Keyboard input handling for the Sendspin CLI."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import readchar
from aiosendspin.models.types import MediaCommand, PlaybackStateType, PlayerStateType

if TYPE_CHECKING:
    from aiosendspin.client import SendspinClient

    from sendspin.audio_connector import AudioStreamHandler
    from sendspin.settings import ClientSettings
    from sendspin.tui.app import AppState
    from sendspin.tui.ui import SendspinUI

logger = logging.getLogger(__name__)


class CommandHandler:
    """Handles keyboard commands."""

    def __init__(
        self,
        client: SendspinClient,
        state: AppState,
        audio_handler: AudioStreamHandler,
        ui: SendspinUI,
        settings: ClientSettings,
    ) -> None:
        """Initialize the command handler."""
        self._client = client
        self._state = state
        self._audio_handler = audio_handler
        self._ui = ui
        self._settings = settings

    async def send_media_command(self, command: MediaCommand) -> None:
        """Send a media command with validation."""
        if command not in self._state.supported_commands:
            self._ui.add_event(f"Server does not support {command.value}")
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
        self._settings.update(player_volume=target)
        self._audio_handler.set_volume(self._state.player_volume, muted=self._state.player_muted)
        self._ui.set_player_volume(self._state.player_volume, muted=self._state.player_muted)
        await self._client.send_player_state(
            state=PlayerStateType.SYNCHRONIZED,
            volume=self._state.player_volume,
            muted=self._state.player_muted,
        )
        self._ui.add_event(f"Player volume: {target}%")

    async def toggle_player_mute(self) -> None:
        """Toggle player (local) mute state."""
        self._state.player_muted = not self._state.player_muted
        self._settings.update(player_muted=self._state.player_muted)
        self._audio_handler.set_volume(self._state.player_volume, muted=self._state.player_muted)
        self._ui.set_player_volume(self._state.player_volume, muted=self._state.player_muted)
        await self._client.send_player_state(
            state=PlayerStateType.SYNCHRONIZED,
            volume=self._state.player_volume,
            muted=self._state.player_muted,
        )
        self._ui.add_event("Player muted" if self._state.player_muted else "Player unmuted")

    async def adjust_delay(self, delta: float) -> None:
        """Adjust static delay by delta milliseconds."""
        self._client.set_static_delay_ms(self._client.static_delay_ms + delta)
        self._ui.set_delay(self._client.static_delay_ms)
        self._settings.update(static_delay_ms=self._client.static_delay_ms)

    def close_server_selector(self) -> None:
        """Close the server selector panel."""
        self._ui.hide_server_selector()


async def keyboard_loop(
    client: SendspinClient,
    state: AppState,
    audio_handler: AudioStreamHandler,
    ui: SendspinUI,
    settings: ClientSettings,
    show_server_selector: Callable[[], None],
    on_server_selected: Callable[[], Awaitable[None]],
    request_shutdown: Callable[[], None],
) -> None:
    """Run the keyboard input loop.

    Args:
        client: Sendspin client instance.
        state: Application state.
        audio_handler: Audio stream handler.
        ui: UI instance.
        settings: Settings manager for persisting player settings.
        show_server_selector: Function to show the server selector UI.
        on_server_selected: Async callback when a server is selected.
        request_shutdown: Callback to request application shutdown.
    """
    handler = CommandHandler(client, state, audio_handler, ui, settings)

    # Key dispatch table: key -> (highlight_name | None, async action)
    # For keys that need case-insensitive matching, use lowercase
    shortcuts: dict[str, tuple[str | None, Callable[[], Awaitable[None]]]] = {
        # Letter keys
        " ": ("space", handler.toggle_play_pause),
        "m": ("mute", handler.toggle_player_mute),
        "g": ("switch", lambda: handler.send_media_command(MediaCommand.SWITCH)),
        # Delay adjustment
        ",": ("delay-", lambda: handler.adjust_delay(-10)),
        ".": ("delay+", lambda: handler.adjust_delay(10)),
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

    # Interactive mode with single keypress input using readchar
    loop = asyncio.get_running_loop()

    while True:
        try:
            # Run blocking readkey in executor to not block the event loop
            key = await loop.run_in_executor(None, readchar.readkey)
        except (asyncio.CancelledError, KeyboardInterrupt):
            request_shutdown()
            break

        # Handle server selector mode
        if ui.is_server_selector_visible():
            if key in "rR":
                show_server_selector()
                continue
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
                await on_server_selected()
                continue
            if key in "qQ":
                ui.hide_server_selector()
                continue
            # Ignore other keys when selector is open
            continue

        # Handle quit
        if key in "qQ":
            ui.highlight_shortcut("quit")
            request_shutdown()
            break

        # Handle 's' to open server selector
        if key in "sS":
            ui.highlight_shortcut("server")
            show_server_selector()
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
