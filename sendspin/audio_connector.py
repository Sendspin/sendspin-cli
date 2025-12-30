"""Audio connector for connecting audio playback to a Sendspin client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.client import PCMFormat, SendspinClient
from aiosendspin.models.core import ServerCommandPayload, StreamStartMessage
from aiosendspin.models.types import PlayerCommand, Roles

from sendspin.audio import AudioDevice, AudioPlayer

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class AudioStreamHandler:
    """Manages audio playback state and stream lifecycle.

    This handler connects to a SendspinClient and manages audio playback
    by listening for audio chunks, stream start/end events, and handling
    format changes.
    """

    def __init__(self, audio_device: AudioDevice) -> None:
        """Initialize the audio stream handler.

        Args:
            audio_device: Audio device to use for playback.
        """
        self._audio_device = audio_device
        self._client: SendspinClient | None = None
        self.audio_player: AudioPlayer | None = None
        self._current_format: PCMFormat | None = None
        self._unsubscribe_fns: list[Callable[[], None]] = []

    def attach_client(self, client: SendspinClient) -> None:
        """Attach to a SendspinClient and register listeners.

        If already attached, detaches from the previous client first.

        Args:
            client: The Sendspin client to attach to.
        """
        # Detach from any previous client first
        if self._unsubscribe_fns:
            raise RuntimeError("AudioStreamHandler is already attached to a client")

        self._client = client

        # Register listeners and store unsubscribe functions
        self._unsubscribe_fns = [
            client.add_audio_chunk_listener(self._on_audio_chunk),
            client.add_stream_start_listener(self._on_stream_start),
            client.add_stream_end_listener(self._on_stream_end),
            client.add_stream_clear_listener(self._on_stream_clear),
            client.add_server_command_listener(self._on_server_command),
        ]

    def detach_client(self) -> None:
        """Detach from the current client and remove listeners."""
        for unsubscribe in self._unsubscribe_fns:
            unsubscribe()
        self._unsubscribe_fns.clear()
        self._client = None

    def _on_audio_chunk(self, server_timestamp_us: int, audio_data: bytes, fmt: PCMFormat) -> None:
        """Handle incoming audio chunks."""
        assert self._client is not None, "Received audio chunk but client is not attached"

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

    def _on_stream_start(self, _message: StreamStartMessage) -> None:
        """Handle stream start by clearing stale audio chunks."""
        if self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream start")

    def _on_stream_end(self, roles: list[Roles] | None) -> None:
        """Handle stream end by clearing audio queue to prevent desync on resume."""
        # For the CLI player, we only care about the player role
        if (roles is None or Roles.PLAYER in roles) and self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream end")

    def _on_stream_clear(self, roles: list[Roles] | None) -> None:
        """Handle stream clear by clearing audio queue (e.g., for seek operations)."""
        # For the CLI player, we only care about the player role
        if (roles is None or Roles.PLAYER in roles) and self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream clear")

    async def _on_server_command(self, payload: ServerCommandPayload) -> None:
        """Handle server commands for player volume/mute control."""
        if payload.player is None or self.audio_player is None:
            return

        player_cmd = payload.player

        if player_cmd.command == PlayerCommand.VOLUME and player_cmd.volume is not None:
            self.audio_player.set_volume(player_cmd.volume, muted=self.audio_player.muted)
        elif player_cmd.command == PlayerCommand.MUTE and player_cmd.mute is not None:
            self.audio_player.set_volume(self.audio_player.volume, muted=player_cmd.mute)

    def clear_queue(self) -> None:
        """Clear the audio queue to prevent desync."""
        if self.audio_player is not None:
            self.audio_player.clear()

    async def cleanup(self) -> None:
        """Stop audio player and clear resources."""
        if self.audio_player is not None:
            await self.audio_player.stop()
            self.audio_player = None
        self._current_format = None
