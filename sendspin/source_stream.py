"""Audio capture and streaming for the Sendspin source role.

``SourceStreamer`` captures 16-bit PCM from a local input (a synthetic sine test
tone or a real line-in/microphone via ``sounddevice``) and feeds an SDK-managed
``SourceCapture``. The server is
the sole initiator of streaming: capture flows to the server only after a
``server/command`` ``start`` and stops on ``stop`` (or disconnect).
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, SignalState

from sendspin.source_utils import calc_level
from sendspin.utils import create_task

if TYPE_CHECKING:
    from aiosendspin.client import SendspinClient
    from aiosendspin.client.source import SourceCapture
    from aiosendspin.models.core import ServerCommandPayload

logger = logging.getLogger(__name__)

_SINE_AMPLITUDE = 0.3


@dataclass(slots=True)
class SourceStreamConfig:
    """Configuration for a source capture session."""

    codec: AudioCodec
    input_kind: str  # "sine" | "linein"
    device: str | None
    sample_rate: int
    channels: int
    frame_ms: int
    sine_hz: float
    signal_threshold_db: float
    line_sense: bool

    @property
    def samples_per_frame(self) -> int:
        """Number of samples per captured frame."""
        return max(1, self.sample_rate * self.frame_ms // 1000)


class SourceStreamer:
    """Captures audio and streams it to the server when the server requests it."""

    def __init__(self, client: SendspinClient, config: SourceStreamConfig) -> None:
        """Initialize the streamer for a client and capture configuration."""
        self._client = client
        self._config = config
        self._streaming = asyncio.Event()
        self._capture: SourceCapture | None = None
        self._last_signal: SignalState | None = None
        self._command_lock = asyncio.Lock()

    async def run(self) -> None:
        """Run the capture loop until cancelled.

        Captured audio flows to the server only while streaming is active (after a
        server ``start`` command); see :meth:`handle_source_command`.
        """
        if self._config.input_kind == "sine":
            await self._stream_sine()
        else:
            await self._stream_linein()

    @property
    def streaming(self) -> bool:
        """Whether the source is currently streaming to the server."""
        return self._streaming.is_set()

    def handle_source_command(self, payload: ServerCommandPayload) -> None:
        """React to a server start/stop command."""
        if payload.source is None:
            return
        logger.info("Received source %s command", payload.source.command)
        if payload.source.command == "start":
            create_task(self._begin_stream())
        elif payload.source.command == "stop":
            create_task(self._end_stream())

    async def reset(self) -> None:
        """Clear streaming state (e.g., on disconnect)."""
        await self._end_stream()
        self._last_signal = None

    async def _begin_stream(self) -> None:
        async with self._command_lock:
            if self._streaming.is_set():
                return
            cfg = self._config
            capture = self._client.create_source_capture(
                SupportedAudioFormat(
                    codec=cfg.codec,
                    sample_rate=cfg.sample_rate,
                    bit_depth=16,
                    channels=cfg.channels,
                )
            )
            await capture.start()
            self._capture = capture
            self._streaming.set()
            logger.info("Source streaming started (%s, %d Hz)", cfg.codec.value, cfg.sample_rate)

    async def _end_stream(self) -> None:
        async with self._command_lock:
            if not self._streaming.is_set():
                return
            self._streaming.clear()
            capture = self._capture
            self._capture = None
            if capture is not None:
                await capture.stop()
            logger.info("Source streaming stopped")

    async def _send_frame(self, pcm: bytes) -> None:
        """Report signal (if line sensing) and stream the frame when active."""
        if self._config.line_sense:
            self._maybe_report_signal(pcm)
        async with self._command_lock:
            if not self._streaming.is_set() or self._capture is None:
                return
            await self._capture.feed(pcm)

    def _maybe_report_signal(self, pcm: bytes) -> None:
        level = calc_level(pcm)
        threshold = 10 ** (self._config.signal_threshold_db / 20)
        signal = SignalState.PRESENT if level >= threshold else SignalState.ABSENT
        if signal != self._last_signal:
            self._last_signal = signal
            connection = self._client._admitted_connection  # noqa: SLF001
            if connection is not None:
                create_task(connection.send_source_signal(signal))

    async def _stream_sine(self) -> None:
        cfg = self._config
        samples = cfg.samples_per_frame
        phase = 0.0
        increment = 2 * math.pi * cfg.sine_hz / cfg.sample_rate
        frame_seconds = cfg.frame_ms / 1000
        while True:
            buffer = bytearray()
            for _ in range(samples):
                value = int(_SINE_AMPLITUDE * math.sin(phase) * 32767)
                phase += increment
                buffer.extend(struct.pack("<h", value) * cfg.channels)
            await self._send_frame(bytes(buffer))
            await asyncio.sleep(frame_seconds)

    async def _stream_linein(self) -> None:
        import sounddevice  # noqa: PLC0415

        cfg = self._config
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)

        def _callback(indata: object, _frames: int, _time: object, status: object) -> None:
            if status:
                logger.debug("Input stream status: %s", status)
            data = bytes(indata)  # type: ignore[call-overload]
            try:
                loop.call_soon_threadsafe(queue.put_nowait, data)
            except asyncio.QueueFull:
                logger.debug("Source capture queue full; dropping frame")

        device = cfg.device if cfg.device is None else _resolve_device(cfg.device)
        stream = sounddevice.RawInputStream(
            samplerate=cfg.sample_rate,
            channels=cfg.channels,
            dtype="int16",
            blocksize=cfg.samples_per_frame,
            device=device,
            callback=_callback,
        )
        with stream:
            logger.info(
                "Capturing from input device %s (%d Hz, %d ch)",
                cfg.device or "default",
                cfg.sample_rate,
                cfg.channels,
            )
            while True:
                data = await queue.get()
                await self._send_frame(data)


def _resolve_device(device: str) -> int | str:
    """Resolve an input device argument to a sounddevice identifier."""
    return int(device) if device.isnumeric() else device
