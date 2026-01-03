"""mDNS advertisement for Sendspin clients in headless mode."""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion, InterfaceChoice, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logger = logging.getLogger(__name__)

# Service type for clients (servers look for this when discovering clients)
CLIENT_SERVICE_TYPE = "_sendspin._tcp.local."
DEFAULT_CLIENT_PORT = 8928
DEFAULT_CLIENT_PATH = "/sendspin"


def _get_local_ip() -> str:
    """Get the local IP address of this machine."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Connect to a public DNS server (doesn't actually send data)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class ClientAdvertisement:
    """Manages mDNS advertisement for a Sendspin client."""

    def __init__(
        self,
        client_id: str,
        client_name: str,
        port: int = DEFAULT_CLIENT_PORT,
        path: str = DEFAULT_CLIENT_PATH,
    ) -> None:
        """Initialize client advertisement.

        Args:
            client_id: Unique identifier for this client.
            client_name: Human-readable name for this client.
            port: Port where the client WebSocket server is listening.
            path: Path for the WebSocket endpoint.
        """
        self._client_id = client_id
        self._client_name = client_name
        self._port = port
        self._path = path
        self._zc: AsyncZeroconf | None = None
        self._service_info: ServiceInfo | None = None

    async def start(self) -> None:
        """Start advertising this client via mDNS."""
        # Get local IP address
        local_ip = _get_local_ip()
        logger.info("Advertising client on %s:%d%s", local_ip, self._port, self._path)

        # Create zeroconf instance
        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only, interfaces=InterfaceChoice.Default)

        # Create service info
        # Service name should be unique (use client_id)
        service_name = f"{self._client_id}.{CLIENT_SERVICE_TYPE}"

        # Properties to advertise
        properties = {
            "path": self._path.encode("utf-8"),
            "name": self._client_name.encode("utf-8"),
            "id": self._client_id.encode("utf-8"),
        }

        # Parse IP address
        ip_bytes = socket.inet_aton(local_ip)

        self._service_info = ServiceInfo(
            type_=CLIENT_SERVICE_TYPE,
            name=service_name,
            addresses=[ip_bytes],
            port=self._port,
            properties=properties,
            server=f"{socket.gethostname()}.local.",
        )

        # Register the service
        await self._zc.async_register_service(self._service_info)
        logger.info(
            "Client advertised as %s via mDNS (type: %s)", self._client_name, CLIENT_SERVICE_TYPE
        )

    async def stop(self) -> None:
        """Stop advertising this client."""
        if self._zc and self._service_info:
            logger.info("Unregistering mDNS advertisement")
            await self._zc.async_unregister_service(self._service_info)
            await self._zc.async_close()
            self._zc = None
            self._service_info = None
