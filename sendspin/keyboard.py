"""Keyboard input handling for the Sendspin CLI."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

import readchar
from aiosendspin.models.types import MediaCommand, PlaybackStateType, PlayerStateType

if TYPE_CHECKING:
    from aiosendspin.client import SendspinClient

    from sendspin.cli import AudioStreamHandler, CLIState
    from sendspin.ui import SendspinUI

logger = logging.getLogger(__name__)


class CommandHandler:
    """Parses and executes user commands from the keyboard."""

    def __init__(
        self,
        client: SendspinClient,
        state: CLIState,
        audio_handler: AudioStreamHandler,
        ui: SendspinUI | None = None,
        print_event: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize the command handler."""
        self._client = client
        self._state = state
        self._audio_handler = audio_handler
        self._ui = ui
        self._print_event = print_event or (lambda _: None)

    async def execute(self, line: str) -> bool:
        """Parse and execute a command.

        Returns True if the user wants to quit, False otherwise.
        """
        raw_line = line.strip()
        if not raw_line:
            return False

        parts = raw_line.split()
        command_lower = raw_line.lower()
        keyword = parts[0].lower()

        if command_lower in {"quit", "exit", "q"}:
            return True
        if command_lower in {"play", "p"}:
            await self._send_media_command(MediaCommand.PLAY)
        elif command_lower in {"pause", "space"}:
            await self._send_media_command(MediaCommand.PAUSE)
        elif command_lower in {"stop", "s"}:
            await self._send_media_command(MediaCommand.STOP)
        elif command_lower in {"next", "n"}:
            await self._send_media_command(MediaCommand.NEXT)
        elif command_lower in {"previous", "prev", "b"}:
            await self._send_media_command(MediaCommand.PREVIOUS)
        elif command_lower in {"vol+", "volume+", "+"}:
            await self._change_volume(5)
        elif command_lower in {"vol-", "volume-", "-"}:
            await self._change_volume(-5)
        elif command_lower in {"mute", "m"}:
            await self._toggle_mute()
        elif command_lower == "toggle":
            await self._toggle_play_pause()
        elif command_lower in {"repeat_off", "repeat-off", "ro"}:
            await self._send_media_command(MediaCommand.REPEAT_OFF)
        elif command_lower in {"repeat_one", "repeat-one", "r1"}:
            await self._send_media_command(MediaCommand.REPEAT_ONE)
        elif command_lower in {"repeat_all", "repeat-all", "ra"}:
            await self._send_media_command(MediaCommand.REPEAT_ALL)
        elif command_lower in {"shuffle", "sh"}:
            await self._send_media_command(MediaCommand.SHUFFLE)
        elif command_lower in {"unshuffle", "ush"}:
            await self._send_media_command(MediaCommand.UNSHUFFLE)
        elif command_lower in {"switch", "sw"}:
            await self._send_media_command(MediaCommand.SWITCH)
        elif command_lower in {"pvol+", "pvolume+"}:
            await self._change_player_volume(5)
        elif command_lower in {"pvol-", "pvolume-"}:
            await self._change_player_volume(-5)
        elif command_lower in {"pmute", "pm"}:
            await self._toggle_player_mute()
        elif keyword == "delay":
            self._handle_delay_command(parts)
        else:
            self._print_event("Unknown command")

        return False

    async def _send_media_command(self, command: MediaCommand) -> None:
        """Send a media command with validation."""
        if command not in self._state.supported_commands:
            self._print_event(f"Server does not support {command.value}")
            return
        await self._client.send_group_command(command)

    async def _toggle_play_pause(self) -> None:
        """Toggle between play and pause."""
        if self._state.playback_state == PlaybackStateType.PLAYING:
            await self._send_media_command(MediaCommand.PAUSE)
        else:
            await self._send_media_command(MediaCommand.PLAY)

    async def _change_volume(self, delta: int) -> None:
        """Adjust volume by delta."""
        if MediaCommand.VOLUME not in self._state.supported_commands:
            self._print_event("Server does not support volume control")
            return
        current = self._state.volume if self._state.volume is not None else 50
        target = max(0, min(100, current + delta))
        await self._client.send_group_command(MediaCommand.VOLUME, volume=target)

    async def _toggle_mute(self) -> None:
        """Toggle mute state."""
        if MediaCommand.MUTE not in self._state.supported_commands:
            self._print_event("Server does not support mute control")
            return
        target = not bool(self._state.muted)
        await self._client.send_group_command(MediaCommand.MUTE, mute=target)

    async def _change_player_volume(self, delta: int) -> None:
        """Adjust player (system) volume by delta."""
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

    async def _toggle_player_mute(self) -> None:
        """Toggle player (system) mute state."""
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

    def _handle_delay_command(self, parts: list[str]) -> None:
        """Process delay commands."""
        if len(parts) == 1:
            self._print_event(f"Static delay: {self._client.static_delay_ms:.1f} ms")
            return
        if len(parts) == 3 and parts[1] in {"+", "-"}:
            try:
                delta = float(parts[2])
            except ValueError:
                self._print_event("Invalid delay value")
                return
            if parts[1] == "-":
                delta = -delta
            self._client.set_static_delay_ms(self._client.static_delay_ms + delta)
            self._print_event(f"Static delay: {self._client.static_delay_ms:.1f} ms")
            return
        if len(parts) == 2:
            try:
                value = float(parts[1])
            except ValueError:
                self._print_event("Invalid delay value")
                return
            self._client.set_static_delay_ms(value)
            self._print_event(f"Static delay: {self._client.static_delay_ms:.1f} ms")
            return
        self._print_event("Usage: delay [<ms>|+ <ms>|- <ms>]")


