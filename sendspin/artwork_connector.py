"""Artwork connector for bridging Sendspin client to the TUI."""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:
    from aiosendspin.client import SendspinClient

logger = logging.getLogger(__name__)


class ArtworkHandler:
    """Bridges between SendspinClient artwork frames and the TUI.

    Subscribes to artwork binary frames (album channel only), decodes them via
    Pillow, and routes the latest image to a callback. Empty payloads, stream
    end, and stream clear all collapse to ``on_image(None)``.
    """

    def __init__(
        self,
        on_image: Callable[[Image.Image | None], None],
    ) -> None:
        self._on_image = on_image
        self._unsubscribes: list[Callable[[], None]] = []

    def attach_client(self, client: SendspinClient) -> None:
        """Register artwork, stream_end, and stream_clear listeners."""
        self._unsubscribes = [
            client.add_artwork_listener(self._on_artwork_frame),
            client.add_stream_end_listener(self._on_stream_end),
            client.add_stream_clear_listener(self._on_stream_clear),
        ]

    def detach(self) -> None:
        """Unregister listeners. Silent: never fires the callback."""
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes = []

    def _on_artwork_frame(self, channel: int, payload: bytes) -> None:
        if channel != 0:
            return
        if not payload:
            self._on_image(None)
            return
        try:
            image = Image.open(io.BytesIO(payload))
            image.load()
        except (UnidentifiedImageError, OSError) as exc:
            logger.warning("Failed to decode artwork payload: %s", exc)
            self._on_image(None)
            return
        self._on_image(image)

    def _on_stream_end(self, roles: list[str] | None) -> None:
        if roles is not None and "artwork" not in roles:
            return
        self._on_image(None)

    def _on_stream_clear(self, roles: list[str] | None) -> None:
        if roles is not None and "artwork" not in roles:
            return
        self._on_image(None)
