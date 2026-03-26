"""IPC message types for multi-worker serve mode.

These dataclasses are sent between the coordinator (main process) and worker
subprocesses via multiprocessing.Queue. They must be picklable.
"""

from __future__ import annotations

from dataclasses import dataclass


# --- Coordinator -> Worker messages (via per-worker audio queue) ---


@dataclass
class AudioChunk:
    """PCM audio data with a shared timestamp for synchronized playback."""

    pcm_bytes: bytes
    sample_rate: int
    bit_depth: int
    channels: int
    play_start_us: int


@dataclass
class Shutdown:
    """Signal the worker to shut down gracefully."""


# --- Worker -> Coordinator messages (via shared status queue) ---


@dataclass
class WorkerListening:
    """Worker server is accepting connections."""

    worker_id: int
    port: int


@dataclass
class WorkerClientConnected:
    """A client connected to this worker."""

    worker_id: int
    client_id: str


@dataclass
class WorkerClientCount:
    """Updated connected client count for this worker."""

    worker_id: int
    count: int


@dataclass
class WorkerError:
    """Worker encountered an error."""

    worker_id: int
    error: str
