"""Audio device resolution and ALSA device listing."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

import sounddevice

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AudioDevice:
    """Represents an audio output device.

    Attributes:
        index: PortAudio device index, or None for string-named devices.
        name: Human-readable device name.
        output_channels: Number of output channels supported.
        sample_rate: Default sample rate in Hz.
        is_default: Whether this is the system default output device.
        alsa_device_name: Raw ALSA device name for direct access (e.g. dmix
            plugin devices). When set, this is used instead of index to open
            the device via sounddevice.
    """

    index: int | None
    name: str
    output_channels: int
    sample_rate: float
    is_default: bool
    alsa_device_name: str | None = None

    def __post_init__(self) -> None:
        if self.index is None and self.alsa_device_name is None:
            raise ValueError("AudioDevice must have an index or alsa_device_name")

    @property
    def device_id(self) -> int | str:
        """Return the identifier to pass to sounddevice APIs."""
        if self.alsa_device_name is not None:
            return self.alsa_device_name
        assert self.index is not None  # guaranteed by __post_init__
        return self.index


def query_devices() -> list[AudioDevice]:
    """Query all available audio output devices.

    Returns:
        List of AudioDevice objects for devices with output channels.
    """
    devices = sounddevice.query_devices()
    default_output = int(sounddevice.default.device[1])

    result: list[AudioDevice] = []
    for i in range(len(devices)):
        dev = devices[i]
        if dev["max_output_channels"] > 0:
            result.append(
                AudioDevice(
                    index=i,
                    name=str(dev["name"]),
                    output_channels=int(dev["max_output_channels"]),
                    sample_rate=float(dev["default_samplerate"]),
                    is_default=(i == default_output),
                )
            )
    return result


def list_alsa_devices() -> list[tuple[str, str]]:
    """List ALSA PCM devices from ``aplay -L``.

    Returns a list of (device_name, description) tuples for output devices.
    Returns an empty list if aplay is not available.
    """
    try:
        result = subprocess.run(
            ["aplay", "-L"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    devices: list[tuple[str, str]] = []
    lines = result.stdout.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Device names start at column 0, descriptions are indented
        if line and not line[0].isspace():
            name = line.strip()
            description = ""
            if i + 1 < len(lines) and lines[i + 1].startswith("    "):
                description = lines[i + 1].strip()
            devices.append((name, description))
        i += 1

    return devices


def resolve_audio_device(device_arg: str | None) -> AudioDevice:
    """Resolve audio device from a CLI argument.

    Args:
        device_arg: Device specifier (index number, name prefix, raw ALSA device
            name, or None for default).

    Returns:
        The resolved AudioDevice.

    Raises:
        ValueError: If the device cannot be found.
    """
    devices = query_devices()

    # Find device by: default, index, or name prefix
    if device_arg is None:
        device = next((d for d in devices if d.is_default), None)
    elif device_arg.isnumeric():
        device_id = int(device_arg)
        device = next((d for d in devices if d.index == device_id), None)
    else:
        device = next((d for d in devices if d.name.startswith(device_arg)), None)

    if device is None:
        if device_arg is None:
            raise ValueError("Default audio device not found.")

        # Not found in enumeration — try as a raw ALSA device name
        device = _try_alsa_device(device_arg)
        if device is None:
            raise ValueError(
                f"Audio device '{device_arg}' not found in enumerated devices "
                "and could not be opened as an ALSA device."
            )

    logger.info("Using audio device %s: %s", device.device_id, device.name)
    return device


def _try_alsa_device(name: str) -> AudioDevice | None:
    """Try to open a raw ALSA device by name.

    This allows using ALSA plugin devices (dmix, plug, etc.) that are not
    enumerated by PortAudio but can be opened by name. This is needed for
    setups like dual mono where multiple clients share hardware via dmix.

    Returns:
        An AudioDevice if the ALSA device could be opened, None otherwise.
    """
    try:
        sounddevice.check_output_settings(device=name)
    except sounddevice.PortAudioError:
        return None

    # Try to query device info from PortAudio
    try:
        info = sounddevice.query_devices(name, "output")
        channels = int(info["max_output_channels"])
        sample_rate = float(info["default_samplerate"])
    except (sounddevice.PortAudioError, ValueError):
        # PortAudio can't enumerate this device — use safe defaults.
        # The actual format is negotiated with the server later.
        channels = 2
        sample_rate = 48000.0

    return AudioDevice(
        index=None,
        name=name,
        output_channels=channels,
        sample_rate=sample_rate,
        is_default=False,
        alsa_device_name=name,
    )
