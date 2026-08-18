"""Signal helpers for the Sendspin source role."""

from __future__ import annotations

import array
import sys

# Source capture is fixed at 16-bit signed PCM (matches sounddevice int16 capture
# and keeps codec init simple). The server can still receive other depths from
# other source implementations.
_MAX_INT16 = 32767.0


def calc_level(pcm: bytes) -> float:
    """Return a normalized RMS level (0.0-1.0) for 16-bit interleaved PCM."""
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    total = 0.0
    for sample in samples:
        norm = sample / _MAX_INT16
        total += norm * norm
    rms = (total / len(samples)) ** 0.5
    return float(min(1.0, rms))
