"""Tests for IPC message types."""

from __future__ import annotations

import pickle

from sendspin.serve.ipc import (
    AudioChunk,
    Shutdown,
    WorkerClientConnected,
    WorkerClientCount,
    WorkerError,
    WorkerListening,
)


def test_audio_chunk_picklable() -> None:
    msg = AudioChunk(
        pcm_bytes=b"\x00" * 100,
        sample_rate=48000,
        bit_depth=16,
        channels=2,
        play_start_us=1_000_000,
    )
    restored = pickle.loads(pickle.dumps(msg))
    assert restored.pcm_bytes == msg.pcm_bytes
    assert restored.sample_rate == msg.sample_rate
    assert restored.bit_depth == msg.bit_depth
    assert restored.channels == msg.channels
    assert restored.play_start_us == msg.play_start_us


def test_shutdown_picklable() -> None:
    msg = Shutdown()
    restored = pickle.loads(pickle.dumps(msg))
    assert isinstance(restored, Shutdown)


def test_worker_listening_picklable() -> None:
    msg = WorkerListening(worker_id=0, port=8928)
    restored = pickle.loads(pickle.dumps(msg))
    assert restored.worker_id == 0
    assert restored.port == 8928


def test_worker_client_connected_picklable() -> None:
    msg = WorkerClientConnected(worker_id=1, client_id="test-001")
    restored = pickle.loads(pickle.dumps(msg))
    assert restored.worker_id == 1
    assert restored.client_id == "test-001"


def test_worker_client_count_picklable() -> None:
    msg = WorkerClientCount(worker_id=2, count=42)
    restored = pickle.loads(pickle.dumps(msg))
    assert restored.worker_id == 2
    assert restored.count == 42


def test_worker_error_picklable() -> None:
    msg = WorkerError(worker_id=0, error="something broke")
    restored = pickle.loads(pickle.dumps(msg))
    assert restored.worker_id == 0
    assert restored.error == "something broke"
