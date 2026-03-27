"""Tests for ServeCoordinator."""

from __future__ import annotations


import pytest

from sendspin.serve.coordinator import ServeCoordinator
from sendspin.serve.ipc import WorkerClientCount, WorkerError, WorkerListening


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
