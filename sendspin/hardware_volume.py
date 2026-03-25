"""Hardware volume control backend for Linux PulseAudio/PipeWire."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from typing import TYPE_CHECKING, Any

from sendspin.volume_controller import VolumeChangeCallback

if TYPE_CHECKING:
    from sendspin.audio_devices import AudioDevice

AVAILABLE = False
UNAVAILABLE_REASON: str | None = None
if not sys.platform.startswith("linux"):
    UNAVAILABLE_REASON = "Hardware volume control is only supported on Linux."
else:
    try:
        import pulsectl_asyncio

        AVAILABLE = True
    except ImportError:
        UNAVAILABLE_REASON = (
            "pulsectl-asyncio package is not installed. "
            "Install it with: pip install pulsectl-asyncio"
        )
    except OSError as _exc:
        UNAVAILABLE_REASON = (
            f"PulseAudio system library failed to load: {_exc}. "
            "Install it with: sudo apt-get install libpulse0"
        )

logger = logging.getLogger(__name__)


async def async_check_available(audio_device: AudioDevice, timeout: float = 2.0) -> bool:
    """Check if PulseAudio is actually reachable at runtime.

    Returns True only if we can connect to the PulseAudio server.
    This goes beyond the module-level AVAILABLE check (which only verifies
    the library is installed) by testing the actual connection. The check
    is bounded by *timeout* seconds to keep CLI startup responsive when
    PulseAudio is down or unreachable.
    """
    if not AVAILABLE:
        return False

    try:
        async with asyncio.timeout(timeout):
            async with pulsectl_asyncio.PulseAsync("sendspin-cli-check") as client:
                await client.server_info()
                sink = await _get_sink(audio_device, client)
                if sink is None:
                    return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _sink_matches_device(sink: Any, device_name: str) -> bool:
    """Return True if *sink* corresponds to the given PortAudio *device_name*."""
    card_name = sink.proplist.get("alsa.card_name", "")
    alsa_name = sink.proplist.get("alsa.name", "")
    if card_name and alsa_name:
        # PortAudio format: "<card_name>: <alsa_name> (hw:...)"
        logger.debug(
            "Hardware volume: checking sink %r with alsa.card_name=%r and alsa.name=%r against device name %r",
            sink.name,
            card_name,
            alsa_name,
            device_name,
        )
        prefix = f"{card_name}: {alsa_name}"
        return device_name.startswith(prefix)
    logger.debug(
        "Hardware volume: sink %r missing alsa.card_name or alsa.name proplist fields, skipping match",
        sink.name,
    )
    return False


async def _get_sink(audio_device: AudioDevice, client: pulsectl_asyncio.PulseAsync) -> Any | None:
    """Return the PulseAudio sink corresponding to *audio_device*, or None if not found."""
    sinks = await client.sink_list()
    if not sinks:
        logger.error("Hardware volume: no PulseAudio sinks available")
        return None

    if audio_device.is_default or audio_device.name in (
        "default",
        "pipewire",
        "pulse",
        "pulseaudio",
    ):
        server_info = await client.server_info()
        sink = next((s for s in sinks if s.name == server_info.default_sink_name), None)
        if sink is None:
            sink = sinks[0]
        return sink

    device_name = audio_device.name
    matched = next(
        (s for s in sinks if _sink_matches_device(s, device_name)),
        None,
    )
    if matched is not None:
        logger.debug("Hardware volume: matched sink %r for device %r", matched.name, device_name)
    else:
        logger.debug("Hardware volume: no sink matched device %r", device_name)
    return matched


class HardwareVolumeController:
    """Controls Linux system output volume through PulseAudio API.

    Callers must verify that hardware volume is available (AVAILABLE is True)
    before creating an instance. Methods assume PulseAudio is functional.
    """

    def __init__(self, audio_device: AudioDevice) -> None:
        """Initialize the controller."""
        self._audio_device = audio_device
        self._watch_task: asyncio.Task[None] | None = None

    async def set_state(self, volume: int, *, muted: bool) -> None:
        """Set hardware volume and mute state.

        Args:
            volume: Volume level in range 0-100.
            muted: Whether output should be muted.

        Raises:
            ValueError: If volume is out of range.
            RuntimeError: If no PulseAudio sink is available.
        """
        if not 0 <= volume <= 100:
            raise ValueError(f"Volume must be 0-100, got {volume}")

        async with pulsectl_asyncio.PulseAsync("sendspin-cli") as client:
            sink = await _get_sink(self._audio_device, client)
            if sink is None:
                raise RuntimeError("No PulseAudio sink available for hardware volume")

            await client.volume_set_all_chans(sink, volume / 100.0)
            await client.mute(sink, muted)

    async def get_state(self) -> tuple[int, bool]:
        """Get current hardware volume and mute state.

        Returns:
            ``(volume, muted)`` tuple.

        Raises:
            RuntimeError: If no PulseAudio sink is available or volume
                cannot be read.
        """
        async with pulsectl_asyncio.PulseAsync("sendspin-cli") as client:
            sink = await _get_sink(self._audio_device, client)
            if sink is None:
                raise RuntimeError("No PulseAudio sink available for hardware volume read")

            volume_obj = sink.volume
            volume_flat = volume_obj.value_flat
            if volume_flat is None:
                # calculate flat volume by averaging all channels' volume levels
                if values := volume_obj.values:
                    volume_flat = sum(values) / len(values)

            if volume_flat is None:
                raise RuntimeError("Failed to read sink volume from PulseAudio")

            volume = max(0, min(100, int(round(float(volume_flat) * 100))))
            muted = bool(sink.mute)
            return volume, muted

    async def start_monitoring(self, callback: VolumeChangeCallback) -> None:
        """Start listening for external hardware volume changes.

        Calls *callback(volume, muted)* whenever the hardware volume or mute
        state changes compared to the last observed value.
        """
        if self._watch_task is not None:
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
            try:
                previous_state = await self.get_state()
            except RuntimeError:
                logger.debug("Failed to read initial hardware volume, retrying...")
                await asyncio.sleep(2)
                continue
            try:
                async with pulsectl_asyncio.PulseAsync("sendspin-cli-monitor") as pulse:
                    async for _event in pulse.subscribe_events("sink"):
                        try:
                            current = await self.get_state()
                        except RuntimeError:
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
