"""Tests for ServeWorker."""

from __future__ import annotations

import multiprocessing as mp
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sendspin.serve.ipc import AudioChunk, Shutdown, WorkerListening
from sendspin.serve.worker import ServeWorker


@pytest.fixture
def mp_context() -> mp.context.SpawnContext:
    return mp.get_context("spawn")


@pytest.fixture
def audio_queue(mp_context: mp.context.SpawnContext) -> mp.Queue:
    return mp_context.Queue()


@pytest.fixture
def status_queue(mp_context: mp.context.SpawnContext) -> mp.Queue:
    return mp_context.Queue()


def test_worker_init(
    audio_queue: mp.Queue,
    status_queue: mp.Queue,
) -> None:
    worker = ServeWorker(
        worker_id=0,
        port=8928,
        audio_queue=audio_queue,
        status_queue=status_queue,
        coordinator_url="http://192.168.1.5:8927",
    )
    assert worker.worker_id == 0
    assert worker.port == 8928


@pytest.mark.asyncio
async def test_worker_signals_listening_on_start(
    audio_queue: mp.Queue,
    status_queue: mp.Queue,
) -> None:
    """Worker should put WorkerListening on the status queue after starting."""
    worker = ServeWorker(
        worker_id=0,
        port=8928,
        audio_queue=audio_queue,
        status_queue=status_queue,
        coordinator_url="http://192.168.1.5:8927",
    )

    # Put a Shutdown immediately so the worker exits after starting
    audio_queue.put(Shutdown())

    with patch.object(worker, "_start_server", new_callable=AsyncMock):
        await worker.run()

    msg = status_queue.get(timeout=1)
    assert isinstance(msg, WorkerListening)
    assert msg.worker_id == 0
    assert msg.port == 8928


@pytest.mark.asyncio
async def test_worker_processes_audio_chunk(
    audio_queue: mp.Queue,
    status_queue: mp.Queue,
) -> None:
    """Worker should call prepare_audio/commit_audio for each AudioChunk."""
    worker = ServeWorker(
        worker_id=0,
        port=8928,
        audio_queue=audio_queue,
        status_queue=status_queue,
        coordinator_url="http://192.168.1.5:8927",
    )

    chunk = AudioChunk(
        pcm_bytes=b"\x00" * 100,
        sample_rate=48000,
        bit_depth=16,
        channels=2,
        play_start_us=1_000_000,
    )
    audio_queue.put(chunk)
    audio_queue.put(Shutdown())

    mock_stream = MagicMock()
    mock_stream.prepare_audio = MagicMock()
    mock_stream.commit_audio = AsyncMock(return_value=1_000_000)

    with (
        patch.object(worker, "_start_server", new_callable=AsyncMock),
        patch.object(worker, "_get_stream", return_value=mock_stream),
    ):
        await worker.run()

    mock_stream.prepare_audio.assert_called_once()
    mock_stream.commit_audio.assert_called_once_with(play_start_us=1_000_000)
