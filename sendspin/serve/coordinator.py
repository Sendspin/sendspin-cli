"""Coordinator for multi-worker serve mode.

The coordinator is the main process. It spawns worker subprocesses, decodes
the audio source, and fans out PCM chunks with shared timestamps to all workers.
It also runs an HTTP server that round-robin redirects browsers to worker ports
and serves an aggregated client count endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import queue as _queue
import signal
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from aiohttp import web
from aiosendspin.server.push_stream import DEFAULT_INITIAL_DELAY_US

from sendspin.serve import get_local_ip, print_qr_code
from sendspin.serve.ipc import (
    AudioChunk,
    Shutdown,
    WorkerClientConnected,
    WorkerClientCount,
    WorkerError,
    WorkerListening,
)
from sendspin.serve.source import decode_audio
from sendspin.serve.worker import worker_main

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ServeCoordinator:
    """Orchestrates multi-worker serve mode."""

    def __init__(
        self,
        *,
        source: str,
        source_format: str | None,
        port: int,
        name: str,
        workers: int,
        log_level: str,
    ) -> None:
        self.source = source
        self.source_format = source_format
        self.port = port
        self.name = name
        self.workers = workers
        self.log_level = log_level
        self.worker_ports = [port + 1 + i for i in range(workers)]

        # IPC
        self._ctx = mp.get_context("spawn")
        self._audio_queues: list[mp.Queue] = []  # type: ignore[type-arg]
        self._status_queue: mp.Queue = self._ctx.Queue()  # type: ignore[type-arg]
        self._processes: list[mp.process.BaseProcess] = []

        # State
        self._client_counts: dict[int, int] = {}
        self._shutdown_requested = False
        self._client_connected_event = asyncio.Event()
        self._active_worker_ports: list[int] = []

        # HTTP server
        self._http_runner: web.AppRunner | None = None

    async def run(self) -> int:
        """Main coordinator loop."""
        loop = asyncio.get_running_loop()

        with suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGINT, self._handle_sigint)

        try:
            self._spawn_workers()
            await self._wait_for_workers_listening()
            await self._start_http_server()
            self._print_banner()

            # Wait for first client on any worker
            await self._consume_status_until_client()

            # Decode and fan out audio
            await self._stream_audio_loop()

        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

        return 0

    def _handle_sigint(self) -> None:
        self._shutdown_requested = True
        print("\nShutting down...")  # noqa: T201
        self._client_connected_event.set()

    def _spawn_workers(self) -> None:
        """Spawn worker subprocesses."""
        local_ip = get_local_ip()
        coordinator_url = f"http://{local_ip}:{self.port}"

        for i in range(self.workers):
            audio_queue: mp.Queue = self._ctx.Queue()  # type: ignore[type-arg]
            self._audio_queues.append(audio_queue)

            p = self._ctx.Process(
                target=worker_main,
                args=(
                    i,
                    self.worker_ports[i],
                    audio_queue,
                    self._status_queue,
                    coordinator_url,
                    self.log_level,
                ),
            )
            p.start()
            self._processes.append(p)

        logger.info("Spawned %d worker processes", self.workers)

    async def _wait_for_workers_listening(self) -> None:
        """Wait for all workers to report they are listening."""
        loop = asyncio.get_running_loop()
        listening_count = 0

        while listening_count < self.workers:
            msg = await loop.run_in_executor(None, self._status_queue.get)

            if isinstance(msg, WorkerListening):
                listening_count += 1
                self._active_worker_ports.append(msg.port)
                logger.info(
                    "Worker %d listening on port %d (%d/%d)",
                    msg.worker_id,
                    msg.port,
                    listening_count,
                    self.workers,
                )
            elif isinstance(msg, WorkerError):
                logger.error("Worker %d error during startup: %s", msg.worker_id, msg.error)
                listening_count += 1

    async def _consume_status_until_client(self) -> None:
        """Process status messages until at least one client connects or shutdown."""
        loop = asyncio.get_running_loop()

        def _get_with_timeout() -> object | None:
            try:
                result: object = self._status_queue.get(timeout=0.5)
            except _queue.Empty:
                return None
            return result

        while not self._shutdown_requested:
            msg = await loop.run_in_executor(None, _get_with_timeout)
            if msg is None:
                continue
            self._handle_status_message(msg)
            if isinstance(msg, WorkerClientConnected):
                return

    def _handle_status_message(self, msg: object) -> None:
        """Handle a single status message from a worker."""
        if isinstance(msg, WorkerClientConnected):
            logger.info("Client %s connected to worker %d", msg.client_id, msg.worker_id)
        elif isinstance(msg, WorkerClientCount):
            self._client_counts[msg.worker_id] = msg.count
        elif isinstance(msg, WorkerError):
            logger.error("Worker %d error: %s", msg.worker_id, msg.error)

    async def _drain_status_queue(self) -> None:
        """Non-blocking drain of pending status messages."""
        while True:
            try:
                msg = self._status_queue.get_nowait()
                self._handle_status_message(msg)
            except Exception:  # noqa: BLE001
                break

    async def _stream_audio_loop(self) -> None:
        """Decode audio and fan out PCM chunks to all workers."""
        consecutive_errors = 0

        while not self._shutdown_requested:
            try:
                audio_source = await decode_audio(self.source, source_format=self.source_format)
                fmt = audio_source.format

                play_start_us = int(time.monotonic() * 1_000_000) + DEFAULT_INITIAL_DELAY_US

                async for pcm_chunk in audio_source.generator:
                    if self._shutdown_requested:
                        break  # type: ignore[unreachable]

                    await self._drain_status_queue()

                    frame_stride = (fmt.bit_depth // 8) * fmt.channels
                    sample_count = len(pcm_chunk) // frame_stride
                    chunk_duration_us = int(sample_count * 1_000_000 / fmt.sample_rate)

                    chunk_msg = AudioChunk(
                        pcm_bytes=pcm_chunk,
                        sample_rate=fmt.sample_rate,
                        bit_depth=fmt.bit_depth,
                        channels=fmt.channels,
                        play_start_us=play_start_us,
                    )
                    for queue in self._audio_queues:
                        queue.put(chunk_msg)

                    play_start_us += chunk_duration_us

                    now_us = int(time.monotonic() * 1_000_000)
                    ahead_us = play_start_us - DEFAULT_INITIAL_DELAY_US - now_us
                    if ahead_us > 0:
                        await asyncio.sleep(ahead_us / 1_000_000)

                consecutive_errors = 0

            except asyncio.CancelledError:
                break
            except FileNotFoundError as e:
                print(f"Error: {e}")  # noqa: T201
                return
            except Exception as e:  # noqa: BLE001
                consecutive_errors += 1
                delay = min(2**consecutive_errors, 30)
                print(f"Playback error: {e}")  # noqa: T201
                logger.debug("Playback error", exc_info=True)
                print(f"Retrying in {delay}s...")  # noqa: T201
                await asyncio.sleep(delay)

    async def _start_http_server(self) -> None:
        """Start the coordinator HTTP server for redirects and status."""
        local_ip = get_local_ip()
        worker_urls = [f"http://{local_ip}:{p}/" for p in self._active_worker_ports]

        # If no workers reported listening yet, use calculated ports
        if not worker_urls:
            worker_urls = [f"http://{local_ip}:{p}/" for p in self.worker_ports]

        app = web.Application()

        redirect_index = 0

        async def redirect_handler(request: web.Request) -> web.Response:
            nonlocal redirect_index
            if not worker_urls:
                return web.Response(text="No workers available", status=503)
            url = worker_urls[redirect_index % len(worker_urls)]
            redirect_index += 1
            return web.Response(
                status=307,
                headers={
                    "Location": url,
                    "Cache-Control": "no-store",
                },
            )

        client_counts = self._client_counts

        async def status_handler(request: web.Request) -> web.Response:
            total = sum(client_counts.values())
            return web.json_response(
                {"total_clients": total},
                headers={"Access-Control-Allow-Origin": "*"},
            )

        app.router.add_get("/", redirect_handler)
        app.router.add_get("/api/status", status_handler)

        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        site = web.TCPSite(self._http_runner, port=self.port)
        await site.start()

    async def _stop_http_server(self) -> None:
        """Stop the coordinator HTTP server."""
        if self._http_runner is not None:
            await self._http_runner.cleanup()
            self._http_runner = None

    def _print_banner(self) -> None:
        """Print the server URLs and QR code."""
        local_ip = get_local_ip()
        url = f"http://{local_ip}:{self.port}/"

        print(f"\nMulti-worker server running ({self.workers} workers)")  # noqa: T201
        for i, port in enumerate(self._active_worker_ports):
            print(f"  Worker {i}: http://{local_ip}:{port}/")  # noqa: T201
        print(f"\nEntry point: {url}")  # noqa: T201

        if local_ip != "localhost":
            print()  # noqa: T201
            print_qr_code(url)
            print()  # noqa: T201
            print("Scan QR to open in browser to use the web player")  # noqa: T201
        else:
            print("Unable to print QR code because no LAN IP available")  # noqa: T201
            print("Open in browser to use the web player")  # noqa: T201
        print("Press Ctrl+C to quit\n")  # noqa: T201

    async def _shutdown(self) -> None:
        """Gracefully shut down all workers."""
        for queue in self._audio_queues:
            with suppress(Exception):
                queue.put(Shutdown())

        for p in self._processes:
            p.join(timeout=5.0)

        for p in self._processes:
            if p.is_alive():
                p.terminate()
        for p in self._processes:
            p.join(timeout=2.0)

        await self._stop_http_server()
