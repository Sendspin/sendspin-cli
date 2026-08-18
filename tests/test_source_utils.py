"""Tests for source encoding and signal helpers."""

from __future__ import annotations

import math
import struct

from sendspin.source_utils import calc_level

RATE = 48000
CHANNELS = 2


def _sine_pcm(duration_ms: int, freq: float = 440.0) -> bytes:
    samples = RATE * duration_ms // 1000
    buffer = bytearray()
    for i in range(samples):
        value = int(0.3 * math.sin(2 * math.pi * freq * i / RATE) * 32767)
        buffer.extend(struct.pack("<h", value) * CHANNELS)
    return bytes(buffer)


def test_calc_level_silence_is_zero() -> None:
    """Silence has zero level; empty input is safe."""
    assert calc_level(b"") == 0.0
    assert calc_level(b"\x00\x00" * 100) == 0.0


def test_calc_level_signal_is_positive() -> None:
    """A real signal produces a positive normalized level."""
    assert 0.0 < calc_level(_sine_pcm(20)) <= 1.0
