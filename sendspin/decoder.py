"""Audio decoders for compressed formats (FLAC, etc.)."""

from __future__ import annotations

import logging
import struct
from typing import TYPE_CHECKING

import av
import numpy as np

if TYPE_CHECKING:
    from aiosendspin.client import AudioFormat

logger = logging.getLogger(__name__)

# FLAC header layout:
# - fLaC marker: 4 bytes
# - Metadata block header: 4 bytes (last-block flag + type + 24-bit length)
# - STREAMINFO block: 34 bytes
_FLAC_HEADER_PREFIX_SIZE = 8  # fLaC marker + metadata block header


class FlacDecoder:
    """Decoder for FLAC audio frames.

    Uses a persistent PyAV codec context to decode individual FLAC frames
    to PCM samples without per-frame container overhead.
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

        # Track total samples decoded for debugging
        self._samples_decoded = 0
        self._scratch_bytes: np.ndarray[tuple[int], np.dtype[np.uint8]] | None = None

        # Create persistent codec context
        self._codec_ctx = av.CodecContext.create("flac", "r")
        self._codec_ctx.extradata = self._build_extradata()
        self._codec_ctx.open()

    def decode(self, flac_frame: bytes) -> bytes | bytearray:
        """Decode a FLAC frame to PCM samples.

        Args:
            flac_frame: Raw FLAC frame bytes.

        Returns:
            PCM audio bytes in the format specified by audio_format.
        """
        try:
            packet = av.Packet(flac_frame)
            frames = self._codec_ctx.decode(packet)  # type: ignore[attr-defined]

            if not frames:
                return b""
            if len(frames) == 1:
                return self._frame_to_pcm(frames[0])

            pcm_bytes = bytearray()
            for frame in frames:
                self._append_frame_to_pcm(frame, pcm_bytes)

            return pcm_bytes

        except av.FFmpegError as e:
            logger.warning("FLAC decode error: %s", e)
            return b""

    def _build_extradata(self) -> bytes:
        """Build the 34-byte FLAC STREAMINFO for codec extradata.

        If the server provided a codec_header (fLaC + block header + STREAMINFO),
        extract the 34-byte STREAMINFO. Otherwise, generate it from params.
        """
        if self._codec_header and len(self._codec_header) >= _FLAC_HEADER_PREFIX_SIZE + 34:
            return self._codec_header[_FLAC_HEADER_PREFIX_SIZE : _FLAC_HEADER_PREFIX_SIZE + 34]

        # Fallback: generate STREAMINFO from parameters (codec_header is optional per spec)
        streaminfo = bytearray(34)
        block_size = 4096
        streaminfo[0:2] = struct.pack(">H", block_size)
        streaminfo[2:4] = struct.pack(">H", block_size)
        packed = (
            (self._sample_rate << 12) | ((self._channels - 1) << 9) | ((self._bit_depth - 1) << 4)
        )
        streaminfo[10:14] = struct.pack(">I", packed)
        return bytes(streaminfo)

    def _frame_to_pcm(self, frame: av.AudioFrame) -> bytearray:
        """Convert an av.AudioFrame to PCM bytes."""
        pcm_bytes = bytearray()
        self._append_frame_to_pcm(frame, pcm_bytes)
        return pcm_bytes

    def _append_frame_to_pcm(self, frame: av.AudioFrame, output: bytearray) -> None:
        """Append one decoded frame to an output PCM buffer."""
        src_bits = frame.format.bits
        src_bytes_per_sample = frame.format.bytes
        samples_per_channel = frame.samples
        channel_count = frame.layout.nb_channels
        total_samples = samples_per_channel * channel_count
        exact_src_bytes = total_samples * src_bytes_per_sample

        if src_bits not in (16, 32):
            logger.warning("Unsupported FLAC sample format: %s", frame.format.name)
            output.extend(memoryview(frame.planes[0])[:exact_src_bytes])
            return

        self._samples_decoded += total_samples

        if not frame.format.is_planar:
            self._append_packed_frame(
                output,
                memoryview(frame.planes[0])[:exact_src_bytes],
                total_samples,
                src_bits,
            )
            return

        self._append_planar_frame(output, frame, samples_per_channel, channel_count, src_bits)

    def _append_packed_frame(
        self,
        output: bytearray,
        plane: memoryview,
        total_samples: int,
        src_bits: int,
    ) -> None:
        """Append a packed frame, avoiding numeric conversions where possible."""
        if src_bits == self._bit_depth:
            output.extend(plane)
            return

        if src_bits == 32 and self._bit_depth == 16:
            samples_16 = np.frombuffer(plane, dtype="<u2", count=total_samples * 2)[1::2]
            output.extend(samples_16.tobytes())
            return

        if src_bits == 32 and self._bit_depth == 24:
            raw = np.frombuffer(plane, dtype=np.uint8, count=total_samples * 4).reshape(
                total_samples, 4
            )
            output.extend(raw[:, 1:4].tobytes())
            return

        raw = np.frombuffer(plane, dtype=np.uint8, count=total_samples * src_bits // 8).reshape(
            total_samples, src_bits // 8
        )
        scratch = self._get_scratch_bytes(total_samples * self._bytes_per_sample).reshape(
            total_samples, self._bytes_per_sample
        )

        if src_bits == 16 and self._bit_depth == 24:
            scratch[:, 0] = 0
            scratch[:, 1:3] = raw
            output.extend(memoryview(scratch.reshape(-1)))
            return

        if src_bits == 16 and self._bit_depth == 32:
            scratch[:, 0:2] = 0
            scratch[:, 2:4] = raw
            output.extend(memoryview(scratch.reshape(-1)))
            return

        logger.warning("Unsupported bit depth conversion: %d -> %d", src_bits, self._bit_depth)
        output.extend(plane)

    def _append_planar_frame(
        self,
        output: bytearray,
        frame: av.AudioFrame,
        samples_per_channel: int,
        channel_count: int,
        src_bits: int,
    ) -> None:
        """Interleave planar audio directly into a reusable byte buffer."""
        src_bytes_per_sample = src_bits // 8
        scratch = self._get_scratch_bytes(
            samples_per_channel * channel_count * self._bytes_per_sample
        ).reshape(samples_per_channel, channel_count, self._bytes_per_sample)

        for ch in range(channel_count):
            plane = np.frombuffer(
                memoryview(frame.planes[ch]),
                dtype=np.uint8,
                count=samples_per_channel * src_bytes_per_sample,
            ).reshape(samples_per_channel, src_bytes_per_sample)

            if src_bits == self._bit_depth:
                scratch[:, ch, :] = plane
                continue

            if src_bits == 32 and self._bit_depth == 16:
                scratch[:, ch, :] = plane[:, 2:4]
                continue

            if src_bits == 32 and self._bit_depth == 24:
                scratch[:, ch, :] = plane[:, 1:4]
                continue

            if src_bits == 16 and self._bit_depth == 24:
                scratch[:, ch, 0] = 0
                scratch[:, ch, 1:3] = plane
                continue

            if src_bits == 16 and self._bit_depth == 32:
                scratch[:, ch, 0:2] = 0
                scratch[:, ch, 2:4] = plane
                continue

            logger.warning("Unsupported bit depth conversion: %d -> %d", src_bits, self._bit_depth)
            output.extend(
                memoryview(frame.planes[ch])[: samples_per_channel * src_bytes_per_sample]
            )
            return

        output.extend(memoryview(scratch.reshape(-1)))

    def _get_scratch_bytes(self, size: int) -> np.ndarray[tuple[int], np.dtype[np.uint8]]:
        """Get a reusable scratch buffer sized for the current conversion."""
        if self._scratch_bytes is None or self._scratch_bytes.size < size:
            self._scratch_bytes = np.empty(size, dtype=np.uint8)
        return self._scratch_bytes[:size]
