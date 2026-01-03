"""mDNS service advertisement for Sendspin daemon."""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

logger = logging.getLogger(__name__)

# Service type for clients advertising to servers (server-initiated connection)
CLIENT_SERVICE_TYPE = "_sendspin._tcp.local."
DEFAULT_PORT = 8927
DEFAULT_PATH = "/sendspin"


@dataclass
class AdvertisementConfig:
    """Configuration for service advertisement."""

    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    name: str | None = None


class ServiceAdvertisement:
    """Advertises the daemon as a discoverable Sendspin client via mDNS."""

    def __init__(self, config: AdvertisementConfig) -> None:
        """Initialize the service advertisement.

        Args:
            config: Advertisement configuration.
        """
        self._config = config
        self._zeroconf: AsyncZeroconf | None = None
        self._service_info: AsyncServiceInfo | None = None
        self._registered = False

    async def start(self) -> None:
        """Start advertising the service via mDNS."""
        if self._registered:
            return

        hostname = socket.gethostname()
        service_name = self._config.name or hostname

        # Build service info
        self._service_info = AsyncServiceInfo(
            CLIENT_SERVICE_TYPE,
            f"{service_name}.{CLIENT_SERVICE_TYPE}",
            port=self._config.port,
            properties={
                "path": self._config.path,
            },
            server=f"{hostname}.local.",
        )

        self._zeroconf = AsyncZeroconf(ip_version=IPVersion.All)

        try:
            await self._zeroconf.async_register_service(self._service_info)
            self._registered = True
            logger.info(
                "Advertising Sendspin client service on port %d (path: %s)",
                self._config.port,
                self._config.path,
            )
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop advertising and clean up resources."""
        if self._zeroconf is not None:
            if self._service_info is not None and self._registered:
                try:
                    await self._zeroconf.async_unregister_service(self._service_info)
                except Exception:
                    logger.exception("Error unregistering service")
            await self._zeroconf.async_close()
            self._zeroconf = None
            self._service_info = None
            self._registered = False
            logger.debug("Service advertisement stopped")

    async def __aenter__(self) -> ServiceAdvertisement:
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit."""
        await self.stop()
