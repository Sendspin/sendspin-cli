"""Audio device listing, resolution, and format validation for the CLI."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiosendspin.models.player import SupportedAudioFormat

    from sendspin.audio import AudioDevice

logger = logging.getLogger(__name__)

PORTAUDIO_NOT_FOUND_MESSAGE = """Error: PortAudio library not found.

Please install PortAudio for your system:
  • Debian/Ubuntu/Raspberry Pi: sudo apt-get install libportaudio2
  • macOS: brew install portaudio
  • Other systems: https://www.portaudio.com/"""


class DeviceError(Exception):
    """Raised when an audio device cannot be found or opened."""


def list_audio_devices() -> None:
    """List all available audio output devices and print to stdout."""
    try:
        from sendspin.audio import query_devices
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            sys.exit(1)
        raise

    try:
        devices = query_devices()

        print("Available audio output devices:")
        print()
        for device in devices:
            default_marker = " (default)" if device.is_default else ""
            print(
                f"  [{device.index}] {device.name}{default_marker}\n"
                f"       Channels: {device.output_channels}, "
                f"Sample rate: {device.sample_rate} Hz"
            )
        if devices:
            print("\nTo select an audio device:\n  sendspin --audio-device 0")

        if sys.platform.startswith("linux"):
            alsa_devices = _list_alsa_devices()
            if alsa_devices:
                print("\nALSA devices (use by name with --audio-device):")
                print()
                for name, description in alsa_devices:
                    print(f"  {name}")
                    if description:
                        print(f"       {description}")

    except Exception as e:  # noqa: BLE001
        print(f"Error listing audio devices: {e}")
        sys.exit(1)


def _list_alsa_devices() -> list[tuple[str, str]]:
    """List ALSA PCM devices from ``aplay -L``.

    Returns a list of (device_name, description) tuples for output devices.
    Returns an empty list if aplay is not available.
    """
    import subprocess

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
        DeviceError: If the device cannot be found.
    """
    from sendspin.audio import query_devices

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
            raise DeviceError("Default audio device not found.")

        # Not found in enumeration — try as a raw ALSA device name
        device = _try_alsa_device(device_arg)
        if device is None:
            raise DeviceError(
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
    import sounddevice

    from sendspin.audio import AudioDevice

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


def resolve_audio_format(
    format_arg: str | None, device: AudioDevice
) -> SupportedAudioFormat | None:
    """Parse and validate a preferred audio format against the audio device.

    Args:
        format_arg: Format string (e.g., "flac:48000:24:2") or None.
        device: The resolved audio device to validate against.

    Returns:
        The parsed SupportedAudioFormat, or None if no format was specified.

    Raises:
        DeviceError: If the format string is invalid or unsupported by the device.
    """
    if format_arg is None:
        return None

    from sendspin.audio import parse_audio_format, validate_audio_format

    try:
        fmt = parse_audio_format(format_arg)
    except ValueError as e:
        raise DeviceError(str(e)) from None

    if not validate_audio_format(fmt, device):
        raise DeviceError(
            f"Audio format '{format_arg}' is not supported by device "
            f"'{device.name}' ({device.device_id})."
        )

    logger.info("Using preferred audio format: %s", format_arg)
    return fmt
