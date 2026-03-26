"""Integration tests for multi-worker serve mode."""

from __future__ import annotations

import pytest
from aiohttp import ClientSession

from sendspin.serve.coordinator import ServeCoordinator


@pytest.mark.asyncio
async def test_multi_worker_starts_and_serves_status() -> None:
    """Start coordinator with 2 workers, verify worker /api/status returns shared count."""
    coordinator = ServeCoordinator(
        source="http://retro.dancewave.online/retrodance.mp3",
        source_format=None,
        port=19800,
        name="Integration Test",
        workers=2,
        log_level="WARNING",
    )

    coordinator._spawn_workers()

    try:
        await coordinator._wait_for_workers_listening()

        async with ClientSession() as session:
            # Each worker should serve /api/status with the shared count (0 initially)
            for port in coordinator.worker_ports:
                async with session.get(f"http://127.0.0.1:{port}/api/status") as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["total_clients"] == 0

    finally:
        await coordinator._shutdown()
