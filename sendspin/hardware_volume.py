"""Hardware volume control backend for Linux PulseAudio/PipeWire."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
import sys
from typing import Any


AVAILABLE = False
if sys.platform.startswith("linux"):
    try:
        import pulsectl_asyncio

        AVAILABLE = True
    except (ImportError, OSError):
        pass

logger = logging.getLogger(__name__)

VolumeChangeCallback = Callable[[int, bool], None]


class HardwareVolumeController:
    """Controls Linux system output volume through PulseAudio API."""

    def __init__(self) -> None:
        """Initialize the controller."""
        self._watch_task: asyncio.Task[None] | None = None

    async def set_state(self, volume: int, *, muted: bool) -> bool:
        """Set hardware volume and mute state.

        Args:
            volume: Volume level in range 0-100.
            muted: Whether output should be muted.

        Returns:
            True if the update completed successfully, False otherwise.
        """
        if not AVAILABLE:
            return False

        clamped = max(0, min(100, volume))
        try:
            async with pulsectl_asyncio.PulseAsync("sendspin-cli") as client:
                sink = await self._get_default_sink(client)
                if sink is None:
                    logger.warning("No PulseAudio sink available for hardware volume")
                    return False

                await client.volume_set_all_chans(sink, clamped / 100.0)
                await client.mute(sink, muted)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to set PulseAudio hardware volume: %s", exc)
            return False

    async def get_state(self) -> tuple[int, bool] | None:
        """Get current hardware volume and mute state.

        Returns:
            ``(volume, muted)`` when available, otherwise ``None``.
        """
        if not AVAILABLE:
            return None

        try:
            async with pulsectl_asyncio.PulseAsync("sendspin-cli") as client:
                sink = await self._get_default_sink(client)
                if sink is None:
                    logger.warning("No PulseAudio sink available for hardware volume read")
                    return None

                volume_obj = sink.volume
                volume_flat = volume_obj.value_flat
                if volume_flat is None:
                    # calculate flat volume by averaging all channels' volume levels
                    if values := volume_obj.values:
                        volume_flat = sum(values) / len(values)

                if volume_flat is None:
                    logger.warning("Failed to read sink volume from PulseAudio")
                    return None

                volume = max(0, min(100, int(round(float(volume_flat) * 100))))
                muted = bool(sink.mute)
                return volume, muted
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read PulseAudio hardware volume: %s", exc)
            return None

    async def _get_default_sink(self, client: pulsectl_asyncio.PulseAsync) -> Any | None:
        """Return default sink if available, otherwise first sink."""
        server_info = await client.server_info()
        sinks = await client.sink_list()
        sink = next((item for item in sinks if item.name == server_info.default_sink_name), None)
        if sink is None and sinks:
            sink = sinks[0]
        return sink

    async def start_monitoring(self, callback: VolumeChangeCallback) -> None:
        """Start listening for external hardware volume changes.

        Calls *callback(volume, muted)* whenever the hardware volume or mute
        state changes compared to the last observed value.
        """
        if not AVAILABLE or self._watch_task is not None:
            return
        self._watch_task = asyncio.get_running_loop().create_task(self._watch_events(callback))

    async def stop_monitoring(self) -> None:
        """Stop the monitoring loop if running."""
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None

    async def _watch_events(self, callback: VolumeChangeCallback) -> None:
        """Subscribe to PulseAudio sink events and invoke callback on change."""
        while True:
            previous_state = await self.get_state()
            try:
                async with pulsectl_asyncio.PulseAsync("sendspin-cli-monitor") as pulse:
                    async for _event in pulse.subscribe_events("sink"):
                        current = await self.get_state()
                        if current is None:
                            continue
                        if previous_state == current:
                            continue
                        logger.debug(
                            "Hardware volume changed externally: %s -> %s",
                            previous_state,
                            current,
                        )
                        previous_state = current
                        volume, muted = current
                        callback(volume, muted)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("PulseAudio event subscription lost: %s, reconnecting...", exc)
                await asyncio.sleep(2)
