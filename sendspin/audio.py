"""Audio playback for the Sendspin CLI with time synchronization.

This module provides an AudioPlayer that handles time-synchronized audio playback
with DAC-level timing precision. It manages buffering, scheduled start times,
and sync error correction to maintain sync between server and client timelines.

This module also provides device enumeration utilities for listing and resolving
audio output devices.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Final, Protocol, cast

import numpy as np
import sounddevice
from aiosendspin.client.time_sync import SendspinTimeFilter
from sounddevice import CallbackFlags

if TYPE_CHECKING:
    from aiosendspin.client import PCMFormat

    class CDataTimeInfo:
        """Type stub for sounddevice CFFI time info."""

        inputBufferAdcTime: float  # noqa: N815
        currentTime: float  # noqa: N815
        outputBufferDacTime: float  # noqa: N815


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AudioDevice:
    """Represents an audio output device.

    Attributes:
        index: Device index used for selection.
        name: Human-readable device name.
        output_channels: Number of output channels supported.
        sample_rate: Default sample rate in Hz.
        is_default: Whether this is the system default output device.
    """

    index: int
    name: str
    output_channels: int
    sample_rate: float
    is_default: bool


def query_devices() -> list[AudioDevice]:
    """Query all available audio output devices.

    Returns:
        List of AudioDevice objects for devices with output channels.
    """
    devices = sounddevice.query_devices()
    default_output = int(sounddevice.default.device[1])

    result: list[AudioDevice] = []
    for i in range(len(devices)):
        dev = devices[i]
        if dev["max_output_channels"] > 0:
            result.append(
                AudioDevice(
                    index=i,
                    name=str(dev["name"]),
                    output_channels=int(dev["max_output_channels"]),
                    sample_rate=float(dev["default_samplerate"]),
                    is_default=(i == default_output),
                )
            )
    return result


class AudioTimeInfo(Protocol):
    """Protocol for audio timing information from sounddevice callback.

    Provides DAC (Digital-to-Analog Converter) and other timing metrics
    needed for precise playback synchronization.
    """

    outputBufferDacTime: float  # noqa: N815
    """DAC time when the output buffer will be played (in seconds)."""


class PlaybackState(Enum):
    """State machine for audio playback lifecycle.

    Tracks the playback progression from initialization through active playback.
    """

    INITIALIZING = auto()
    """Waiting for first audio chunk and sync info."""

    WAITING_FOR_START = auto()
    """Buffer filled, scheduled start time computed, awaiting start gate."""

    PLAYING = auto()
    """Audio actively playing with sync corrections."""

    REANCHORING = auto()
    """Sync error exceeded threshold, resetting and waiting to restart."""


@dataclass
class _QueuedChunk:
    """Represents a queued audio chunk with timing information."""

    server_timestamp_us: int
    """Server timestamp when this chunk should start playing."""
    audio_data: bytes
    """Raw PCM audio bytes."""


class AudioPlayer:
    """
    Audio player for the Sendspin CLI with time synchronization support.

    This player accepts audio chunks with server timestamps and dynamically
    computes playback times using a time synchronization function. This allows
    for accurate synchronization even when the time base changes during playback.

    Attributes:
        _loop: The asyncio event loop used for scheduling.
        _compute_client_time: Function that converts server timestamps to client
            timestamps (monotonic loop time), accounting for clock drift, offset,
            and static delay.
        _compute_server_time: Function that converts client timestamps (monotonic
            loop time) to server timestamps (inverse of _compute_client_time).
    """

    _loop: asyncio.AbstractEventLoop
    _compute_client_time: Callable[[int], int]
    _compute_server_time: Callable[[int], int]

    _MIN_CHUNKS_TO_START: Final[int] = 16
    """Minimum chunks buffered before starting playback to absorb network jitter."""
    _MIN_CHUNKS_TO_MAINTAIN: Final[int] = 8
    """Minimum chunks to maintain during playback to avoid underruns."""
    _MICROSECONDS_PER_SECOND: Final[int] = 1_000_000
    """Conversion factor for time calculations."""
    _DAC_PER_LOOP_MIN: Final[float] = 0.999
    """Minimum DAC-to-loop time ratio to prevent wild extrapolation."""
    _DAC_PER_LOOP_MAX: Final[float] = 1.001
    """Maximum DAC-to-loop time ratio to prevent wild extrapolation."""

    # Sync error correction: playback speed adjustment range
    _MAX_SPEED_CORRECTION: Final[float] = 0.04
    """Maximum playback speed deviation for sync correction (0.04 = ±4% speed variation)."""

    # Sync error correction: secondary thresholds (rarely need adjustment)
    _CORRECTION_DEADBAND_US: Final[int] = 2_000
    """Sync error threshold below which no correction is applied (2 ms)."""
    _REANCHOR_THRESHOLD_US: Final[int] = 500_000
    """Sync error threshold above which re-anchoring is triggered (500 ms)."""
    _REANCHOR_COOLDOWN_US: Final[int] = 5_000_000
    """Minimum time between re-anchor events (5 seconds)."""
    _MIN_BUFFER_DURATION_US: Final[int] = 200_000
    """Minimum buffer duration (200ms) to start playback and absorb network jitter."""

    # Audio stream configuration
    _BLOCKSIZE: Final[int] = 2048
    """Audio block size (~46ms at 44.1kHz)."""

    # Time synchronization thresholds
    _EARLY_START_THRESHOLD_US: Final[int] = 700_000
    """Threshold for detecting early start due to fallback mapping (700ms)."""
    _START_TIME_UPDATE_THRESHOLD_US: Final[int] = 5_000
    """Minimum threshold for updating start time to avoid churn (5ms)."""

    # Sync correction planning
    _CORRECTION_TARGET_SECONDS: Final[float] = 2.0
    """Target window to fix sync error through micro-corrections (2 seconds)."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        compute_client_time: Callable[[int], int],
        compute_server_time: Callable[[int], int],
    ) -> None:
        """
        Initialize the audio player.

        Args:
            loop: The asyncio event loop to use for scheduling.
            compute_client_time: Function that converts server timestamps to client
                timestamps (monotonic loop time), accounting for clock drift, offset,
                and static delay.
            compute_server_time: Function that converts client timestamps (monotonic
                loop time) to server timestamps (inverse of compute_client_time).
        """
        self._loop = loop
        self._compute_client_time = compute_client_time
        self._compute_server_time = compute_server_time
        self._format: PCMFormat | None = None
        self._queue: asyncio.Queue[_QueuedChunk] = asyncio.Queue()
        self._stream: sounddevice.RawOutputStream | None = None
        self._closed = False
        self._stream_started = False
        self._first_real_chunk = True  # Flag to initialize timing from first chunk

        self._volume: int = 100  # 0-100 range
        self._muted: bool = False

        # Partial chunk tracking (to avoid discarding partial chunks)
        self._current_chunk: _QueuedChunk | None = None
        self._current_chunk_offset = 0

        # Track expected next chunk timestamp for intelligent gap/overlap handling
        self._expected_next_timestamp: int | None = None

        # Underrun tracking
        self._underrun_count = 0
        self._last_buffer_warning_us = 0

        # Track queued audio duration instead of just item count
        self._queued_duration_us = 0

        # DAC timing for accurate playback position tracking
        self._dac_loop_calibrations: collections.deque[tuple[int, int]] = collections.deque(
            maxlen=100
        )
        # Recent [(dac_time_us, loop_time_us), ...] pairs for DAC-Loop mapping
        self._last_known_playback_position_us: int = 0
        # Current playback position in server timestamp space
        self._last_dac_calibration_time_us: int = 0
        # Last loop time when we calibrated DAC-Loop mapping

        # Playback state machine
        self._playback_state: PlaybackState = PlaybackState.INITIALIZING
        """Current playback state (INITIALIZING, WAITING_FOR_START, PLAYING, REANCHORING)."""

        # Scheduled start anchoring
        self._scheduled_start_loop_time_us: int | None = None
        self._scheduled_start_dac_time_us: int | None = None

        # Server timeline cursor for the next input frame to be consumed
        self._server_ts_cursor_us: int = 0
        self._server_ts_cursor_remainder: int = 0  # fractional accumulator for microseconds

        # First-chunk and re-anchor tracking
        self._first_server_timestamp_us: int | None = None
        self._early_start_suspect: bool = False
        self._has_reanchored: bool = False

        # Low-overhead drift/sync correction scheduling (sample drop/insert)
        self._insert_every_n_frames: int = 0
        self._drop_every_n_frames: int = 0
        self._frames_until_next_insert: int = 0
        self._frames_until_next_drop: int = 0
        self._last_output_frame: bytes = b""

        # Sync error smoothing (Kalman filter) and re-anchor cooldown
        self._sync_error_filter = SendspinTimeFilter(process_std_dev=0.01, forget_factor=1.001)
        self._sync_error_filtered_us: float = 0.0  # Cached filtered error value
        self._last_reanchor_loop_time_us: int = 0
        self._last_sync_error_log_us: int = 0  # Rate limit sync error logging
        self._frames_inserted_since_log: int = 0  # Track inserts for logging
        self._frames_dropped_since_log: int = 0  # Track drops for logging
        self._callback_time_total_us: int = 0  # Total callback time for averaging
        self._callback_count: int = 0  # Number of callbacks for averaging

        # Thread-safe flag for deferred operations (audio thread → main thread)
        self._clear_requested: bool = False

    def set_format(self, pcm_format: PCMFormat, device: AudioDevice) -> None:
        """Configure the audio output format.

        Args:
            pcm_format: PCM audio format specification.
            device: Audio device to use.
        """
        self._format = pcm_format
        self._close_stream()

        # Reset state on format change
        self._stream_started = False
        self._first_real_chunk = True

        # Low latency settings for accurate playback (chunks arrive 5+ seconds early)
        self._stream = sounddevice.RawOutputStream(
            samplerate=pcm_format.sample_rate,
            channels=pcm_format.channels,
            dtype="int16",
            blocksize=self._BLOCKSIZE,
            callback=self._audio_callback,
            latency="high",
            device=device.index,
        )
        logger.info(
            "Audio stream configured: blocksize=%d, latency=high, device=%s",
            self._BLOCKSIZE,
            device,
        )

    def set_volume(self, volume: int, *, muted: bool) -> None:
        """
        Set the player volume and mute state.

        Args:
            volume: Volume level 0-100.
            muted: Whether audio is muted.
        """
        self._volume = max(0, min(100, volume))
        self._muted = muted

    async def stop(self) -> None:
        """Stop playback and release resources."""
        self._closed = True
        self._close_stream()

    def clear(self) -> None:
        """Drop all queued audio chunks."""
        # Clear deferred operation flag
        self._clear_requested = False

        # Drain all queued chunks
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Reset playback state
        self._playback_state = PlaybackState.INITIALIZING
        self._first_real_chunk = True
        self._current_chunk = None
        self._current_chunk_offset = 0
        self._expected_next_timestamp = None
        self._underrun_count = 0
        self._queued_duration_us = 0
        # Reset timing calibration for fresh start
        self._dac_loop_calibrations.clear()
        self._last_known_playback_position_us = 0
        self._last_dac_calibration_time_us = 0
        self._scheduled_start_loop_time_us = None
        self._scheduled_start_dac_time_us = None
        self._server_ts_cursor_us = 0
        self._server_ts_cursor_remainder = 0
        self._first_server_timestamp_us = None
        self._early_start_suspect = False
        self._has_reanchored = False
        self._insert_every_n_frames = 0
        self._drop_every_n_frames = 0
        self._frames_until_next_insert = 0
        self._frames_until_next_drop = 0
        self._last_output_frame = b""
        self._sync_error_filter.reset()
        self._sync_error_filtered_us = 0.0
        self._last_reanchor_loop_time_us = 0
        self._last_sync_error_log_us = 0
        self._frames_inserted_since_log = 0
        self._frames_dropped_since_log = 0
        self._callback_time_total_us = 0
        self._callback_count = 0

    def _audio_callback(  # noqa: PLR0915
        self,
        outdata: memoryview,
        frames: int,
        time: AudioTimeInfo,
        status: CallbackFlags,
    ) -> None:
        """
        Audio callback invoked by sounddevice when output buffer needs filling.

        Args:
            outdata: Output buffer to fill with audio data.
            frames: Number of frames requested.
            time: CFFI cdata structure with timing info (outputBufferDacTime, etc).
            status: Status flags (underrun, overflow, etc.).
        """
        callback_start_us = int(self._loop.time() * 1_000_000)

        assert self._format is not None

        bytes_needed = frames * self._format.frame_size
        output_buffer = memoryview(outdata).cast("B")

        if status:
            # Detect underflow and request re-anchor (processed by main thread)
            if status.input_underflow or status.output_underflow:
                logger.warning("Audio underflow detected; requesting re-anchor")
                self._clear_requested = True
                # Fill buffer with silence and return early to avoid glitches
                self._fill_silence(output_buffer, 0, bytes_needed)
                return
            logger.debug("Audio callback status: %s", status)

        # Capture exact DAC output time and update playback position
        self._update_playback_position_from_dac(time)
        bytes_written = 0

        try:
            # Pre-start gating: fill silence until scheduled start time
            if self._playback_state == PlaybackState.WAITING_FOR_START:
                bytes_written = self._handle_start_gating(
                    output_buffer, bytes_written, frames, time
                )

            # If still waiting after gating, fill remaining buffer with silence
            if self._playback_state == PlaybackState.WAITING_FOR_START:
                if bytes_written < bytes_needed:
                    silence_bytes = bytes_needed - bytes_written
                    self._fill_silence(output_buffer, bytes_written, silence_bytes)
                    bytes_written += silence_bytes
            else:
                frame_size = self._format.frame_size

                # Thread-safe snapshot of correction schedule (prevent mid-callback changes)
                insert_every_n = self._insert_every_n_frames
                drop_every_n = self._drop_every_n_frames

                # Fast path: no sync corrections needed - use bulk operations
                if insert_every_n == 0 and drop_every_n == 0:
                    # Bulk read all frames at once - 15-25x faster than frame-by-frame
                    frames_data = self._read_input_frames_bulk(frames)
                    frames_bytes = len(frames_data)
                    output_buffer[bytes_written : bytes_written + frames_bytes] = frames_data
                    bytes_written += frames_bytes
                else:
                    # Slow path: sync corrections active - process in optimized segments
                    # Reset cadence counters if needed
                    if self._frames_until_next_insert <= 0 and insert_every_n > 0:
                        self._frames_until_next_insert = insert_every_n
                    if self._frames_until_next_drop <= 0 and drop_every_n > 0:
                        self._frames_until_next_drop = drop_every_n

                    if not self._last_output_frame:
                        self._last_output_frame = b"\x00" * frame_size

                    insert_counter = self._frames_until_next_insert
                    drop_counter = self._frames_until_next_drop
                    frames_remaining = frames

                    while frames_remaining > 0:
                        # Calculate frames until next correction event
                        frames_until_insert = (
                            insert_counter if insert_every_n > 0 else frames_remaining + 1
                        )
                        frames_until_drop = (
                            drop_counter if drop_every_n > 0 else frames_remaining + 1
                        )

                        # Find next event and process segment before it
                        next_event_in = min(
                            frames_until_insert, frames_until_drop, frames_remaining
                        )

                        if next_event_in > 0:
                            # Bulk read segment of normal frames
                            segment_data = self._read_input_frames_bulk(next_event_in)
                            segment_bytes = len(segment_data)
                            output_buffer[bytes_written : bytes_written + segment_bytes] = (
                                segment_data
                            )
                            bytes_written += segment_bytes
                            frames_remaining -= next_event_in
                            insert_counter -= next_event_in
                            drop_counter -= next_event_in

                        # Handle correction event if at boundary
                        if frames_remaining > 0:
                            if drop_counter <= 0 and self._drop_every_n_frames > 0:
                                # Drop frame: read EXTRA frame to advance cursor faster
                                _ = self._read_one_input_frame()  # Read frame we're replacing
                                _ = self._read_one_input_frame()  # Read frame we're DROPPING
                                drop_counter = self._drop_every_n_frames
                                self._frames_dropped_since_log += 1
                                # Output last frame instead (don't output either frame we read)
                                output_buffer[bytes_written : bytes_written + frame_size] = (
                                    self._last_output_frame
                                )
                                bytes_written += frame_size
                                frames_remaining -= 1
                                insert_counter -= 1
                            elif insert_counter <= 0 and self._insert_every_n_frames > 0:
                                # Insert frame: output duplicate WITHOUT reading
                                # This makes playback catch up to cursor (cursor doesn't advance)
                                insert_counter = self._insert_every_n_frames
                                self._frames_inserted_since_log += 1
                                output_buffer[bytes_written : bytes_written + frame_size] = (
                                    self._last_output_frame
                                )
                                bytes_written += frame_size
                                frames_remaining -= 1
                                drop_counter -= 1

                    # Write cadence state back
                    self._frames_until_next_insert = insert_counter
                    self._frames_until_next_drop = drop_counter

        except Exception:
            logger.exception("Error in audio callback")
            # Fill rest with silence on error
            if bytes_written < bytes_needed:
                silence_bytes = bytes_needed - bytes_written
                output_buffer[bytes_written : bytes_written + silence_bytes] = (
                    b"\x00" * silence_bytes
                )
            # Reset partial chunk state on error
            self._current_chunk = None
            self._current_chunk_offset = 0

        # Apply volume scaling to the output
        self._apply_volume(output_buffer, bytes_needed)

        # Track callback execution time for performance monitoring
        callback_end_us = int(self._loop.time() * 1_000_000)
        self._callback_time_total_us += callback_end_us - callback_start_us
        self._callback_count += 1

    def _update_playback_position_from_dac(self, time: AudioTimeInfo) -> None:
        """Capture DAC and loop time simultaneously, update playback position.

        Note: loop.time() is thread-safe - it's a wrapper around time.monotonic(),
        which is a fast, thread-safe system call.
        """
        try:
            dac_time_us = int(time.outputBufferDacTime * 1_000_000)
            # Safe to call from audio callback thread - just calls time.monotonic()
            loop_time_us = int(self._loop.time() * 1_000_000)

            # Store complete calibration pair atomically
            self._dac_loop_calibrations.append((dac_time_us, loop_time_us))
            self._last_dac_calibration_time_us = loop_time_us

            # Update playback position in server time using latest calibration
            try:
                # Estimate the loop time that corresponds to the captured DAC time
                loop_at_dac_us = self._estimate_loop_time_for_dac_time(dac_time_us)
                if loop_at_dac_us == 0:
                    loop_at_dac_us = loop_time_us
                estimated_position = self._compute_server_time(loop_at_dac_us)
                self._last_known_playback_position_us = estimated_position
            except Exception:
                logger.exception("Failed to estimate playback position")

            # If we haven't set the DAC-anchored start yet, approximate it now
            if self._scheduled_start_dac_time_us is None and self._scheduled_start_loop_time_us:
                try:
                    loop_start = self._scheduled_start_loop_time_us
                    est_dac = self._estimate_dac_time_for_server_timestamp(
                        self._compute_server_time(loop_start)
                    )
                    if est_dac:
                        self._scheduled_start_dac_time_us = est_dac
                except Exception:
                    logger.exception("Failed to estimate DAC start time")
                    self._scheduled_start_dac_time_us = self._scheduled_start_loop_time_us

        except (AttributeError, TypeError):
            # time object may not have expected attributes in all backends
            logger.debug("Could not extract timing info from callback")

    def _initialize_current_chunk(self) -> None:
        """Load next chunk from queue and initialize read position.

        Updates server timestamp cursor if needed.
        """
        self._current_chunk = self._queue.get_nowait()
        self._current_chunk_offset = 0
        # Initialize server cursor if needed
        if self._server_ts_cursor_us == 0:
            self._server_ts_cursor_us = self._current_chunk.server_timestamp_us

    def _read_one_input_frame(self) -> bytes | None:
        """Read and consume a single audio frame from the queue.

        Returns frame bytes or None if no data available.
        Updates internal cursor and buffer duration when chunks are exhausted.
        """
        if self._format is None or self._format.frame_size == 0:
            return None

        frame_size = self._format.frame_size

        # Ensure we have a current chunk
        if self._current_chunk is None:
            if self._queue.empty():
                return None
            self._initialize_current_chunk()

        chunk = self._current_chunk
        assert chunk is not None
        data = chunk.audio_data
        if self._current_chunk_offset >= len(data):
            # Should not happen, but guard
            self._advance_finished_chunk()
            return None

        start = self._current_chunk_offset
        end = start + frame_size
        end = min(end, len(data))
        frame = data[start:end]

        # Advance offsets and timeline cursor
        self._current_chunk_offset = end
        self._advance_server_cursor_frames(1)

        # If chunk finished, advance and update buffered duration tracking
        if self._current_chunk_offset >= len(data):
            self._advance_finished_chunk()

        # Ensure full frame size by padding nulls if needed (shouldn't occur normally)
        if len(frame) < frame_size:
            frame = frame + b"\x00" * (frame_size - len(frame))
        return frame

    def _read_input_frames_bulk(self, n_frames: int) -> bytes:
        """Read N frames efficiently in bulk, handling chunk boundaries.

        Returns concatenated frame data. Much faster than calling
        _read_one_input_frame() N times due to reduced overhead.
        """
        if self._format is None or n_frames <= 0:
            return b""

        frame_size = self._format.frame_size
        total_bytes_needed = n_frames * frame_size
        result = bytearray(total_bytes_needed)
        bytes_written = 0

        while bytes_written < total_bytes_needed:
            # Get frames from current chunk
            if self._current_chunk is None:
                if self._queue.empty():
                    # No more data - pad with silence
                    silence_bytes = total_bytes_needed - bytes_written
                    result[bytes_written:] = b"\x00" * silence_bytes
                    break
                self._initialize_current_chunk()

            # Calculate how much we can read from current chunk
            assert self._current_chunk is not None
            chunk_data = self._current_chunk.audio_data
            available_bytes = len(chunk_data) - self._current_chunk_offset
            bytes_to_read = min(available_bytes, total_bytes_needed - bytes_written)

            # Bulk copy from chunk to result
            result[bytes_written : bytes_written + bytes_to_read] = chunk_data[
                self._current_chunk_offset : self._current_chunk_offset + bytes_to_read
            ]

            # Update state
            self._current_chunk_offset += bytes_to_read
            bytes_written += bytes_to_read
            frames_read = bytes_to_read // frame_size
            self._advance_server_cursor_frames(frames_read)

            # Check if chunk finished
            if self._current_chunk_offset >= len(chunk_data):
                self._advance_finished_chunk()

        # Save last frame for potential duplication
        if bytes_written >= frame_size:
            self._last_output_frame = bytes(result[bytes_written - frame_size : bytes_written])

        return bytes(result)

    def _advance_finished_chunk(self) -> None:
        """Update durations and state when current chunk is fully consumed."""
        assert self._format is not None
        if self._current_chunk is None:
            return
        data = self._current_chunk.audio_data
        chunk_frames = len(data) // self._format.frame_size
        chunk_duration_us = (chunk_frames * 1_000_000) // self._format.sample_rate
        self._queued_duration_us = max(0, self._queued_duration_us - chunk_duration_us)
        self._current_chunk = None
        self._current_chunk_offset = 0

    def _advance_server_cursor_frames(self, frames: int) -> None:
        """Advance server timeline cursor by a number of frames."""
        if self._format is None or frames <= 0:
            return
        # Accumulate microseconds precisely: add 1e6 per frame, carry by sample_rate
        self._server_ts_cursor_remainder += frames * 1_000_000
        sr = self._format.sample_rate
        if self._server_ts_cursor_remainder >= sr:
            inc_us = self._server_ts_cursor_remainder // sr
            self._server_ts_cursor_remainder = self._server_ts_cursor_remainder % sr
            self._server_ts_cursor_us += int(inc_us)

    def _skip_input_frames(self, frames_to_skip: int) -> None:
        """Discard frames from the input to reduce buffer depth quickly."""
        if self._format is None or frames_to_skip <= 0:
            return
        frame_size = self._format.frame_size
        while frames_to_skip > 0:
            if self._current_chunk is None:
                if self._queue.empty():
                    break
                self._current_chunk = self._queue.get_nowait()
                self._current_chunk_offset = 0
                if self._server_ts_cursor_us == 0:
                    self._server_ts_cursor_us = self._current_chunk.server_timestamp_us
            data = self._current_chunk.audio_data
            rem_bytes = len(data) - self._current_chunk_offset
            rem_frames = rem_bytes // frame_size
            if rem_frames <= 0:
                self._advance_finished_chunk()
                continue
            take = min(rem_frames, frames_to_skip)
            self._current_chunk_offset += take * frame_size
            self._advance_server_cursor_frames(take)
            frames_to_skip -= take
            if self._current_chunk_offset >= len(data):
                self._advance_finished_chunk()

    def _estimate_dac_time_for_server_timestamp(self, server_timestamp_us: int) -> int:
        """Estimate when a server timestamp will play out (in DAC time).

        Maps: server_ts → loop_time → dac_time
        """
        # Need at least one calibration point
        if self._last_dac_calibration_time_us == 0:
            return 0

        # Convert server timestamp to client loop time
        loop_time_us = self._compute_client_time(server_timestamp_us)

        # Find calibration point closest to this loop time
        if not self._dac_loop_calibrations:
            return 0

        # Use most recent calibration and previous one (if available) to estimate slope
        dac_ref_us, loop_ref_us = self._dac_loop_calibrations[-1]
        dac_prev_us, loop_prev_us = (0, 0)
        if len(self._dac_loop_calibrations) >= 2:
            dac_prev_us, loop_prev_us = self._dac_loop_calibrations[-2]

        if loop_ref_us == 0:
            # Calibration not yet filled in
            return 0

        # Estimate DAC-per-Loop slope if possible, else assume 1.0
        dac_per_loop = 1.0
        if loop_prev_us and dac_prev_us and (loop_ref_us != loop_prev_us):
            dac_per_loop = (dac_ref_us - dac_prev_us) / (loop_ref_us - loop_prev_us)
            # Clamp to sane bounds to avoid wild extrapolation
            dac_per_loop = max(self._DAC_PER_LOOP_MIN, min(self._DAC_PER_LOOP_MAX, dac_per_loop))

        return round(dac_ref_us + (loop_time_us - loop_ref_us) * dac_per_loop)

    def _estimate_loop_time_for_dac_time(self, dac_time_us: int) -> int:
        """Estimate loop time corresponding to a DAC time using recent calibrations."""
        if not self._dac_loop_calibrations:
            return 0
        dac_ref_us, loop_ref_us = self._dac_loop_calibrations[-1]
        if loop_ref_us == 0:
            return 0
        dac_prev_us, loop_prev_us = (0, 0)
        if len(self._dac_loop_calibrations) >= 2:
            dac_prev_us, loop_prev_us = self._dac_loop_calibrations[-2]
        loop_per_dac = 1.0
        if dac_prev_us and (dac_ref_us != dac_prev_us):
            loop_per_dac = (loop_ref_us - loop_prev_us) / (dac_ref_us - dac_prev_us)
            loop_per_dac = max(self._DAC_PER_LOOP_MIN, min(self._DAC_PER_LOOP_MAX, loop_per_dac))
        return round(loop_ref_us + (dac_time_us - dac_ref_us) * loop_per_dac)

    def _get_current_playback_position_us(self) -> int:
        """Get the current playback position in server timestamp space."""
        return self._last_known_playback_position_us

    def get_timing_metrics(self) -> dict[str, float]:
        """Return current timing metrics for monitoring."""
        return {
            "playback_position_us": float(self._get_current_playback_position_us()),
            "buffered_audio_us": float(self._queued_duration_us),
            "dac_samples_recorded": len(self._dac_loop_calibrations),
        }

    def _log_chunk_timing(self, _server_timestamp_us: int) -> None:
        """Log sync error and buffer status for debugging sync issues."""
        if self._sync_error_filter.is_synchronized:
            now_us = int(self._loop.time() * 1_000_000)
            if now_us - self._last_sync_error_log_us >= 1_000_000:
                self._last_sync_error_log_us = now_us
                # Calculate playback speed relative to source timeline.
                # Drops skip source frames (track advances faster), inserts repeat
                # frames (track advances slower). Reflect that in the speed metric.
                if self._format is not None:
                    expected_frames = self._format.sample_rate
                    track_frames = (
                        expected_frames
                        + self._frames_dropped_since_log
                        - self._frames_inserted_since_log
                    )
                    playback_speed_percent = (track_frames / expected_frames) * 100.0
                    # Distinct output frames rendered (for info):
                    normal_frames = (
                        expected_frames
                        - self._frames_dropped_since_log
                        + self._frames_inserted_since_log
                    )
                else:
                    playback_speed_percent = 100.0
                    normal_frames = 0

                # Calculate average callback execution time
                avg_callback_us = self._callback_time_total_us / max(self._callback_count, 1)

                logger.debug(
                    "Sync error: %.1f ms, buffer: %.2f s, speed: %.2f%%, "
                    "played: %d, inserted: %d, dropped: %d, callback: %.1f µs",
                    self._sync_error_filtered_us / 1000.0,
                    self._queued_duration_us / 1_000_000,
                    playback_speed_percent,
                    normal_frames,
                    self._frames_inserted_since_log,
                    self._frames_dropped_since_log,
                    avg_callback_us,
                )
                # Reset counters for next logging period
                self._frames_inserted_since_log = 0
                self._frames_dropped_since_log = 0
                self._callback_time_total_us = 0
                self._callback_count = 0

    def _smooth_sync_error(self, error_us: int) -> None:
        """Update Kalman filtered sync error to optimally track error and drift."""
        now_us = int(self._loop.time() * 1_000_000)
        # Use fixed max_error representing expected jitter/noise (5ms)
        max_error_us = 5_000
        self._sync_error_filter.update(
            measurement=error_us,
            max_error=max_error_us,
            time_added=now_us,
        )
        # Cache filtered offset for use in correction logic
        self._sync_error_filtered_us = self._sync_error_filter.offset

    def _fill_silence(self, output_buffer: memoryview, offset: int, num_bytes: int) -> None:
        """Fill output buffer range with silence."""
        if num_bytes > 0:
            output_buffer[offset : offset + num_bytes] = b"\x00" * num_bytes

    def _apply_volume(self, output_buffer: memoryview, num_bytes: int) -> None:
        """
        Apply volume scaling to the output buffer.

        Scales 16-bit audio samples by the current volume level.
        """
        muted = self._muted
        volume = self._volume

        if muted or volume == 0:
            # Fill with silence
            output_buffer[:num_bytes] = b"\x00" * num_bytes
            return

        if volume == 100:
            return

        # Create view of buffer as int16 samples (no copy)
        samples = np.frombuffer(output_buffer[:num_bytes], dtype=np.int16).copy()
        # Power curve for natural volume control (gentler at high volumes)
        amplitude = (volume / 100.0) ** 1.5
        samples = (samples * amplitude).astype(np.int16)
        # Write back to buffer
        output_buffer[:num_bytes] = samples.tobytes()

    def _compute_and_set_loop_start(self, server_timestamp_us: int) -> None:
        """Compute and set scheduled start time from server timestamp."""
        try:
            self._scheduled_start_loop_time_us = self._compute_client_time(server_timestamp_us)
        except Exception:
            logger.exception("Failed to compute client time for start")
            self._scheduled_start_loop_time_us = int(
                self._loop.time() * self._MICROSECONDS_PER_SECOND
            )

    def _handle_start_gating(
        self,
        output_buffer: memoryview,
        bytes_written: int,
        frames: int,
        time: AudioTimeInfo | None = None,
    ) -> int:
        """Handle pre-start gating using DAC or loop time. Returns bytes written."""
        assert self._format is not None

        # Try DAC-based gating first if time info available
        use_dac_gating = False
        dac_now_us = 0
        if time is not None and self._scheduled_start_dac_time_us is not None:
            try:
                dac_now_us = int(time.outputBufferDacTime * self._MICROSECONDS_PER_SECOND)
                if dac_now_us > 0:
                    use_dac_gating = True
            except (AttributeError, TypeError):
                pass

        if use_dac_gating:
            # DAC-based gating: precise hardware timing
            assert self._scheduled_start_dac_time_us is not None
            delta_us = self._scheduled_start_dac_time_us - dac_now_us
            target_time_us = self._scheduled_start_dac_time_us
            current_time_us = dac_now_us
            can_drop_frames = True  # DAC gating allows frame dropping when late
        elif self._scheduled_start_loop_time_us is not None:
            # Loop-based gating: fallback when DAC timing unavailable
            loop_now_us = int(self._loop.time() * self._MICROSECONDS_PER_SECOND)
            delta_us = self._scheduled_start_loop_time_us - loop_now_us
            target_time_us = self._scheduled_start_loop_time_us
            current_time_us = loop_now_us
            can_drop_frames = False  # Loop gating waits for DAC calibration
        else:
            return bytes_written

        if delta_us > 0:
            # Not yet time to start: fill with silence
            frames_until_start = int(
                (delta_us * self._format.sample_rate + 999_999) // self._MICROSECONDS_PER_SECOND
            )
            frames_to_silence = min(frames_until_start, frames)
            silence_bytes = frames_to_silence * self._format.frame_size
            self._fill_silence(output_buffer, bytes_written, silence_bytes)
            bytes_written += silence_bytes
        elif delta_us < 0 and can_drop_frames:
            # Late: fast-forward by dropping input frames (DAC gating only)
            if not (self._early_start_suspect and not self._has_reanchored):
                frames_to_drop = int(
                    ((-delta_us) * self._format.sample_rate + 999_999)
                    // self._MICROSECONDS_PER_SECOND
                )
                self._skip_input_frames(frames_to_drop)
                self._playback_state = PlaybackState.PLAYING

        # If we've reached/overrun the scheduled time, arm playback
        if current_time_us >= target_time_us:
            self._playback_state = PlaybackState.PLAYING

        return bytes_written

    def _update_correction_schedule(self, error_us: int) -> None:
        """Plan occasional sample drop/insert to correct sync error.

        Uses simple proportional control: correction rate is proportional to error.
        The feedback loop naturally handles both clock drift and accumulated error.

        Positive error means DAC/server playback is ahead of our read cursor;
        schedule drops to catch up. Negative error means we're ahead; schedule
        inserts to slow down. Large errors trigger re-anchoring instead of
        aggressive correction to avoid artifacts.
        """
        if self._format is None or self._format.sample_rate <= 0:
            return

        # Smooth the error to avoid reacting to jitter
        self._smooth_sync_error(error_us)

        abs_err = abs(self._sync_error_filtered_us)

        # Do nothing within deadband
        if abs_err <= self._CORRECTION_DEADBAND_US:
            self._insert_every_n_frames = 0
            self._drop_every_n_frames = 0
            return

        # Re-anchor only if error is very large and cooldown has elapsed
        now_loop_us = int(self._loop.time() * 1_000_000)
        if (
            abs_err > self._REANCHOR_THRESHOLD_US
            and self._playback_state == PlaybackState.PLAYING
            and now_loop_us - self._last_reanchor_loop_time_us > self._REANCHOR_COOLDOWN_US
        ):
            logger.info("Sync error %.1f ms too large; re-anchoring", abs_err / 1000.0)
            # Reset cadence
            self._insert_every_n_frames = 0
            self._drop_every_n_frames = 0
            self._frames_until_next_insert = 0
            self._frames_until_next_drop = 0
            self._last_reanchor_loop_time_us = now_loop_us
            # Re-anchor on next chunk boundary by clearing queue
            self.clear()
            return

        # Simple proportional control: correction rate proportional to error
        # Target is to fix error within _CORRECTION_TARGET_SECONDS
        frames_error = abs_err * self._format.sample_rate / 1_000_000.0
        desired_corrections_per_sec = frames_error / self._CORRECTION_TARGET_SECONDS

        # Cap at maximum allowed correction rate (4%)
        max_corrections_per_sec = self._format.sample_rate * self._MAX_SPEED_CORRECTION
        corrections_per_sec = min(desired_corrections_per_sec, max_corrections_per_sec)

        # Convert to interval between corrections
        if corrections_per_sec > 0:
            interval_frames = int(self._format.sample_rate / corrections_per_sec)
            interval_frames = max(interval_frames, 1)
        else:
            interval_frames = int(1.0 / max(self._MAX_SPEED_CORRECTION, 0.001))

        # Determine direction based on sign of sync error
        if self._sync_error_filtered_us > 0:
            # We are behind (DAC ahead) -> drop to catch up
            self._drop_every_n_frames = interval_frames
            self._insert_every_n_frames = 0
        else:
            # We are ahead -> insert to slow down
            self._insert_every_n_frames = interval_frames
            self._drop_every_n_frames = 0

    def submit(self, server_timestamp_us: int, payload: bytes) -> None:  # noqa: PLR0915
        """
        Queue an audio payload for playback, intelligently handling gaps and overlaps.

        Fills gaps with silence and trims overlaps to ensure a continuous stream.

        Args:
            server_timestamp_us: Server timestamp when this audio should play.
            payload: Raw PCM audio bytes.
        """
        # Handle deferred operations from audio thread
        if self._clear_requested:
            self._clear_requested = False
            self.clear()
            logger.info("Cleared audio queue after underflow (deferred from audio thread)")

        if self._format is None:
            logger.debug("Audio format missing; dropping audio chunk")
            return
        if self._format.frame_size == 0:
            return
        if len(payload) % self._format.frame_size != 0:
            logger.warning(
                "Dropping audio chunk with invalid size: %s bytes (frame size %s)",
                len(payload),
                self._format.frame_size,
            )
            return

        now_us = int(self._loop.time() * 1_000_000)

        # On first real chunk, schedule start time aligned to server timeline
        if self._scheduled_start_loop_time_us is None:
            self._compute_and_set_loop_start(server_timestamp_us)
            # Best-effort DAC schedule; refined later as calibrations accumulate
            est_dac = self._estimate_dac_time_for_server_timestamp(server_timestamp_us)
            # Only set DAC time when we can estimate it; otherwise use loop-based gating
            self._scheduled_start_dac_time_us = est_dac if est_dac else None
            self._playback_state = PlaybackState.WAITING_FOR_START
            self._first_server_timestamp_us = server_timestamp_us
            # If scheduled start is very near now, suspect unsynchronized fallback mapping
            # Cast: we just set this via _compute_and_set_loop_start so it's not None
            scheduled_start = cast("int", self._scheduled_start_loop_time_us)
            if scheduled_start - now_us <= self._EARLY_START_THRESHOLD_US:
                self._early_start_suspect = True

        # While waiting to start, keep the scheduled loop start updated as time sync improves
        elif (
            self._playback_state == PlaybackState.WAITING_FOR_START
            and self._first_server_timestamp_us is not None
        ):
            try:
                updated_loop_start = self._compute_client_time(self._first_server_timestamp_us)
                # Only update if it moves significantly to avoid churn
                if (
                    abs(updated_loop_start - (self._scheduled_start_loop_time_us or 0))
                    > self._START_TIME_UPDATE_THRESHOLD_US
                ):
                    self._scheduled_start_loop_time_us = updated_loop_start
                    est_dac = self._estimate_dac_time_for_server_timestamp(
                        self._first_server_timestamp_us
                    )
                    self._scheduled_start_dac_time_us = est_dac if est_dac else None
            except Exception:
                logger.exception("Failed to update start time")

        # After calibration, if we have both a DAC-derived playback position and a
        # server-timeline cursor, compute sync error and schedule micro-corrections.
        # Only compute sync error when actively playing (not during initial buffering)
        if (
            self._playback_state == PlaybackState.PLAYING
            and self._last_known_playback_position_us > 0
            and self._server_ts_cursor_us > 0
        ):
            sync_error_us = self._last_known_playback_position_us - self._server_ts_cursor_us
            self._update_correction_schedule(sync_error_us)

        # Log timing information (verbose, for debugging latency issues)
        self._log_chunk_timing(server_timestamp_us)

        # Initialize expected next timestamp on first chunk
        if self._expected_next_timestamp is None:
            self._expected_next_timestamp = server_timestamp_us
        # Handle gap: insert silence to fill the gap
        elif server_timestamp_us > self._expected_next_timestamp:
            gap_us = server_timestamp_us - self._expected_next_timestamp
            gap_frames = (gap_us * self._format.sample_rate) // 1_000_000
            silence_bytes = gap_frames * self._format.frame_size
            silence = b"\x00" * silence_bytes
            self._queue.put_nowait(
                _QueuedChunk(
                    server_timestamp_us=self._expected_next_timestamp,
                    audio_data=silence,
                )
            )
            # Account for inserted silence in buffer duration
            silence_duration_us = (gap_frames * 1_000_000) // self._format.sample_rate
            self._queued_duration_us += silence_duration_us
            logger.debug(
                "Gap: %.1f ms filled with silence",
                gap_us / 1000.0,
            )
            self._expected_next_timestamp = server_timestamp_us

        # Handle overlap: trim the start of the chunk
        elif server_timestamp_us < self._expected_next_timestamp:
            overlap_us = self._expected_next_timestamp - server_timestamp_us
            overlap_frames = (overlap_us * self._format.sample_rate) // 1_000_000
            trim_bytes = overlap_frames * self._format.frame_size
            if trim_bytes < len(payload):
                payload = payload[trim_bytes:]
                server_timestamp_us = self._expected_next_timestamp
                logger.debug(
                    "Overlap: %.1f ms trimmed",
                    overlap_us / 1000.0,
                )
            else:
                # Entire chunk is overlap, skip it
                logger.debug(
                    "Overlap: %.1f ms (chunk skipped, already played)",
                    overlap_us / 1000.0,
                )
                return

        # Queue the chunk
        chunk_duration_us = 0
        if len(payload) > 0:
            # Compute duration from the post-trim payload
            chunk_frames = len(payload) // self._format.frame_size
            chunk_duration_us = (chunk_frames * 1_000_000) // self._format.sample_rate
            chunk = _QueuedChunk(
                server_timestamp_us=server_timestamp_us,
                audio_data=payload,
            )
            self._queue.put_nowait(chunk)
            # Track duration of queued audio
            self._queued_duration_us += chunk_duration_us
            # Update expected position for next chunk
            self._expected_next_timestamp = server_timestamp_us + chunk_duration_us

        # Start stream immediately when first chunk arrives
        if not self._stream_started and self._queue.qsize() > 0 and self._stream is not None:
            self._stream.start()
            self._stream_started = True
            logger.info(
                "Stream STARTED: %d chunks, %.2f seconds buffered",
                self._queue.qsize(),
                self._queued_duration_us / 1_000_000,
            )

    def _close_stream(self) -> None:
        """Close the audio output stream."""
        stream = self._stream
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                logger.exception("Failed to close audio output stream")
        self._stream = None


