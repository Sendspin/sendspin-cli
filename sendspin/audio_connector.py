"""Audio connector for connecting audio playback to a Sendspin client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.models.core import (
    GroupUpdateServerPayload,
    ServerCommandPayload,
    StreamStartMessage,
)
from aiosendspin.models.types import PlayerCommand, PlayerStateType, Roles

from sendspin.audio import AudioDevice, AudioPlayer

if TYPE_CHECKING:
    from aiosendspin.client import PCMFormat, SendspinClient

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
        # Track user's preferred volume to restore after group changes
        self._saved_player_volume: int = 100
        self._saved_player_muted: bool = False
        self._pending_volume_restore: bool = False
        self._current_group_id: str | None = None

    def attach_client(self, client: SendspinClient) -> list[Callable[[], None]]:
        """Attach to a SendspinClient and register listeners.

        Args:
            client: The Sendspin client to attach to.

        Returns:
            List of unsubscribe functions for all registered listeners.
        """
        self._client = client

        # Register listeners directly with the client
        return [
            client.add_audio_chunk_listener(self._on_audio_chunk),
            client.add_stream_start_listener(self._on_stream_start),
            client.add_stream_end_listener(self._on_stream_end),
            client.add_stream_clear_listener(self._on_stream_clear),
            client.add_server_command_listener(self._on_server_command),
            client.add_group_update_listener(self._on_group_update),
        ]

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

    def _on_group_update(self, payload: GroupUpdateServerPayload) -> None:
        """Handle group update messages."""
        # Only track group changes for volume persistence
        # Ensure we're switching TO a group (not leaving)
        group_changed = (
            payload.group_id is not None
            and self._current_group_id is not None
            and payload.group_id != self._current_group_id
        )
        if group_changed:
            self._current_group_id = payload.group_id
            # Save current volume settings before group change
            # and flag that we should restore after server command
            # Only save if audio player exists to avoid restoring stale values
            if self.audio_player is not None:
                self._saved_player_volume = self.audio_player.volume
                self._saved_player_muted = self.audio_player.muted
                self._pending_volume_restore = True
                logger.debug(
                    "Group changed, saved volume: %d (muted: %s)",
                    self._saved_player_volume,
                    self._saved_player_muted,
                )
        elif payload.group_id is not None and self._current_group_id is None:
            # First time joining a group - just track it, don't set restore flag
            self._current_group_id = payload.group_id
            logger.debug("Joined first group: %s", self._current_group_id)

    def _on_server_command(self, payload: ServerCommandPayload) -> None:
        """Handle server commands for player volume/mute control."""
        if payload.player is None or self.audio_player is None or self._client is None:
            return

        player_cmd = payload.player

        if player_cmd.command == PlayerCommand.VOLUME and player_cmd.volume is not None:
            self.audio_player.set_volume(player_cmd.volume, muted=self.audio_player.muted)
            logger.debug("Server set player volume: %d", player_cmd.volume)
        elif player_cmd.command == PlayerCommand.MUTE and player_cmd.mute is not None:
            self.audio_player.set_volume(self.audio_player.volume, muted=player_cmd.mute)
            logger.debug("Server %s player", "muted" if player_cmd.mute else "unmuted")

        # If volume restore is pending (after group change), restore saved volume
        if self._pending_volume_restore:
            self._pending_volume_restore = False
            self.audio_player.set_volume(self._saved_player_volume, muted=self._saved_player_muted)
            logger.info(
                "Restored player volume: %d (muted: %s)",
                self._saved_player_volume,
                self._saved_player_muted,
            )

        # Send state update back to server per spec
        asyncio.create_task(
            self._client.send_player_state(
                state=PlayerStateType.SYNCHRONIZED,
                volume=self.audio_player.volume,
                muted=self.audio_player.muted,
            )
        )

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
