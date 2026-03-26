"""Tests for ServeCoordinator."""

from __future__ import annotations


import pytest
from aiohttp import ClientSession

from sendspin.serve.coordinator import ServeCoordinator


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
    assert coordinator.worker_ports == [18928, 18929]


def test_coordinator_worker_ports_calculation() -> None:
    coord = ServeCoordinator(
        source="test.mp3",
        source_format=None,
        port=9000,
        name="Test",
        workers=4,
        log_level="WARNING",
    )
    assert coord.worker_ports == [9001, 9002, 9003, 9004]


@pytest.mark.asyncio
async def test_coordinator_round_robin_redirect() -> None:
    """The HTTP server should 307-redirect round-robin to worker ports."""
    coord = ServeCoordinator(
        source="test.mp3",
        source_format=None,
        port=18950,
        name="Test",
        workers=2,
        log_level="WARNING",
    )

    await coord._start_http_server()

    try:
        async with ClientSession() as session:
            # First request -> worker 0 (port 18951)
            async with session.get("http://127.0.0.1:18950/", allow_redirects=False) as resp:
                assert resp.status == 307
                location = resp.headers["Location"]
                assert ":18951/" in location

            # Second request -> worker 1 (port 18952)
            async with session.get("http://127.0.0.1:18950/", allow_redirects=False) as resp:
                assert resp.status == 307
                location = resp.headers["Location"]
                assert ":18952/" in location

            # Third request -> back to worker 0
            async with session.get("http://127.0.0.1:18950/", allow_redirects=False) as resp:
                assert resp.status == 307
                location = resp.headers["Location"]
                assert ":18951/" in location
    finally:
        await coord._stop_http_server()


@pytest.mark.asyncio
async def test_coordinator_status_endpoint() -> None:
    """GET /api/status should return aggregated client count."""
    coord = ServeCoordinator(
        source="test.mp3",
        source_format=None,
        port=18951,
        name="Test",
        workers=2,
        log_level="WARNING",
    )

    # Simulate client counts from 2 workers
    coord._client_counts = {0: 5, 1: 3}

    await coord._start_http_server()

    try:
        async with ClientSession() as session:
            async with session.get("http://127.0.0.1:18951/api/status") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data == {"total_clients": 8}
    finally:
        await coord._stop_http_server()