async def keyboard_loop(
    client: SendspinClient,
    state: CLIState,
    audio_handler: AudioStreamHandler,
    ui: SendspinUI | None = None,
    print_event: Callable[[str], None] | None = None,
) -> None:
    """Run the keyboard input loop."""
    handler = CommandHandler(client, state, audio_handler, ui, print_event)

    if not sys.stdin.isatty():
        logger.info("Running as daemon without interactive input")
        await asyncio.Event().wait()
        return

    # Interactive mode with single keypress input using readchar
    loop = asyncio.get_running_loop()
    input_buffer = ""

    while True:
        try:
            # Run blocking readkey in executor to not block the event loop
            key = await loop.run_in_executor(None, readchar.readkey)
        except asyncio.CancelledError:
            break

        # Handle Ctrl+C
        if key == "\x03":
            break

        # Handle arrow keys
        if key == readchar.key.RIGHT:
            if ui:
                ui.highlight_shortcut("next")
            await handler.execute("n")
            continue
        if key == readchar.key.LEFT:
            if ui:
                ui.highlight_shortcut("prev")
            await handler.execute("b")
            continue
        if key == readchar.key.UP:
            if ui:
                ui.highlight_shortcut("up")
            await handler.execute("+")
            continue
        if key == readchar.key.DOWN:
            if ui:
                ui.highlight_shortcut("down")
            await handler.execute("-")
            continue

        # Ignore any other escape sequences
        if key.startswith("\x1b"):
            continue

        # Handle Enter - execute buffered command
        if key in ("\r", "\n", readchar.key.ENTER):
            if input_buffer:
                if await handler.execute(input_buffer):
                    break
                input_buffer = ""
            continue

        # Handle backspace
        if key in ("\x7f", "\x08", readchar.key.BACKSPACE):
            input_buffer = input_buffer[:-1]
            continue

        # Handle quit immediately
        if not input_buffer and key in "qQ":
            if ui:
                ui.highlight_shortcut("quit")
            break

        # Handle single-char shortcuts (immediate execution)
        if not input_buffer and key in "sSmM":
            if key.lower() == "m" and ui:
                ui.highlight_shortcut("mute")
            await handler.execute(key)
            continue

        # Handle space for play/pause toggle
        if not input_buffer and key == " ":
            if ui:
                ui.highlight_shortcut("space")
            await handler.execute("toggle")
            continue

        # Accumulate other characters
        if len(key) == 1 and key.isprintable():
            input_buffer += key
