"""mDNS service discovery for Sendspin servers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

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

    @classmethod
    def from_url(cls, name: str, url: str) -> DiscoveredServer:
        """Create a discovered server."""
        parts = urlparse(url)
        if parts.hostname is None:
            raise ValueError("URL contains no hostname")
        port = parts.port
        if port is None:
            port = 443 if parts.scheme in ("wss", "https") else 80
        return cls(
            name=name,
            url=url,
            host=parts.hostname,
            port=port,
        )


@dataclass
class DiscoveredClient:
    """Information about a discovered Sendspin client."""

    name: str
    url: str
    host: str
    port: int


SERVICE_TYPE = "_sendspin-server._tcp.local."
CLIENT_SERVICE_TYPE = "_sendspin._tcp.local."
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
        self._next_result: asyncio.Future[DiscoveredServer] | None = None
        self._servers: dict[str, DiscoveredServer] = {}

    @property
    def servers(self) -> dict[str, DiscoveredServer]:
        """Get all discovered servers."""
        return self._servers

    async def wait_for_next(self) -> DiscoveredServer:
        """Wait for the first server to be discovered."""
        if self._next_result is None:
            self._next_result = self._loop.create_future()
        return await self._next_result

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

        # Track this server
        self._servers[name] = DiscoveredServer(
            name=name.removesuffix(f".{SERVICE_TYPE}"),
            url=url,
            host=host,
            port=info.port,
        )

        # Signal first server discovery
        if self._next_result and not self._next_result.done():
            self._next_result.set_result(self._servers[name])
            self._next_result = None

    def add_service(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        create_task(self._process_service_info(zeroconf, service_type, name), loop=self._loop)

    def update_service(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        create_task(self._process_service_info(zeroconf, service_type, name), loop=self._loop)

    def remove_service(self, _zeroconf: AsyncZeroconf, _service_type: str, name: str) -> None:
        """Handle service removal (server offline)."""
        self._servers.pop(name, None)


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

    async def wait_for_server(self) -> DiscoveredServer:
        """Wait indefinitely for a server to be discovered.

        Will return directly if a server is currently known.
        """
        if self._listener is None:
            raise RuntimeError("Discovery not started. Call start() first.")
        if servers := self.get_servers():
            return servers[0]
        return await self._listener.wait_for_next()

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


class _ClientDiscoveryListener:
    """Listens for Sendspin client advertisements via mDNS."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._clients: dict[str, DiscoveredClient] = {}

    @property
    def clients(self) -> dict[str, DiscoveredClient]:
        """Get all discovered clients."""
        return self._clients

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

        # Track this client
        self._clients[name] = DiscoveredClient(
            name=name.removesuffix(f".{CLIENT_SERVICE_TYPE}"),
            url=url,
            host=host,
            port=info.port,
        )

    def add_service(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        create_task(self._process_service_info(zeroconf, service_type, name), loop=self._loop)

    def update_service(self, zeroconf: AsyncZeroconf, service_type: str, name: str) -> None:
        create_task(self._process_service_info(zeroconf, service_type, name), loop=self._loop)

    def remove_service(self, _zeroconf: AsyncZeroconf, _service_type: str, name: str) -> None:
        """Handle service removal (client offline)."""
        self._clients.pop(name, None)


async def discover_clients(discovery_time: float = 3.0) -> list[DiscoveredClient]:
    """Discover Sendspin clients on the network.

    Args:
        discovery_time: How long to wait for discovery in seconds.

    Returns:
        List of discovered clients.
    """
    loop = asyncio.get_running_loop()
    listener = _ClientDiscoveryListener(loop)
    zeroconf = AsyncZeroconf()
    await zeroconf.__aenter__()

    try:
        browser = AsyncServiceBrowser(
            zeroconf.zeroconf, CLIENT_SERVICE_TYPE, cast("ServiceListener", listener)
        )
        await asyncio.sleep(discovery_time)
        await browser.async_cancel()
        return list(listener.clients.values())
    finally:
        await zeroconf.__aexit__(None, None, None)
