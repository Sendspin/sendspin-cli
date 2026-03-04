"""Audio connector for connecting audio playback to a Sendspin client."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import StreamStartMessage
from aiosendspin.models.types import AudioCodec, ClientStateType, Roles

from sendspin.audio import AudioDevice, AudioPlayer
from sendspin.decoder import FlacDecoder
from sendspin.hardware_volume import HardwareVolumeController
from sendspin.utils import create_task

if TYPE_CHECKING:
    from aiosendspin.client import AudioFormat, SendspinClient


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ChunkWorkItem:
    """Audio chunk submission work for the synchronous audio worker."""

    server_timestamp_us: int
    audio_data: bytes
    fmt: AudioFormat


@dataclass(slots=True)
class _ClearWorkItem:
    """Queue-clear work for the synchronous audio worker."""


@dataclass(slots=True)
class _SetVolumeWorkItem:
    """Software volume update work for the synchronous audio worker."""

    volume: int
    muted: bool


@dataclass(slots=True)
class _StopWorkItem:
    """Stop signal for the synchronous audio worker."""


type _AudioWorkItem = _ChunkWorkItem | _ClearWorkItem | _SetVolumeWorkItem | _StopWorkItem


class _AudioSyncWorker:
    """Owns AudioPlayer + decode pipeline on a dedicated thread."""

    def __init__(
        self,
        *,
        audio_device: AudioDevice,
        use_software_volume: bool,
        volume: int,
        muted: bool,
    ) -> None:
        self._audio_device = audio_device
        self._use_software_volume = use_software_volume
        self._initial_volume = volume
        self._initial_muted = muted

        self._queue: queue.Queue[_AudioWorkItem] | None = None
        self._thread: threading.Thread | None = None

    def start(
        self,
        compute_play_time: Callable[[int], int],
        compute_server_time: Callable[[int], int],
    ) -> None:
        """Start worker thread if needed."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._queue = queue.Queue(maxsize=512)
        self._thread = threading.Thread(
            target=self._run,
            args=(compute_play_time, compute_server_time),
            name="sendspin-audio-worker",
            daemon=True,
        )
        self._thread.start()

    def is_running(self) -> bool:
        """Whether the worker thread is currently alive."""
        return self._thread is not None and self._thread.is_alive()

    def submit_chunk(self, server_timestamp_us: int, audio_data: bytes, fmt: AudioFormat) -> None:
        """Submit one incoming audio chunk for processing."""
        self._enqueue(_ChunkWorkItem(server_timestamp_us, audio_data, fmt))

    def clear(self) -> None:
        """Clear queued audio on worker."""
        self._enqueue(_ClearWorkItem())

    def set_volume(self, volume: int, *, muted: bool) -> None:
        """Update software volume and forward to worker if enabled."""
        if self._use_software_volume:
            self._enqueue(_SetVolumeWorkItem(volume=volume, muted=muted))

    async def stop(self) -> None:
        """Stop worker thread and close queue resources."""
        queue_obj = self._queue
        thread = self._thread
        self._queue = None
        self._thread = None

        if queue_obj is None:
            return

        try:
            queue_obj.put_nowait(_StopWorkItem())
        except queue.Full:
            while True:
                try:
                    queue_obj.get_nowait()
                except queue.Empty:
                    break
            try:
                queue_obj.put_nowait(_StopWorkItem())
            except queue.Full:
                logger.warning("Failed to enqueue audio worker stop sentinel")

        if thread is not None and thread.is_alive():
            await asyncio.get_running_loop().run_in_executor(None, thread.join, 1.0)

    def _enqueue(self, item: _AudioWorkItem) -> None:
        """Best-effort enqueue to worker; drops on sustained overload."""
        queue_obj = self._queue
        if queue_obj is None:
            return
        try:
            queue_obj.put_nowait(item)
        except queue.Full:
            logger.warning("Audio worker queue full; dropping %s", type(item).__name__)

    def _run(
        self,
        compute_play_time: Callable[[int], int],
        compute_server_time: Callable[[int], int],
    ) -> None:
        """Worker thread main loop."""
        queue_obj = self._queue
        if queue_obj is None:
            return

        player = AudioPlayer(compute_play_time, compute_server_time)
        current_format: AudioFormat | None = None
        flac_decoder: FlacDecoder | None = None
        software_volume = self._initial_volume
        software_muted = self._initial_muted

        if self._use_software_volume:
            player.set_volume(software_volume, muted=software_muted)

        while True:
            try:
                item = queue_obj.get()
            except Exception:
                break

            item_type = type(item)

            if item_type is _StopWorkItem:
                break

            if item_type is _ClearWorkItem:
                player.clear()
                continue

            if item_type is _SetVolumeWorkItem:
                if self._use_software_volume:
                    volume_item = cast(_SetVolumeWorkItem, item)
                    software_volume = volume_item.volume
                    software_muted = volume_item.muted
                    player.set_volume(software_volume, muted=software_muted)
                continue

            chunk_item = cast(_ChunkWorkItem, item)
            fmt = chunk_item.fmt
            if current_format != fmt:
                current_format = fmt
                player.set_format(fmt, device=self._audio_device)

                if fmt.codec == AudioCodec.FLAC:
                    flac_decoder = FlacDecoder(fmt)
                    pcm_format = fmt.pcm_format
                    logger.info(
                        "Initialized FLAC decoder for %dHz/%d-bit/%dch",
                        pcm_format.sample_rate,
                        pcm_format.bit_depth,
                        pcm_format.channels,
                    )
                else:
                    flac_decoder = None

                if self._use_software_volume:
                    player.set_volume(software_volume, muted=software_muted)

            payload = chunk_item.audio_data
            if fmt.codec == AudioCodec.FLAC:
                if flac_decoder is None:
                    flac_decoder = FlacDecoder(fmt)
                payload = flac_decoder.decode(payload)
                if not payload:
                    continue

            player.submit(chunk_item.server_timestamp_us, payload)

        player.stop()