class SyncCalibrator:
    """Experimental sync calibrator using cross-correlation.

    Captures audio from a microphone and compares it against the expected audio
    (received from the server) using cross-correlation to determine timing offset.
    This can be used to calibrate other devices playing the audio.
    """

    _WINDOW_SECONDS: Final[float] = 1.0
    """Cross-correlation window size in seconds (should be >= chirp duration)."""
    _REPORT_INTERVAL_SECONDS: Final[float] = 1.0
    """How often to report the measured offset."""
    _MAX_LAG_MS: Final[float] = 250.0
    """How far back to look in reference buffer (accounts for audio arriving early)."""
    _GCC_PHAT_EPS: Final[float] = 1e-10
    """Small epsilon to avoid division by zero in GCC-PHAT."""

    def __init__(
        self,
        sample_rate: int = 48000,
        channels: int = 2,
        mic_device: int | None = None,
        compute_server_time: Callable[[int], int] | None = None,
    ) -> None:
        """Initialize the sync calibrator.

        Args:
            sample_rate: Audio sample rate (must match stream).
            channels: Number of audio channels.
            mic_device: Microphone device ID. None for default.
            compute_server_time: Function to convert loop time (us) to server time (us).
        """
        self._sample_rate = sample_rate
        self._channels = channels
        self._mic_device = mic_device
        self._compute_server_time = compute_server_time

        # Buffer sizes - 10 seconds should be plenty
        self._buffer_duration_seconds = 10.0
        self._window_samples = int(self._WINDOW_SECONDS * sample_rate)
        self._buffer_samples = int(self._buffer_duration_seconds * sample_rate)

        # Ring buffer for reference (expected) audio with timestamp tracking
        # We track timestamp at position 0 and update it as we overwrite
        self._reference_buffer = np.zeros(self._buffer_samples, dtype=np.float32)
        self._reference_write_pos = 0
        self._reference_pos0_timestamp_us: int | None = (
            None  # Server playback timestamp at buffer position 0
        )

        # Ring buffer for captured (mic) audio with timestamp tracking
        self._capture_buffer = np.zeros(self._buffer_samples, dtype=np.float32)
        self._capture_write_pos = 0
        self._capture_pos0_time_us: int | None = None  # Loop time at buffer position 0

        # Mic input stream
        self._mic_stream: sounddevice.InputStream | None = None
        self._started = False

        # Timing for reports
        self._last_report_time = 0.0

        # Smoothed offset tracking
        self._smoothed_offset_ms: float | None = None

        # Accumulated confidence per offset (rounded to nearest ms)
        # Maps offset_ms (int) -> accumulated confidence
        self._accumulated_confidence: dict[int, float] = {}
        self._confidence_decay: float = 0.9  # Decay factor per measurement

        # Start time for elapsed time logging
        self._start_time: float = 0.0

        # Drift tracking - store (elapsed_s, best_offset_ms) for linear regression
        self._drift_history: list[tuple[float, float]] = []
        self._max_drift_history: int = 50  # Keep last N measurements

        # Mic sample rate diagnostic - track actual vs expected sample rate
        self._total_mic_samples: int = 0
        self._mic_start_time: float | None = None

        # Warmup baseline for accurate rate calculation (skip startup noise)
        self._warmup_complete: bool = False
        self._warmup_baseline_time: float | None = None
        self._warmup_baseline_samples: int = 0
        self._WARMUP_SECONDS: float = 30.0  # Wait this long before establishing baseline

        # Sliding window for empirical capture rate (more accurate than cumulative average)
        # Store (monotonic_time, sample_count) pairs
        self._capture_rate_history: collections.deque[tuple[float, int]] = collections.deque(
            maxlen=100  # ~20 seconds of history at 5 samples/sec
        )

    def get_histogram_data(self) -> tuple[dict[int, float], int | None, float]:
        """Get histogram data for UI display.

        Returns:
            Tuple of (confidence_dict, best_offset_ms, elapsed_seconds):
            - confidence_dict: Maps offset_ms (int) to accumulated confidence (float)
            - best_offset_ms: The offset with highest confidence, or None if no data
            - elapsed_seconds: Time since calibration started
        """
        elapsed = time_module.monotonic() - self._start_time if self._start_time > 0 else 0.0

        if not self._accumulated_confidence:
            return {}, None, elapsed

        # Find best offset
        best_offset = max(self._accumulated_confidence.items(), key=lambda x: x[1])[0]

        return dict(self._accumulated_confidence), best_offset, elapsed

    def start(self) -> None:
        """Start the microphone capture stream."""
        if self._started:
            return

        self._mic_stream = sounddevice.InputStream(
            samplerate=self._sample_rate,
            channels=1,  # Capture mono for simplicity
            dtype="float32",
            blocksize=2048,
            callback=self._mic_callback,
            device=self._mic_device,
        )
        self._mic_stream.start()
        self._started = True
        self._start_time = time_module.monotonic()  # Track start time for timestamps
        actual_mic_rate = self._mic_stream.samplerate
        print(  # noqa: T201
            f"[Calibrator] Requested sample_rate={self._sample_rate}, "
            f"actual mic sample_rate={actual_mic_rate}"
        )
        logger.info(
            "Sync calibrator started: mic_device=%s, sample_rate=%d, actual_mic_rate=%s",
            self._mic_device,
            self._sample_rate,
            actual_mic_rate,
        )

    def stop(self) -> None:
        """Stop the microphone capture stream."""
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                logger.exception("Failed to close mic stream")
            self._mic_stream = None
        self._started = False

    def reset_buffers(self) -> None:
        """Reset ring buffers and accumulated state (call on song/stream change)."""
        # Clear reference buffer state
        self._reference_buffer.fill(0)
        self._reference_write_pos = 0
        self._reference_pos0_timestamp_us = None
        self._ref_newest_server_timestamp_us: int | None = None
        self._ref_total_samples = 0
        self._buffer_ahead_samples: int | None = None

        # Clear capture buffer state
        self._capture_buffer.fill(0)
        self._capture_write_pos = 0
        self._capture_pos0_time_us = None
        self._cap_total_samples = 0
        self._cap_newest_loop_time_us: int | None = None

        # Clear accumulated confidence
        self._accumulated_confidence.clear()

        # Reset timing
        self._last_report_time = 0.0
        self._start_time = time_module.monotonic()
        self._chunk_count = 0

        # Reset mic sample rate tracking
        self._total_mic_samples = 0
        self._mic_start_time = None
        self._capture_rate_history.clear()

        # Reset warmup state
        self._warmup_complete = False
        self._warmup_baseline_time = None
        self._warmup_baseline_samples = 0

        print("[Calibrator] Buffers reset (stream change)")  # noqa: T201

    def _mic_callback(
        self,
        indata: np.ndarray,
        _frames: int,
        time_info: CDataTimeInfo,
        status: CallbackFlags,
    ) -> None:
        """Handle microphone input data."""
        if status:
            logger.debug("Mic callback status: %s", status)

        # Track actual mic sample rate vs monotonic clock
        now = time_module.monotonic()
        if self._mic_start_time is None:
            self._mic_start_time = now
        self._total_mic_samples += len(indata)

        # Record sample count for sliding window rate calculation (~5 times/sec)
        if self._total_mic_samples % (self._sample_rate // 5) < len(indata):
            self._capture_rate_history.append((now, self._total_mic_samples))

        elapsed = now - self._mic_start_time

        # Establish baseline after warmup period (skip startup noise)
        if not self._warmup_complete and elapsed >= self._WARMUP_SECONDS:
            self._warmup_complete = True
            self._warmup_baseline_time = now
            self._warmup_baseline_samples = self._total_mic_samples
            print(f"[Calibrator] Warmup complete, baseline established at {elapsed:.1f}s")  # noqa: T201

        # Calculate empirical rate from baseline (if available) or cumulative
        if self._warmup_complete and self._warmup_baseline_time is not None:
            time_since_baseline = now - self._warmup_baseline_time
            samples_since_baseline = self._total_mic_samples - self._warmup_baseline_samples
            if time_since_baseline > 1.0:
                empirical_rate = samples_since_baseline / time_since_baseline
            else:
                empirical_rate = self._sample_rate
        elif elapsed > 1.0:
            empirical_rate = self._total_mic_samples / elapsed
        else:
            empirical_rate = self._sample_rate

        if elapsed > 0:
            actual_rate = self._total_mic_samples / elapsed
            # Print every ~10 seconds
            if self._total_mic_samples % (self._sample_rate * 10) < len(indata):
                if self._warmup_complete:
                    print(  # noqa: T201
                        f"[Calibrator] Mic rate: cumulative={actual_rate:.2f} Hz, "
                        f"post-warmup={empirical_rate:.2f} Hz (diff: {empirical_rate - self._sample_rate:+.2f})"
                    )
                else:
                    print(  # noqa: T201
                        f"[Calibrator] Mic rate: {actual_rate:.2f} Hz "
                        f"(warmup {elapsed:.0f}/{self._WARMUP_SECONDS:.0f}s)"
                    )

        # Get loop time for this chunk
        loop_time_us = round(now * 1_000_000.0)
        # Use empirical sample rate for chunk duration
        chunk_duration_us = round(len(indata) * 1_000_000.0 / empirical_rate)

        # Compute capture time using ADC time if available
        # ADC time = when samples were captured (stream time)
        # currentTime = when callback fired (stream time)
        # The difference is the input latency
        try:
            adc_time = time_info.inputBufferAdcTime
            current_time = time_info.currentTime
            if adc_time > 0 and current_time > 0:
                latency_us = round((current_time - adc_time) * 1_000_000.0)
                capture_time_us = loop_time_us - latency_us
            else:
                # Fallback: callback time minus chunk duration
                capture_time_us = loop_time_us - chunk_duration_us
        except (AttributeError, TypeError, ValueError):
            # No timing info available
            capture_time_us = loop_time_us - chunk_duration_us

        # Convert to mono float32 if needed (should already be mono float32)
        mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()

        # Initialize timestamp at position 0 on first chunk
        if self._capture_pos0_time_us is None:
            self._capture_pos0_time_us = capture_time_us

        # Write to ring buffer, tracking when we overwrite position 0
        samples_to_write = len(mono)
        space_at_end = self._buffer_samples - self._capture_write_pos

        if samples_to_write <= space_at_end:
            self._capture_buffer[
                self._capture_write_pos : self._capture_write_pos + samples_to_write
            ] = mono
        else:
            # Wrap around - we're overwriting position 0
            self._capture_buffer[self._capture_write_pos :] = mono[:space_at_end]
            self._capture_buffer[: samples_to_write - space_at_end] = mono[space_at_end:]
            # Calculate new pos0 time based on where in the new cycle we are
            # capture_time_us is start of THIS chunk, which wrote samples_into_new_cycle
            # samples at the start of the buffer
            samples_into_new_cycle = samples_to_write - space_at_end
            # Use empirical rate for consistency with chunk_duration_us
            time_into_new_cycle_us = (
                round(samples_into_new_cycle * 1_000_000.0 / empirical_rate)
                if elapsed > 1.0
                else round(samples_into_new_cycle * 1_000_000.0 / self._sample_rate)
            )
            self._capture_pos0_time_us = (
                capture_time_us + chunk_duration_us - time_into_new_cycle_us
            )

        self._capture_write_pos = (
            self._capture_write_pos + samples_to_write
        ) % self._buffer_samples

        # Track total samples for buffer fullness check
        if not hasattr(self, "_cap_total_samples"):
            self._cap_total_samples = 0
        self._cap_total_samples += samples_to_write

        # Track loop_time of newest sample (for time-based extraction)
        self._cap_newest_loop_time_us = capture_time_us + chunk_duration_us

    def submit_reference_audio(
        self, server_timestamp_us: int, audio_data: bytes, channels: int
    ) -> None:
        """Submit reference audio (what we expect to hear).

        Args:
            server_timestamp_us: Server timestamp when this audio should play.
            audio_data: Raw PCM int16 audio bytes.
            channels: Number of channels in the audio data.
        """
        if not self._started:
            return

        # Convert int16 PCM to float32 mono
        samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

        # Convert to mono using left channel only
        if channels > 1:
            samples = samples.reshape(-1, channels)[:, 0]

        chunk_duration_us = round(len(samples) * 1_000_000.0 / self._sample_rate)

        # Use current loop time converted to server time as the timestamp basis
        # This ensures the reference buffer uses the same time reference as capture buffer
        # (both track when audio ARRIVES, not when it should PLAY)
        if self._compute_server_time is None:
            return

        loop_time_now = round(time_module.monotonic() * 1_000_000.0)
        # The chunk we're receiving now corresponds to audio that arrived at loop_time_now
        # We use compute_server_time to get the equivalent server time
        arrival_server_time_us = self._compute_server_time(loop_time_now)

        # Track buffer_ahead for debugging (how far ahead server_timestamp is vs arrival time)
        buffer_ahead_ms = (server_timestamp_us - arrival_server_time_us) / 1000.0
        if not hasattr(self, "_chunk_count"):
            self._chunk_count = 0
        self._chunk_count += 1
        if self._chunk_count % 500 == 1:  # ~every 10 seconds at 2048 samples/chunk
            print(f"[Calibrator] buffer_ahead={buffer_ahead_ms:+.1f}ms")  # noqa: T201

        # Initialize timestamp at position 0 on first chunk (use playback time)
        if self._reference_pos0_timestamp_us is None:
            self._reference_pos0_timestamp_us = server_timestamp_us

        # Write to ring buffer, tracking when we overwrite position 0
        samples_to_write = len(samples)
        space_at_end = self._buffer_samples - self._reference_write_pos

        if samples_to_write <= space_at_end:
            self._reference_buffer[
                self._reference_write_pos : self._reference_write_pos + samples_to_write
            ] = samples
        else:
            # Wrap around - we're overwriting position 0
            self._reference_buffer[self._reference_write_pos :] = samples[:space_at_end]
            self._reference_buffer[: samples_to_write - space_at_end] = samples[space_at_end:]
            # Calculate new pos0 time based on playback time
            samples_into_new_cycle = samples_to_write - space_at_end
            time_into_new_cycle_us = round(samples_into_new_cycle * 1_000_000.0 / self._sample_rate)
            self._reference_pos0_timestamp_us = (
                server_timestamp_us + chunk_duration_us - time_into_new_cycle_us
            )

        self._reference_write_pos = (
            self._reference_write_pos + samples_to_write
        ) % self._buffer_samples

        # Track total samples for buffer fullness check
        if not hasattr(self, "_ref_total_samples"):
            self._ref_total_samples = 0
        self._ref_total_samples += samples_to_write

        # Track playback time of newest sample (right before write_pos)
        # server_timestamp_us = when first sample of chunk plays
        # + chunk_duration_us = when last sample of chunk plays
        self._ref_newest_server_timestamp_us = server_timestamp_us + chunk_duration_us

        # Periodically compute and report offset
        self._maybe_report_offset()

    def _maybe_report_offset(self) -> None:
        """Compute and print offset if enough time has passed."""
        now = time_module.monotonic()
        if now - self._last_report_time < self._REPORT_INTERVAL_SECONDS:
            return

        # Need compute_server_time for timestamp conversion
        if self._compute_server_time is None:
            return

        # Need enough data in both buffers
        safety_samples = int(0.1 * self._sample_rate)  # 100ms safety margin
        min_samples_needed = self._window_samples + safety_samples

        if not hasattr(self, "_ref_total_samples"):
            self._ref_total_samples = 0
        if not hasattr(self, "_cap_total_samples"):
            self._cap_total_samples = 0

        if self._ref_total_samples < min_samples_needed:
            return
        if self._cap_total_samples < min_samples_needed:
            return

        # Need capture buffer timestamp to determine target time
        if not hasattr(self, "_cap_newest_loop_time_us") or self._cap_newest_loop_time_us is None:
            return

        self._last_report_time = now

        # Extract based on TIME, not write_pos directly.
        # The two buffers advance at different rates (different clocks), so we
        # calculate positions based on timestamps and work backwards from write_pos.
        #
        # We track:
        # - _ref_newest_server_timestamp_us: playback time of newest sample (at write_pos - 1)
        # - _cap_newest_loop_time_us: loop_time of newest sample in capture buffer
        #
        # For extraction:
        # - Pick a target server_time T (what audio should have been playing)
        # - Find reference position: samples_ago = (newest_timestamp - T) * sample_rate
        # - Find capture position: samples_ago = (newest_loop_time - target_loop_time) * sample_rate

        # Target time: what audio should have been playing at (now - safety)?
        # We use capture buffer's newest_loop_time as the reference for "now"
        # Safety margin must be > window/2 since we extract centered on the target
        safety_time_us = round((self._WINDOW_SECONDS / 2 + 0.5) * 1_000_000.0)  # window/2 + 0.5s
        target_loop_time_us = self._cap_newest_loop_time_us - safety_time_us
        target_server_time_us = self._compute_server_time(target_loop_time_us)

        # REFERENCE BUFFER: Find position by calculating backwards from write_pos
        if not hasattr(self, "_ref_newest_server_timestamp_us"):
            return

        if self._ref_newest_server_timestamp_us is None:
            return

        # How many samples back from newest sample?
        ref_time_ago_us = self._ref_newest_server_timestamp_us - target_server_time_us
        ref_samples_ago = round(ref_time_ago_us * self._sample_rate / 1_000_000.0)

        # Check bounds - ensure target is within valid buffer region
        if ref_samples_ago < self._window_samples // 2:
            return  # Too recent - not enough samples after target
        if ref_samples_ago > self._buffer_samples - self._window_samples // 2:
            return  # Too old - data has been overwritten

        ref_center = (self._reference_write_pos - ref_samples_ago) % self._buffer_samples
        ref_start = (ref_center - self._window_samples // 2) % self._buffer_samples
        ref_end = (ref_start + self._window_samples) % self._buffer_samples

        if ref_start < ref_end:
            reference = self._reference_buffer[ref_start:ref_end].copy()
        else:
            reference = np.concatenate(
                [self._reference_buffer[ref_start:], self._reference_buffer[:ref_end]]
            )

        # CAPTURE BUFFER: Find position for target_loop_time
        # Use empirical sample rate to compensate for audio hardware clock drift
        cap_time_ago_us = self._cap_newest_loop_time_us - target_loop_time_us

        # Calculate empirical rate from post-warmup baseline (more accurate than cumulative)
        empirical_capture_rate: float = self._sample_rate  # Default fallback
        if self._warmup_complete and self._warmup_baseline_time is not None:
            now = time_module.monotonic()
            time_since_baseline = now - self._warmup_baseline_time
            samples_since_baseline = self._total_mic_samples - self._warmup_baseline_samples
            if time_since_baseline > 1.0:
                empirical_capture_rate = samples_since_baseline / time_since_baseline
        elif self._mic_start_time is not None and self._total_mic_samples > 0:
            elapsed = time_module.monotonic() - self._mic_start_time
            if elapsed > 1.0:
                empirical_capture_rate = self._total_mic_samples / elapsed

        cap_samples_ago = round(cap_time_ago_us * empirical_capture_rate / 1_000_000.0)

        # Check bounds
        if cap_samples_ago < self._window_samples // 2:
            return  # Too recent
        if cap_samples_ago > self._buffer_samples - self._window_samples // 2:
            return  # Too old

        cap_center = (self._capture_write_pos - cap_samples_ago) % self._buffer_samples
        cap_start = (cap_center - self._window_samples // 2) % self._buffer_samples
        cap_end = (cap_start + self._window_samples) % self._buffer_samples

        if cap_start < cap_end:
            captured = self._capture_buffer[cap_start:cap_end].copy()
        else:
            captured = np.concatenate(
                [self._capture_buffer[cap_start:], self._capture_buffer[:cap_end]]
            )

        # Check for sufficient signal level
        ref_std = np.std(reference)
        cap_std = np.std(captured)
        if ref_std < 1e-6 or cap_std < 1e-6:
            print("[Calibrator] Insufficient signal level for correlation")  # noqa: T201
            return

        # Remove DC bias (GCC-PHAT handles amplitude normalization via phase transform)
        reference = reference - np.mean(reference)
        captured = captured - np.mean(captured)

        # GCC-PHAT (Generalized Cross-Correlation with Phase Transform)
        # More robust to reverberation/noise than basic cross-correlation
        # Pad to next power of 2 for FFT efficiency and to avoid circular wrap-around
        n_samples = len(reference)
        fft_size = 2 ** int(np.ceil(np.log2(2 * n_samples - 1)))

        # FFT both signals with zero-padding
        ref_fft = np.fft.rfft(reference, n=fft_size)
        cap_fft = np.fft.rfft(captured, n=fft_size)

        # Cross-power spectrum
        cross_spectrum = cap_fft * np.conj(ref_fft)

        # PHAT weighting: normalize by magnitude (whitening)
        magnitude = np.abs(cross_spectrum)
        # Add small epsilon to avoid division by zero
        phat_spectrum = cross_spectrum / (magnitude + self._GCC_PHAT_EPS)

        # IFFT to get correlation in time domain
        correlation_full = np.fft.irfft(phat_spectrum)

        # Extract the valid correlation range (equivalent to "full" mode)
        # irfft gives us circular correlation, we need linear correlation
        correlation = np.concatenate(
            [correlation_full[-(n_samples - 1) :], correlation_full[:n_samples]]
        )

        # Limit search range to ±_MAX_LAG_MS
        max_lag_samples = int(self._MAX_LAG_MS * self._sample_rate / 1000.0)
        center_idx = n_samples - 1  # Zero-lag index in correlation array
        search_start = max(0, center_idx - max_lag_samples)
        search_end = min(len(correlation), center_idx + max_lag_samples + 1)

        # Find peak within limited range
        search_region = np.abs(correlation[search_start:search_end])
        mean_val = np.mean(search_region)

        # Find top N peaks using local maxima detection
        # A local maximum is where the value is greater than both neighbors
        num_peaks_to_show = 5
        peaks: list[tuple[float, float, float]] = []  # (lag_ms, value, confidence)

        for i in range(1, len(search_region) - 1):
            if search_region[i] > search_region[i - 1] and search_region[i] > search_region[i + 1]:
                peak_idx = search_start + i
                lag_samples = center_idx - peak_idx
                lag_ms_candidate = lag_samples * 1000.0 / self._sample_rate
                peak_confidence = search_region[i] / mean_val if mean_val > 0 else 0
                peaks.append((lag_ms_candidate, search_region[i], peak_confidence))

        # Sort by value (descending) and take top N
        peaks.sort(key=lambda x: x[1], reverse=True)
        top_peaks = peaks[:num_peaks_to_show]

        if not top_peaks:
            print("[Calibrator] No peaks found in correlation")  # noqa: T201
            return

        # Apply decay to all accumulated confidences
        for offset_ms in self._accumulated_confidence:
            self._accumulated_confidence[offset_ms] *= self._confidence_decay

        # Add new peaks to accumulator (rounded to nearest ms)
        for lag_ms_peak, _, conf in top_peaks:
            offset_key = round(lag_ms_peak)
            self._accumulated_confidence[offset_key] = (
                self._accumulated_confidence.get(offset_key, 0.0) + conf
            )

        # Remove entries with negligible confidence
        self._accumulated_confidence = {
            k: v for k, v in self._accumulated_confidence.items() if v > 0.1
        }

        # Get top accumulated offsets
        sorted_accumulated = sorted(
            self._accumulated_confidence.items(), key=lambda x: x[1], reverse=True
        )[:10]

        # Calculate elapsed time since start
        elapsed_s = time_module.monotonic() - self._start_time

        # Get top accumulated offset for easy drift tracking
        top_offset = sorted_accumulated[0][0] if sorted_accumulated else 0

        # Track drift over time (only after warmup for stable measurements)
        if self._warmup_complete:
            self._drift_history.append((elapsed_s, float(top_offset)))
            if len(self._drift_history) > self._max_drift_history:
                self._drift_history.pop(0)

        # Calculate drift rate using linear regression if we have enough data
        drift_rate_str = ""
        if len(self._drift_history) >= 10:
            times = [t for t, _ in self._drift_history]
            offsets = [o for _, o in self._drift_history]
            n = len(times)
            sum_t = sum(times)
            sum_o = sum(offsets)
            sum_tt = sum(t * t for t in times)
            sum_to = sum(t * o for t, o in zip(times, offsets))

            # Linear regression: offset = slope * time + intercept
            denom = n * sum_tt - sum_t * sum_t
            if abs(denom) > 1e-10:
                slope = (n * sum_to - sum_t * sum_o) / denom  # ms per second
                drift_per_min = slope * 60  # ms per minute
                drift_rate_str = f" | drift={drift_per_min:+.2f}ms/min"

        # Format this measurement's top peaks
        peaks_str = ", ".join(f"{p[0]:+.1f}ms({p[2]:.1f})" for p in top_peaks)

        # Format accumulated winners
        accumulated_str = ", ".join(
            f"{offset:+d}ms({conf:.1f})" for offset, conf in sorted_accumulated[:5]
        )

        print(  # noqa: T201
            f"[Calibrator] t={elapsed_s:6.1f}s | best={top_offset:+4d}ms{drift_rate_str}\n"
            f"             this=[{peaks_str}]\n"
            f"             accumulated=[{accumulated_str}]"
        )
