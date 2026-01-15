"""Audio connector for connecting audio playback to a Sendspin client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.models.core import StreamStartMessage
from aiosendspin.models.types import Roles

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

    def __init__(
        self,
        audio_device: AudioDevice,
        *,
        volume: int = 100,
        muted: bool = False,
        on_format_change: Callable[[str | None, int, int, int], None] | None = None,
    ) -> None:
        """Initialize the audio stream handler.

        Args:
            audio_device: Audio device to use for playback.
            volume: Initial volume (0-100).
            muted: Initial muted state.
            on_format_change: Callback for format changes (codec, sample_rate, bit_depth, channels).
        """
        self._audio_device = audio_device
        self._volume = volume
        self._muted = muted
        self._on_format_change = on_format_change
        self._client: SendspinClient | None = None
        self.audio_player: AudioPlayer | None = None
        self._current_format: PCMFormat | None = None
        self._current_codec: str | None = None

    def set_volume(self, volume: int, *, muted: bool) -> None:
        """Set the volume and muted state.

        Updates the cached values and applies to the audio player if active.

        Args:
            volume: Volume level (0-100).
            muted: Muted state.
        """
        self._volume = volume
        self._muted = muted
        if self.audio_player is not None:
            self.audio_player.set_volume(volume, muted=muted)

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

            self.audio_player.set_volume(self._volume, muted=self._muted)

            if self._on_format_change is not None:
                self._on_format_change(
                    self._current_codec,
                    fmt.sample_rate,
                    fmt.bit_depth,
                    fmt.channels,
                )

        # Submit audio chunk - AudioPlayer handles timing
        self.audio_player.submit(server_timestamp_us, audio_data)

    def _on_stream_start(self, message: StreamStartMessage) -> None:
        """Handle stream start by clearing stale audio chunks."""
        # Capture codec from stream start message for UI display
        if message.payload.player is not None:
            self._current_codec = message.payload.player.codec.value

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
