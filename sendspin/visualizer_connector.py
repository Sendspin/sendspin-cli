"""Visualizer connector for bridging Sendspin client to the TUI visualizer."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.models.core import StreamStartMessage
from aiosendspin.models.types import Roles
from aiosendspin.models.visualizer import VisualizerFrame

if TYPE_CHECKING:
    from aiosendspin.client import SendspinClient

logger = logging.getLogger(__name__)


class VisualizerHandler:
    """Bridges between SendspinClient visualizer data and the TUI.

    Receives VisualizerFrame batches from the client, converts timestamps
    to client time, and provides the latest frame for rendering.
    """

    def __init__(
        self,
        on_frame: Callable[[VisualizerFrame], None],
    ) -> None:
        """Initialize the visualizer handler.

        Args:
            on_frame: Callback invoked with the latest frame for display.
        """
        self._on_frame = on_frame
        self._client: SendspinClient | None = None
        self._unsubscribes: list[Callable[[], None]] = []
        self._pending: deque[tuple[int, VisualizerFrame]] = deque()
        self._timer: asyncio.TimerHandle | None = None

    def attach_client(self, client: SendspinClient) -> None:
        """Attach to a SendspinClient and register listeners."""
        self._client = client
        self._unsubscribes = [
            client.add_visualizer_listener(self._on_visualizer_data),
            client.add_stream_start_listener(self._on_stream_start),
            client.add_stream_end_listener(self._on_stream_end),
            client.add_stream_clear_listener(self._on_stream_clear),
        ]

    def reset(self) -> None:
        """Clear pending frames and cancel scheduled emissions."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending.clear()
        self._on_frame(VisualizerFrame(timestamp_us=0))

    def detach(self) -> None:
        """Detach from the client and unregister listeners."""
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes = []
        self.reset()
        self._client = None

    def _on_visualizer_data(self, frames: list[VisualizerFrame]) -> None:
        """Handle incoming visualizer frames.

        Only use real spectrum frames; drop non-spectrum frames.
        """
        if not frames:
            return

        if self._client is None:
            return

        # Queue frames by synced server timestamps (independent of local audio delay).
        for frame in frames:
            if frame.spectrum is None:
                continue
            play_time_us = self._client.compute_play_time(frame.timestamp_us)
            self._pending.append((play_time_us, frame))

        if not self._pending:
            return
        self._schedule_next()

    def _on_stream_start(self, message: StreamStartMessage) -> None:
        """Flush stale frames when a new stream begins."""
        if message.payload.visualizer is None:
            return
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending.clear()

    def _on_stream_end(self, roles: list[str] | None) -> None:
        """Handle stream end for visualizer role."""
        if roles is not None and Roles.VISUALIZER.value not in roles:
            return
        self._pending.clear()
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        # Send an empty frame to trigger decay
        self._on_frame(VisualizerFrame(timestamp_us=0))

    def _on_stream_clear(self, roles: list[str] | None) -> None:
        """Handle stream clear for visualizer role."""
        if roles is not None and Roles.VISUALIZER.value not in roles:
            return
        self._pending.clear()
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._on_frame(VisualizerFrame(timestamp_us=0))

    def _schedule_next(self) -> None:
        """Schedule emission of the next due visualizer frame."""
        if self._client is None or not self._pending:
            return
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

        loop = asyncio.get_running_loop()
        now_us = int(loop.time() * 1_000_000)
        next_play_us = self._pending[0][0]
        delay_s = max(0.0, (next_play_us - now_us) / 1_000_000.0)
        self._timer = loop.call_later(delay_s, self._emit_due_frames)

    def _emit_due_frames(self) -> None:
        """Emit the newest frame whose play time is due."""
        self._timer = None
        if self._client is None or not self._pending:
            return

        now_us = int(asyncio.get_running_loop().time() * 1_000_000)
        latest_due: VisualizerFrame | None = None
        while self._pending and self._pending[0][0] <= now_us:
            _play_us, frame = self._pending.popleft()
            latest_due = frame

        if latest_due is not None:
            self._on_frame(latest_due)

        if self._pending:
            self._schedule_next()
