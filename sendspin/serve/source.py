"""Audio source decoding for local files and URLs."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass

import av
import av.audio.frame
import numpy as np
from aiosendspin.server.stream import AudioFormat

# 16-bit = 2 bytes per sample
BYTES_PER_SAMPLE = 2


@dataclass
class AudioSource:
    """Represents an audio source with its decoded PCM stream."""

    generator: AsyncGenerator[bytes, None]
    format: AudioFormat


def _frame_to_bytes(frame: av.AudioFrame, channels: int) -> bytes:
    """Convert an audio frame to interleaved PCM bytes.

    For packed formats (s16), all data is in planes[0].
    For planar formats (s16p), each channel is in a separate plane.

    Note: FFmpeg audio buffers often have padding for alignment.
    We must only read the actual sample data, not the padding.
    """
    actual_bytes = frame.samples * channels * BYTES_PER_SAMPLE

    if frame.format.is_planar:
        # Planar format: interleave the channels manually
        samples_per_channel = frame.samples
        bytes_per_plane = samples_per_channel * BYTES_PER_SAMPLE
        result = np.empty(samples_per_channel * channels, dtype=np.int16)
        for ch in range(channels):
            plane_data = np.frombuffer(bytes(frame.planes[ch])[:bytes_per_plane], dtype=np.int16)
            result[ch::channels] = plane_data
        return result.tobytes()
    else:
        # Packed format: all interleaved data is in planes[0]
        return bytes(frame.planes[0])[:actual_bytes]


async def decode_audio(
    source: str,
    *,
    target_sample_rate: int = 48000,
    target_channels: int = 2,
) -> AudioSource:
    """
    Decode an audio source (file path or URL) to PCM.

    PyAV's av.open() natively supports:
    - Local files: /path/to/file.mp3
    - HTTP/HTTPS URLs: https://example.com/stream.mp3
    - Many streaming protocols via FFmpeg

    The source is automatically looped/reconnected forever.

    Args:
        source: File path or URL to the audio source.
        target_sample_rate: Output sample rate in Hz.
        target_channels: Output channel count (1=mono, 2=stereo).

    Returns:
        AudioSource with async generator yielding PCM bytes.
    """

    async def pcm_generator() -> AsyncGenerator[bytes, None]:
        layout = "stereo" if target_channels == 2 else "mono"
        container = None
        try:
            while True:
                if container is not None:
                    container.close()

                container = av.open(source)
                resampler = av.AudioResampler(format="s16", layout=layout, rate=target_sample_rate)
                for frame in container.decode(container.streams.audio[0]):
                    for resampled in resampler.resample(frame):
                        yield _frame_to_bytes(resampled, target_channels)

                # Flush resampler
                for remaining in resampler.resample(None):
                    yield _frame_to_bytes(remaining, target_channels)
        finally:
            if container is not None:
                container.close()

    audio_format = AudioFormat(
        sample_rate=target_sample_rate,
        bit_depth=16,
        channels=target_channels,
    )

    return AudioSource(
        generator=pcm_generator(),
        format=audio_format,
    )
