"""Tests for ServeCoordinator."""

from __future__ import annotations


from unittest.mock import MagicMock

import pytest

from sendspin.serve.coordinator import (
    ServeCoordinator,
    _MULTI_WORKER_EXTRA_DELAY_US,
    _WORKER_QUEUE_MAXSIZE,
)
from sendspin.serve.ipc import AudioChunk, WorkerClientCount, WorkerError, WorkerListening


@pytest.fixture
def coordinator() -> ServeCoordinator:
    return ServeCoordinator(
        source="http://example.com/test.mp3",
        source_format=None,
        port=18927,
        name="Test Server",
        workers=2,
        log_level="WARNING",
    )


def test_coordinator_init(coordinator: ServeCoordinator) -> None:
    assert coordinator.port == 18927
    assert coordinator.workers == 2
    assert coordinator.worker_ports == [18927, 18928]


def test_coordinator_worker_ports_calculation() -> None:
    coord = ServeCoordinator(
        source="test.mp3",
        source_format=None,
        port=9000,
        name="Test",
        workers=4,
        log_level="WARNING",
    )
    assert coord.worker_ports == [9000, 9001, 9002, 9003]


def test_coordinator_updates_total_listeners(coordinator: ServeCoordinator) -> None:
    """_handle_status_message should update shared _total_listeners value."""
    coordinator._handle_status_message(WorkerClientCount(worker_id=0, count=5))
    coordinator._handle_status_message(WorkerClientCount(worker_id=1, count=3))
    assert coordinator._total_listeners.value == 8

    # Worker 0 loses a client
    coordinator._handle_status_message(WorkerClientCount(worker_id=0, count=4))
    assert coordinator._total_listeners.value == 7


@pytest.mark.asyncio
async def test_wait_for_workers_fails_when_all_error(coordinator: ServeCoordinator) -> None:
    """If all workers report errors, _wait_for_workers_listening should return 0."""
    coordinator._status_queue.put(WorkerError(worker_id=0, error="bind failed"))
    coordinator._status_queue.put(WorkerError(worker_id=1, error="bind failed"))

    ready = await coordinator._wait_for_workers_listening()
    assert ready == 0


@pytest.mark.asyncio
async def test_wait_for_workers_partial_success(coordinator: ServeCoordinator) -> None:
    """If some workers succeed and some fail, return the success count."""
    coordinator._status_queue.put(WorkerListening(worker_id=0, port=18927))
    coordinator._status_queue.put(WorkerError(worker_id=1, error="bind failed"))

    ready = await coordinator._wait_for_workers_listening()
    assert ready == 1
    assert coordinator._failed_workers == {1}


def test_check_worker_health_ignores_startup_failed_workers(coordinator: ServeCoordinator) -> None:
    """Startup-failed workers should not trigger later crash shutdown."""
    healthy_proc = MagicMock()
    healthy_proc.is_alive.return_value = True
    failed_proc = MagicMock()
    failed_proc.is_alive.return_value = False

    coordinator._processes = [healthy_proc, failed_proc]
    coordinator._failed_workers = {1}
    coordinator._reported_crashed = {1}

    coordinator._check_worker_health()

    assert coordinator._shutdown_requested is False


def test_worker_queue_maxsize_is_bounded() -> None:
    """Worker queues should be created with a bounded maxsize for backpressure."""
    assert _WORKER_QUEUE_MAXSIZE > 0


def test_multi_worker_extra_delay_is_positive() -> None:
    """Multi-worker mode should add extra initial delay for IPC latency."""
    assert _MULTI_WORKER_EXTRA_DELAY_US > 0


@pytest.mark.asyncio
async def test_fan_out_chunk_sends_to_all_workers(coordinator: ServeCoordinator) -> None:
    """_fan_out_chunk should put the chunk into every worker queue."""
    # Manually create bounded queues (simulating _spawn_workers)
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    coordinator._audio_queues = [ctx.Queue(maxsize=_WORKER_QUEUE_MAXSIZE) for _ in range(2)]

    chunk = AudioChunk(
        pcm_bytes=b"\x00" * 100,
        sample_rate=48000,
        bit_depth=16,
        channels=2,
        play_start_us=1_000_000,
    )
    await coordinator._fan_out_chunk(chunk)

    for q in coordinator._audio_queues:
        msg = q.get(timeout=1)
        assert isinstance(msg, AudioChunk)
        assert msg.pcm_bytes == chunk.pcm_bytes


def test_check_worker_health_shuts_down_on_unexpected_worker_crash(
    coordinator: ServeCoordinator,
) -> None:
    """A healthy worker dying after startup should still shut the coordinator down."""
    healthy_proc = MagicMock()
    healthy_proc.is_alive.return_value = False
    other_proc = MagicMock()
    other_proc.is_alive.return_value = True

    coordinator._processes = [healthy_proc, other_proc]

    coordinator._check_worker_health()

    assert coordinator._shutdown_requested is True
    assert coordinator._reported_crashed == {0}
