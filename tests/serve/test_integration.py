"""Integration tests for multi-worker serve mode."""

from __future__ import annotations


import pytest
from aiohttp import ClientSession

from sendspin.serve.coordinator import ServeCoordinator


@pytest.mark.asyncio
async def test_multi_worker_http_redirect_and_status() -> None:
    """Start coordinator with 2 workers, verify redirect and status endpoint."""
    coordinator = ServeCoordinator(
        source="http://retro.dancewave.online/retrodance.mp3",
        source_format=None,
        port=19800,
        name="Integration Test",
        workers=2,
        log_level="WARNING",
    )

    # Start workers and HTTP server only (don't start audio streaming)
    coordinator._spawn_workers()

    try:
        await coordinator._wait_for_workers_listening()
        await coordinator._start_http_server()

        async with ClientSession() as session:
            # Test round-robin redirect
            async with session.get("http://127.0.0.1:19800/", allow_redirects=False) as resp:
                assert resp.status == 307
                location = resp.headers["Location"]
                assert ":19801/" in location or ":19802/" in location

            # Test status endpoint (0 clients initially)
            async with session.get("http://127.0.0.1:19800/api/status") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["total_clients"] == 0

    finally:
        await coordinator._shutdown()
