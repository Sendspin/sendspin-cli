from __future__ import annotations

from types import SimpleNamespace

from sendspin.audio import AudioPlayer, PlaybackState, _QueuedChunk


class _NoCallbackStatus:
    input_underflow = False
    output_underflow = False

    def __bool__(self) -> bool:
        return False


def test_drop_correction_discards_one_frame_without_repeating_previous() -> None:
    now_us = 0

    def now() -> int:
        return now_us

    player = AudioPlayer(lambda ts: ts, lambda ts: ts, now_us=now)
    player._format = SimpleNamespace(  # noqa: SLF001
        sample_rate=48_000,
        channels=1,
        bit_depth=16,
        frame_size=2,
    )
    player._playback_state = PlaybackState.PLAYING  # noqa: SLF001
    player._drop_every_n_frames = 1  # noqa: SLF001
    player._frames_until_next_drop = 1  # noqa: SLF001
    player._queue.put(  # noqa: SLF001
        _QueuedChunk(
            server_timestamp_us=0,
            audio_data=b"\x01\x00\x02\x00\x03\x00",
        )
    )

    out = bytearray(4)
    player._audio_callback(  # noqa: SLF001
        memoryview(out),
        frames=2,
        time=SimpleNamespace(outputBufferDacTime=0.0),
        status=_NoCallbackStatus(),
    )

    assert bytes(out) == b"\x01\x00\x03\x00"


def test_sync_correction_waits_for_startup_grace_period() -> None:
    now_us = 1_000_000

    def now() -> int:
        return now_us

    player = AudioPlayer(lambda ts: ts, lambda ts: ts, now_us=now)
    player._format = SimpleNamespace(sample_rate=48_000)  # noqa: SLF001
    player._playback_started_loop_time_us = now_us  # noqa: SLF001

    player._update_correction_schedule(50_000)  # noqa: SLF001

    assert player._drop_every_n_frames == 0  # noqa: SLF001
    assert player._insert_every_n_frames == 0  # noqa: SLF001
