"""Audio source decoding for local files and URLs."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass

import av
import av.audio.frame
import numpy as np
from aiosendspin.server.stream import AudioFormat


@dataclass
class AudioSource:
    """Represents an audio source with its decoded PCM stream."""

    generator: AsyncGenerator[bytes, None]
    format: AudioFormat
    duration_us: int | None  # None for live streams


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

    Args:
        source: File path or URL to the audio source.
        target_sample_rate: Output sample rate in Hz.
        target_channels: Output channel count (1=mono, 2=stereo).

    Returns:
        AudioSource with async generator yielding PCM bytes.
    """
    container = av.open(source)
    audio_stream = container.streams.audio[0]

    # Calculate duration if available (None for live streams)
    duration_us = None
    if audio_stream.duration and audio_stream.time_base:
        duration_us = int(float(audio_stream.duration * audio_stream.time_base) * 1_000_000)

    # Set up resampler for consistent output format
    # Use s16 (packed/interleaved) format for direct PCM output
    resampler = av.AudioResampler(
        format="s16",  # 16-bit signed PCM (packed/interleaved)
        layout="stereo" if target_channels == 2 else "mono",
        rate=target_sample_rate,
    )

    # Calculate bytes per sample for s16 format
    bytes_per_sample = 2  # 16-bit = 2 bytes

    def frame_to_bytes(frame: av.AudioFrame) -> bytes:
        """Convert an audio frame to interleaved PCM bytes.

        For packed formats (s16), all data is in planes[0].
        For planar formats (s16p), each channel is in a separate plane.

        Note: FFmpeg audio buffers often have padding for alignment.
        We must only read the actual sample data, not the padding.
        """
        # Calculate exact byte count for actual audio data
        actual_bytes = frame.samples * target_channels * bytes_per_sample

        if frame.format.is_planar:
            # Planar format: interleave the channels manually
            # Each plane contains samples for one channel
            samples_per_channel = frame.samples
            bytes_per_plane = samples_per_channel * bytes_per_sample
            result = np.empty(samples_per_channel * target_channels, dtype=np.int16)
            for ch in range(target_channels):
                # Only read actual sample bytes, not padding
                plane_data = np.frombuffer(
                    bytes(frame.planes[ch])[:bytes_per_plane], dtype=np.int16
                )
                result[ch::target_channels] = plane_data
            return result.tobytes()
        else:
            # Packed format: all interleaved data is in planes[0]
            # Only return actual audio bytes, exclude padding
            return bytes(frame.planes[0])[:actual_bytes]

    async def pcm_generator() -> AsyncGenerator[bytes, None]:
        try:
            for frame in container.decode(audio_stream):
                resampled_frames = resampler.resample(frame)
                for resampled in resampled_frames:
                    yield frame_to_bytes(resampled)

            # Flush resampler
            for remaining in resampler.resample(None):
                yield frame_to_bytes(remaining)
        finally:
            container.close()

    audio_format = AudioFormat(
        sample_rate=target_sample_rate,
        bit_depth=16,
        channels=target_channels,
    )

    return AudioSource(
        generator=pcm_generator(),
        format=audio_format,
        duration_us=duration_us,
    )
