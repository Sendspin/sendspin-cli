"""mDNS service discovery for Sendspin servers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from zeroconf import ServiceListener

from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from sendspin.utils import create_task

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredServer:
    """Information about a discovered Sendspin server."""

    name: str
    url: str
    host: str
    port: int


SERVICE_TYPE = "_sendspin-server._tcp.local."
DEFAULT_PATH = "/sendspin"


def _build_service_url(host: str, port: int, properties: dict[bytes, bytes | None]) -> str:
    """Construct WebSocket URL from mDNS service info."""
    path_raw = properties.get(b"path")
    path = path_raw.decode("utf-8", "ignore") if isinstance(path_raw, bytes) else DEFAULT_PATH
    if not path:
        path = DEFAULT_PATH
    if not path.startswith("/"):
        path = "/" + path
    host_fmt = f"[{host}]" if ":" in host else host
    return f"ws://{host_fmt}:{port}{path}"


class _ServiceDiscoveryListener:
    """Listens for Sendspin server advertisements via mDNS."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._current_url: str | None = None
        self._first_result: asyncio.Future[str] = loop.create_future()
        self._servers: dict[str, DiscoveredServer] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    @property
    def current_url(self) -> str | None:
        """Get the current discovered server URL, or None if no servers."""
        return self._current_url

    @property
    def servers(self) -> dict[str, DiscoveredServer]:
        """Get all discovered servers."""
        return self._servers

    async def wait_for_first(self) -> str:
        """Wait for the first server to be discovered."""
        return await self._first_result

    async def _process_service_info(
        self, zeroconf: AsyncZeroconf, service_type: str, name: str
    ) -> None:
        """Extract and construct WebSocket URL from service info."""
        info = await zeroconf.async_get_service_info(service_type, name)
        if info is None or info.port is None:
            return
        addresses = info.parsed_addresses()
        if not addresses:
            return
        host = addresses[0]
        url = _build_service_url(host, info.port, info.properties)
        self._current_url = url

        # Track this server
        self._servers[name] = DiscoveredServer(
            name=name.removesuffix(f".{SERVICE_TYPE}"),
            url=url,
            host=host,
            port=info.port,
        )

        # Signal first server discovery
        if not self._first_result.done():
            self._first_result.set_result(url)

    def _schedule(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        task = create_task(self._process_service_info(zeroconf, service_type, name), loop=self._loop)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def add_service(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        self._schedule(zeroconf, service_type, name)

    def update_service(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        self._schedule(zeroconf, service_type, name)

    def remove_service(self, _zeroconf: AsyncZeroconf, _service_type: str, name: str) -> None:
        """Handle service removal (server offline)."""
        self._servers.pop(name, None)
        self._current_url = None


class ServiceDiscovery:
    """Manages continuous discovery of Sendspin servers via mDNS."""

    def __init__(self) -> None:
        """Initialize the service discovery manager."""
        self._listener: _ServiceDiscoveryListener | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._zeroconf: AsyncZeroconf | None = None

    async def start(self) -> None:
        """Start continuous discovery (keeps running until stop() is called)."""
        loop = asyncio.get_running_loop()
        self._listener = _ServiceDiscoveryListener(loop)
        self._zeroconf = AsyncZeroconf()
        await self._zeroconf.__aenter__()

        try:
            self._browser = AsyncServiceBrowser(
                self._zeroconf.zeroconf, SERVICE_TYPE, cast("ServiceListener", self._listener)
            )
        except Exception:
            await self.stop()
            raise

    async def wait_for_first_server(self) -> str:
        """Wait indefinitely for the first server to be discovered."""
        if self._listener is None:
            raise RuntimeError("Discovery not started. Call start() first.")
        return await self._listener.wait_for_first()

    def current_url(self) -> str | None:
        """Get the current discovered server URL, or None if no servers."""
        return self._listener.current_url if self._listener else None

    def get_servers(self) -> list[DiscoveredServer]:
        """Get all discovered servers."""
        if self._listener is None:
            return []
        return list(self._listener.servers.values())

    async def stop(self) -> None:
        """Stop discovery and clean up resources."""
        if self._browser:
            await self._browser.async_cancel()
            self._browser = None
        if self._zeroconf:
            await self._zeroconf.__aexit__(None, None, None)
            self._zeroconf = None
        self._listener = None


async def discover_servers(discovery_time: float = 3.0) -> list[DiscoveredServer]:
    """Discover Sendspin servers on the network.

    Args:
        discovery_time: How long to wait for discovery in seconds.

    Returns:
        List of discovered servers.
    """
    discovery = ServiceDiscovery()
    await discovery.start()
    try:
        await asyncio.sleep(discovery_time)
        return discovery.get_servers()
    finally:
        await discovery.stop()
