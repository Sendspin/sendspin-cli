"""ALSA mixer volume control backend for Linux."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import sys
from typing import TYPE_CHECKING

from sendspin.volume_controller import VolumeChangeCallback

if TYPE_CHECKING:
    from sendspin.audio import AudioDevice

logger = logging.getLogger(__name__)

AVAILABLE = sys.platform.startswith("linux") and shutil.which("amixer") is not None

_HW_CARD_RE = re.compile(r"\bhw:(\d+)")

_SCONTROL_RE = re.compile(r"Simple mixer control '([^']+)'")
_VOLUME_RE = re.compile(r"\[(\d+)%\]")
_SWITCH_RE = re.compile(r"\[(on|off)\]")

_POLL_INTERVAL_S = 1.0

# Well-known ALSA mixer element names, in priority order.
# When multiple elements have playback volume, prefer these over others.
# - Digital: HiFiBerry DAC+/DAC2/Amp2, most I2S DAC HATs (PCM5122)
# - Master: HiFiBerry Amp+, generic ALSA cards, many USB interfaces
# - PCM: bcm2835 headphones, some USB DACs
_PREFERRED_ELEMENTS: tuple[str, ...] = ("Digital", "Master", "PCM")


def parse_alsa_card(device_name: str) -> int | None:
    """Extract the ALSA card number from a PortAudio device name.

    PortAudio names hardware devices like:
      "snd_rpi_hifiberry_dacplus: ... (hw:1,0)"

    Returns the card index or None for virtual devices.
    """
    m = _HW_CARD_RE.search(device_name)
    return int(m.group(1)) if m else None


async def _has_playback_volume(card: int, element: str) -> bool:
    """Check if an ALSA mixer element has playback volume capability."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "amixer",
            "-M",
            "-c",
            str(card),
            "sget",
            element,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        return False
    if proc.returncode != 0:
        return False
    return "pvolume" in stdout.decode()


async def find_mixer_element(card: int) -> str | None:
    """Discover the playback volume mixer element on an ALSA card.

    Runs ``amixer -c <card> scontrols``, then checks each element for
    playback volume (``pvolume``) capability.  Prefers well-known element
    names (Digital, Master, PCM) when multiple elements have playback
    volume.  Returns the best match, or None if no element has playback
    volume control.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "amixer",
            "-c",
            str(card),
            "scontrols",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        logger.debug("amixer not found on this system")
        return None

    if proc.returncode != 0:
        logger.debug("amixer -c %d scontrols failed (exit %d)", card, proc.returncode)
        return None

    available: list[str] = _SCONTROL_RE.findall(stdout.decode())
    if not available:
        logger.debug("ALSA card %d has no mixer controls", card)
        return None

    seen: set[str] = set()
    pvolume_elements: list[str] = []
    for element in available:
        if element in seen:
            continue
        seen.add(element)
        if await _has_playback_volume(card, element):
            pvolume_elements.append(element)

    if not pvolume_elements:
        logger.debug(
            "ALSA card %d: no playback volume element among %s",
            card,
            sorted(seen),
        )
        return None

    # Prefer well-known element names used by common DAC HATs.
    for preferred in _PREFERRED_ELEMENTS:
        if preferred in pvolume_elements:
            logger.debug("ALSA card %d: selected preferred mixer element %r", card, preferred)
            return preferred

    # Fallback: first element with pvolume (e.g. USB DACs with non-standard names).
    selected = pvolume_elements[0]
    logger.debug("ALSA card %d: selected mixer element %r", card, selected)
    return selected


async def async_check_alsa_available(
    audio_device: AudioDevice,
) -> tuple[int, str] | None:
    """Check if ALSA mixer volume control is available for a device.

    Returns ``(card_number, mixer_element)`` if available, or None.
    """
    if not AVAILABLE:
        return None

    card = parse_alsa_card(audio_device.name)
    if card is None:
        return None

    element = await find_mixer_element(card)
    if element is None:
        return None

    return card, element


class AlsaVolumeController:
    """Controls audio volume directly via ALSA mixer using amixer.

    This bypasses PulseAudio/PipeWire and sets the hardware mixer element
    on the ALSA card, giving true hardware volume control on DAC HATs.
    """

    def __init__(self, card: int, element: str) -> None:
        self._card = str(card)
        self._element = element
        self._watch_task: asyncio.Task[None] | None = None

    async def set_state(self, volume: int, *, muted: bool) -> None:
        """Set ALSA mixer volume and mute state."""
        if not 0 <= volume <= 100:
            raise ValueError(f"Volume must be 0-100, got {volume}")

        mute_arg = "mute" if muted else "unmute"
        proc = await asyncio.create_subprocess_exec(
            "amixer",
            "-M",
            "-c",
            self._card,
            "sset",
            self._element,
            "playback",
            f"{volume}%",
            mute_arg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"amixer sset failed (exit {proc.returncode}): "
                f"{stderr.decode().strip() if stderr else '(empty)'}"
            )

    async def get_state(self) -> tuple[int, bool]:
        """Read ALSA mixer volume and mute state."""
        proc = await asyncio.create_subprocess_exec(
            "amixer",
            "-M",
            "-c",
            self._card,
            "sget",
            self._element,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"amixer sget failed (exit {proc.returncode}): "
                f"{stderr.decode().strip() if stderr else '(empty)'}"
            )

        output = stdout.decode()
        vol_match = _VOLUME_RE.search(output)
        switch_match = _SWITCH_RE.search(output)

        if vol_match is None:
            raise RuntimeError(f"Could not parse volume from amixer output: {output!r}")

        volume = int(vol_match.group(1))
        muted = switch_match.group(1) == "off" if switch_match else False
        return volume, muted

    async def start_monitoring(self, callback: VolumeChangeCallback) -> None:
        """Start polling for external ALSA volume changes."""
        if self._watch_task is not None:
            return
        self._watch_task = asyncio.get_running_loop().create_task(self._poll_loop(callback))

    async def stop_monitoring(self) -> None:
        """Stop the monitoring poll loop."""
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None

    async def _poll_loop(self, callback: VolumeChangeCallback) -> None:
        """Poll amixer for volume changes and invoke callback on change."""
        while True:
            try:
                previous = await self.get_state()
            except RuntimeError:
                logger.debug("Failed to read initial ALSA volume, retrying...")
                await asyncio.sleep(2)
                continue
            break

        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            try:
                current = await self.get_state()
            except RuntimeError:
                continue
            if current != previous:
                logger.debug("ALSA volume changed externally: %s -> %s", previous, current)
                previous = current
                volume, muted = current
                callback(volume, muted)