class AudioStreamHandler:
    """Manages audio playback state and stream lifecycle.

    This handler connects to a SendspinClient and manages audio playback
    by listening for audio chunks, stream start/end events, and handling
    format changes. Supports PCM and FLAC codecs.

    When hardware volume is enabled, the handler owns a HardwareVolumeController
    and routes volume changes to it, keeping the software player at full volume.
    """

    def __init__(
        self,
        audio_device: AudioDevice,
        *,
        volume: int = 100,
        muted: bool = False,
        on_event: Callable[[str], None] | None = None,
        on_format_change: Callable[[str | None, int, int, int], None] | None = None,
        on_volume_change: Callable[[int, bool], None] | None = None,
        use_hardware_volume: bool = False,
    ) -> None:
        """Initialize the audio stream handler.

        Args:
            audio_device: Audio device to use for playback.
            volume: Initial volume (0-100).
            muted: Initial muted state.
            on_event: Callback for stream lifecycle events ("start" or "stop").
            on_format_change: Callback for format changes (codec, sample_rate, bit_depth, channels).
            on_volume_change: Callback for volume changes.
            use_hardware_volume: Whether to use hardware volume control if available.
        """
        self._audio_device = audio_device
        self._volume = volume
        self._muted = muted
        self._on_event = on_event
        self._on_format_change = on_format_change
        self._on_volume_change = on_volume_change
        self._client: SendspinClient | None = None
        self._current_format: AudioFormat | None = None
        self._stream_active = False

        # Kept for compatibility; playback is managed by _AudioSyncWorker.
        self.audio_player: AudioPlayer | None = None

        self._audio_worker: _AudioSyncWorker | None = None

        self._hw_volume: HardwareVolumeController | None = None
        if use_hardware_volume:
            self._hw_volume = HardwareVolumeController()

    @property
    def volume(self) -> int:
        """Current logical volume (what the server/user sees)."""
        return self._volume

    @property
    def muted(self) -> bool:
        """Current logical muted state (what the server/user sees)."""
        return self._muted

    async def read_initial_volume(self) -> None:
        """Read the effective initial volume state.

        When hardware volume is active, reads the current system volume/mute
        state. Otherwise the constructor values are used as-is.
        """
        if self._hw_volume is None:
            return

        self._volume, self._muted = await self._hw_volume.get_state()

    async def start_volume_monitor(self) -> None:
        """Start hardware volume monitoring if applicable."""
        if self._hw_volume is not None:
            await self._hw_volume.start_monitoring(self._on_hw_volume_change)

    @property
    def use_hardware_volume(self) -> bool:
        """Whether this handler is using hardware volume control."""
        return self._hw_volume is not None

    def set_volume(self, volume: int, *, muted: bool) -> None:
        """Set the volume and muted state.

        Routes to the hardware controller when active, otherwise forwards
        software volume updates to the sync audio worker. Notifies the server
        and fires the on_volume_change callback.

        Args:
            volume: Volume level (0-100).
            muted: Muted state.
        """
        if self._hw_volume is not None:
            create_task(self._hw_volume.set_state(volume, muted=muted))
            return

        self._volume = volume
        self._muted = muted
        if self._audio_worker is not None:
            self._audio_worker.set_volume(volume, muted=muted)

        self.send_player_volume()
        if self._on_volume_change is not None:
            self._on_volume_change(volume, muted)

    def _on_hw_volume_change(self, volume: int, muted: bool) -> None:
        """Handle external hardware volume changes from the controller."""
        self._volume = volume
        self._muted = muted
        self.send_player_volume()
        if self._on_volume_change is not None:
            self._on_volume_change(volume, muted)

    def send_player_volume(self) -> None:
        """Send current player volume/mute state to the server."""
        if self._client is not None and self._client.connected:
            create_task(
                self._client.send_player_state(
                    state=ClientStateType.SYNCHRONIZED,
                    volume=self._volume,
                    muted=self._muted,
                )
            )

    def attach_client(self, client: SendspinClient) -> list[Callable[[], None]]:
        """Attach to a SendspinClient and register listeners.

        Args:
            client: The Sendspin client to attach to.

        Returns:
            List of unsubscribe functions for all registered listeners.
        """
        self._client = client
        self._start_audio_worker(client)

        return [
            client.add_audio_chunk_listener(self._on_audio_chunk),
            client.add_stream_start_listener(self._on_stream_start),
            client.add_stream_end_listener(self._on_stream_end),
            client.add_stream_clear_listener(self._on_stream_clear),
        ]

    def _start_audio_worker(self, client: SendspinClient) -> None:
        """Start sync worker once during attach and fail fast if unavailable."""
        if self._audio_worker is None:
            self._audio_worker = _AudioSyncWorker(
                audio_device=self._audio_device,
                use_software_volume=self._hw_volume is None,
                volume=self._volume,
                muted=self._muted,
            )

        self._audio_worker.start(client.compute_play_time, client.compute_server_time)
        if not self._audio_worker.is_running():
            raise RuntimeError("Audio worker failed to start")

    def _require_audio_worker(self) -> _AudioSyncWorker:
        """Get a running audio worker or raise immediately."""
        worker = self._audio_worker
        if worker is None or not worker.is_running():
            raise RuntimeError("Audio worker is not running")
        return worker

    def _clear_audio_worker(self) -> None:
        """Clear worker queue when worker is available."""
        worker = self._audio_worker
        if worker is not None and worker.is_running():
            worker.clear()

    def _on_audio_chunk(
        self, server_timestamp_us: int, audio_data: bytes, fmt: AudioFormat
    ) -> None:
        """Handle incoming audio chunks by enqueueing them to the sync worker."""
        assert self._client is not None, "Received audio chunk but client is not attached"
        worker = self._require_audio_worker()

        pcm_format = fmt.pcm_format
        if self._current_format != fmt:
            self._current_format = fmt
            if self._on_format_change is not None:
                self._on_format_change(
                    fmt.codec.value,
                    pcm_format.sample_rate,
                    pcm_format.bit_depth,
                    pcm_format.channels,
                )

        worker.submit_chunk(server_timestamp_us, audio_data, fmt)

    def _on_stream_start(self, _message: StreamStartMessage) -> None:
        """Handle stream start by clearing stale audio chunks."""
        self._clear_audio_worker()

        if not self._stream_active:
            self._stream_active = True
            if self._on_event:
                self._on_event("start")

    def _on_stream_end(self, roles: list[str] | None) -> None:
        """Handle stream end by clearing audio queue."""
        if roles is not None and Roles.PLAYER.value not in roles:
            return

        self._clear_audio_worker()

        if self._stream_active:
            self._stream_active = False
            if self._on_event:
                self._on_event("stop")

    def _on_stream_clear(self, roles: list[str] | None) -> None:
        """Handle stream clear by clearing audio queue (e.g., for seek operations)."""
        if roles is None or Roles.PLAYER.value in roles:
            self._clear_audio_worker()

    def clear_queue(self) -> None:
        """Clear the audio queue to prevent desync."""
        self._clear_audio_worker()

    async def cleanup(self) -> None:
        """Stop audio worker, hardware monitoring, and clear resources."""
        if self._hw_volume is not None:
            await self._hw_volume.stop_monitoring()

        if self._stream_active:
            self._stream_active = False
            if self._on_event:
                self._on_event("stop")

        if self._audio_worker is not None:
            await self._audio_worker.stop()
            self._audio_worker = None

        self._current_format = None
        self.audio_player = None
