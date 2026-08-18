"""Tests for the source streamer command handling and framing."""

from __future__ import annotations

import asyncio

from aiosendspin.models.core import ServerCommandPayload
from aiosendspin.models.source import SourceCommandServerPayload
from aiosendspin.models.types import AudioCodec, SignalState

from sendspin.source_stream import SourceStreamConfig, SourceStreamer


class _FakeCapture:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.frames: list[bytes] = []
        self.start_gate: asyncio.Event | None = None

    async def start(self) -> None:
        self.starts += 1
        if self.start_gate is not None:
            await self.start_gate.wait()

    async def stop(self) -> None:
        self.stops += 1

    async def feed(self, pcm: bytes) -> None:
        self.frames.append(pcm)


class _FakeConnection:
    def __init__(self) -> None:
        self.signals: list[SignalState] = []

    async def send_source_signal(self, signal: SignalState) -> None:
        self.signals.append(signal)


class _FakeClient:
    """Records the source-related calls a SourceStreamer makes."""

    def __init__(self) -> None:
        self.capture = _FakeCapture()
        self._admitted_connection = _FakeConnection()

    def create_source_capture(self, _audio_format: object) -> _FakeCapture:
        return self.capture


def _config(*, codec: AudioCodec = AudioCodec.PCM, line_sense: bool = False) -> SourceStreamConfig:
    return SourceStreamConfig(
        codec=codec,
        input_kind="sine",
        device=None,
        sample_rate=48000,
        channels=2,
        frame_ms=20,
        sine_hz=440.0,
        signal_threshold_db=-50.0,
        line_sense=line_sense,
    )


def _make() -> tuple[SourceStreamer, _FakeClient]:
    client = _FakeClient()
    return SourceStreamer(client, _config()), client  # type: ignore[arg-type]


async def test_begin_stream_announces_format_and_starts() -> None:
    """Beginning a stream sends client_stream/start and marks streaming active."""
    streamer, client = _make()
    await streamer._begin_stream()  # noqa: SLF001
    assert client.capture.starts == 1
    assert streamer._streaming.is_set()  # noqa: SLF001


async def test_end_stream_sends_end_and_stops() -> None:
    """Ending a stream sends client_stream/end and clears streaming."""
    streamer, client = _make()
    await streamer._begin_stream()  # noqa: SLF001
    await streamer._end_stream()  # noqa: SLF001
    assert client.capture.stops == 1
    assert not streamer._streaming.is_set()  # noqa: SLF001


async def test_send_frame_streams_only_when_active() -> None:
    """Frames are streamed only after the stream has begun."""
    streamer, client = _make()
    pcm = b"\x01\x02\x03\x04" * 16

    await streamer._send_frame(pcm)  # noqa: SLF001  (not started yet)
    assert client.capture.frames == []

    await streamer._begin_stream()  # noqa: SLF001
    await streamer._send_frame(pcm)  # noqa: SLF001
    assert client.capture.frames == [pcm]


async def test_line_sense_reports_signal_changes() -> None:
    """With line sensing enabled, signal presence changes are reported once."""
    client = _FakeClient()
    streamer = SourceStreamer(client, _config(line_sense=True))  # type: ignore[arg-type]

    loud = b"\x00\x40" * 64  # non-trivial amplitude
    silence = b"\x00\x00" * 64

    streamer._maybe_report_signal(loud)  # noqa: SLF001
    streamer._maybe_report_signal(loud)  # no change -> not re-reported
    streamer._maybe_report_signal(silence)  # noqa: SLF001
    await asyncio.sleep(0.05)  # let the scheduled send_source_state tasks run

    assert client._admitted_connection.signals == [SignalState.PRESENT, SignalState.ABSENT]


async def test_handle_source_command_dispatches_start_stop() -> None:
    """A server start command begins streaming; a stop command ends it."""
    streamer, client = _make()

    streamer.handle_source_command(
        ServerCommandPayload(source=SourceCommandServerPayload(command="start"))
    )
    await asyncio.sleep(0.05)
    assert streamer._streaming.is_set()  # noqa: SLF001
    assert client.capture.starts == 1

    streamer.handle_source_command(
        ServerCommandPayload(source=SourceCommandServerPayload(command="stop"))
    )
    await asyncio.sleep(0.05)
    assert not streamer._streaming.is_set()  # noqa: SLF001
    assert client.capture.stops == 1


async def test_stop_waits_for_in_progress_start() -> None:
    """A quick stop cannot be overtaken by an unfinished start."""
    streamer, client = _make()
    client.capture.start_gate = asyncio.Event()

    start = asyncio.create_task(streamer._begin_stream())  # noqa: SLF001
    await asyncio.sleep(0)
    stop = asyncio.create_task(streamer._end_stream())  # noqa: SLF001
    client.capture.start_gate.set()
    await asyncio.gather(start, stop)

    assert not streamer.streaming
    assert client.capture.stops == 1
