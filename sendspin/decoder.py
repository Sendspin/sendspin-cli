"""Audio decoders for compressed formats (FLAC, etc.)."""

from __future__ import annotations

import io
import logging
import struct
from typing import TYPE_CHECKING

import av
import numpy as np
from av.container import InputContainer

if TYPE_CHECKING:
    from aiosendspin.client import AudioFormat

logger = logging.getLogger(__name__)


class FlacDecoder:
    """Decoder for FLAC audio frames.

    Decodes individual FLAC frames to PCM samples using PyAV.
    Requires FLAC streaminfo header for initialization.
    """

    def __init__(self, audio_format: AudioFormat) -> None:
        """Initialize the FLAC decoder.

        Args:
            audio_format: Audio format from stream start, including codec_header.
        """
        self._format = audio_format
        self._sample_rate = audio_format.pcm_format.sample_rate
        self._channels = audio_format.pcm_format.channels
        self._bit_depth = audio_format.pcm_format.bit_depth
        self._codec_header = audio_format.codec_header

        # Bytes per sample for output PCM
        self._bytes_per_sample = self._bit_depth // 8
        self._frame_size = self._bytes_per_sample * self._channels

        # Track total samples decoded for debugging
        self._samples_decoded = 0

    def decode(self, flac_frame: bytes) -> bytes:
        """Decode a FLAC frame to PCM samples.

        Args:
            flac_frame: Raw FLAC frame bytes.

        Returns:
            PCM audio bytes in the format specified by audio_format.
        """
        # Build a minimal FLAC stream with header + frame
        flac_data = self._build_flac_stream(flac_frame)

        container: InputContainer | None = None
        try:
            # Decode using PyAV
            container = av.open(io.BytesIO(flac_data), format="flac")  # type: ignore[assignment]
            assert isinstance(container, InputContainer)
            pcm_bytes = bytearray()

            for frame in container.decode(audio=0):
                pcm_bytes.extend(self._frame_to_pcm(frame))

            return bytes(pcm_bytes)

        except av.FFmpegError as e:
            logger.warning("FLAC decode error: %s", e)
            return b""
        finally:
            if container is not None:
                container.close()

    def _build_flac_stream(self, flac_frame: bytes) -> bytes:
        """Build a FLAC stream from header and frame.

        The codec_header from the server already contains a complete FLAC header:
        - fLaC marker (4 bytes)
        - Metadata block header (4 bytes)
        - STREAMINFO block (34 bytes)

        We just append the audio frame to it.
        """
        if self._codec_header:
            # Server provides complete FLAC header, just append frame
            return self._codec_header + flac_frame

        # Fallback: generate minimal header if not provided
        stream = bytearray()
        stream.extend(self._generate_streaminfo())
        stream.extend(flac_frame)
        return bytes(stream)

    def _generate_streaminfo(self) -> bytes:
        """Generate a complete FLAC header when codec_header is not provided.

        Structure:
        - fLaC marker (4 bytes)
        - Metadata block header (4 bytes): last-block flag + type + length
        - STREAMINFO block (34 bytes)
        """
        header = bytearray()

        # FLAC stream marker
        header.extend(b"fLaC")

        # Metadata block header: last-metadata-block (1) + type (0) = 0x80, length = 34
        header.append(0x80)
        header.extend(struct.pack(">I", 34)[1:])  # 24-bit length

        # STREAMINFO block (34 bytes)
        streaminfo = bytearray(34)

        # Min/max block size (16 bits each) - use typical values
        block_size = 4096
        streaminfo[0:2] = struct.pack(">H", block_size)  # min
        streaminfo[2:4] = struct.pack(">H", block_size)  # max

        # Min/max frame size (24 bits each) - 0 means unknown (bytes 4-9)
        # Already zero from initialization

        # Sample rate (20 bits) + channels-1 (3 bits) + bits per sample - 1 (5 bits)
        # + total samples high 4 bits
        sample_rate = self._sample_rate
        channels = self._channels - 1
        bps = self._bit_depth - 1

        # Pack: sample_rate(20) | channels(3) | bps(5) | total_samples_high(4)
        packed = (sample_rate << 12) | (channels << 9) | (bps << 4)
        streaminfo[10:14] = struct.pack(">I", packed)

        # Total samples low 32 bits (bytes 14-17) - 0 means unknown
        # MD5 signature (16 bytes, 18-33) - zeros are acceptable for streaming
        # Already zero from initialization

        header.extend(streaminfo)
        return bytes(header)

    def _frame_to_pcm(self, frame: av.AudioFrame) -> bytes:
        """Convert an av.AudioFrame to PCM bytes.

        FFmpeg decodes FLAC to s32 (32-bit signed) format internally, so we need
        to convert to the target bit depth. The samples are left-justified in
        the 32-bit container.
        """
        samples_per_channel = frame.samples

        # Get source format info
        src_format = frame.format.name  # e.g., 's32', 's32p', 's16', 's16p'
        is_planar = frame.format.is_planar

        # Determine source bytes per sample from format
        # FFmpeg typically decodes FLAC to s32/s32p
        is_16bit_source = "16" in src_format
        src_bytes_per_sample = 2 if is_16bit_source else 4

        # Read samples from frame
        samples: np.ndarray[tuple[int], np.dtype[np.int16 | np.int32]]
        if is_planar:
            # Planar: each channel in separate plane, interleave them
            src_bytes_per_plane = samples_per_channel * src_bytes_per_sample
            if is_16bit_source:
                samples = np.empty(samples_per_channel * self._channels, dtype=np.int16)
                for ch in range(self._channels):
                    plane_data = np.frombuffer(
                        bytes(frame.planes[ch])[:src_bytes_per_plane], dtype=np.int16
                    )
                    samples[ch :: self._channels] = plane_data
            else:
                samples = np.empty(samples_per_channel * self._channels, dtype=np.int32)
                for ch in range(self._channels):
                    plane_data = np.frombuffer(
                        bytes(frame.planes[ch])[:src_bytes_per_plane], dtype=np.int32
                    )
                    samples[ch :: self._channels] = plane_data
        else:
            # Packed: all channels interleaved in plane 0
            total_src_bytes = samples_per_channel * self._channels * src_bytes_per_sample
            if is_16bit_source:
                samples = np.frombuffer(
                    bytes(frame.planes[0])[:total_src_bytes], dtype=np.int16
                ).copy()
            else:
                samples = np.frombuffer(
                    bytes(frame.planes[0])[:total_src_bytes], dtype=np.int32
                ).copy()

        # Convert to target bit depth
        return self._convert_bit_depth(samples, src_bytes_per_sample * 8)

    def _convert_bit_depth(self, samples: np.ndarray, src_bits: int) -> bytes:
        """Convert samples from source bit depth to target bit depth."""
        if src_bits == self._bit_depth:
            # No conversion needed
            if self._bit_depth == 24:
                return self._pack_24bit(samples.astype(np.int32))
            return samples.tobytes()

        # Convert from source to target bit depth
        # FFmpeg stores samples left-justified, so shift right to normalize
        if src_bits == 32 and self._bit_depth == 24:
            # 32-bit to 24-bit: shift right 8 bits, then pack
            samples_32 = samples.astype(np.int32) >> 8
            return self._pack_24bit(samples_32)

        if src_bits == 32 and self._bit_depth == 16:
            # 32-bit to 16-bit: shift right 16 bits
            samples_16 = (samples.astype(np.int32) >> 16).astype(np.int16)
            return samples_16.tobytes()

        if src_bits == 16 and self._bit_depth == 24:
            # 16-bit to 24-bit: shift left 8 bits, then pack
            samples_32 = samples.astype(np.int32) << 8
            return self._pack_24bit(samples_32)

        if src_bits == 16 and self._bit_depth == 32:
            # 16-bit to 32-bit: shift left 16 bits
            samples_32 = samples.astype(np.int32) << 16
            return samples_32.tobytes()

        # Fallback: just return as-is (may not work correctly)
        logger.warning("Unsupported bit depth conversion: %d -> %d", src_bits, self._bit_depth)
        return samples.tobytes()

    def _pack_24bit(self, samples_32: np.ndarray) -> bytes:
        """Pack 32-bit samples to 24-bit (3 bytes per sample, little-endian)."""
        # Extract lower 24 bits of each sample
        num_samples = len(samples_32)
        result = bytearray(num_samples * 3)

        for i, sample in enumerate(samples_32):
            # Little-endian: LSB first
            result[i * 3] = sample & 0xFF
            result[i * 3 + 1] = (sample >> 8) & 0xFF
            result[i * 3 + 2] = (sample >> 16) & 0xFF

        return bytes(result)
