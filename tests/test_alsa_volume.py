from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import NoReturn
from types import SimpleNamespace

import sendspin.alsa_volume as _alsa_mod
from sendspin.alsa_volume import (
    AlsaVolumeController,
    async_check_alsa_available,
    find_mixer_element,
    parse_alsa_card,
)


# -- Helpers ------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


_AmixerExecFactory = Callable[..., Awaitable[_FakeProcess]]


def _amixer_exec(stdout: str, returncode: int = 0) -> _AmixerExecFactory:
    """Return an async factory that produces a fake amixer process."""
    proc = _FakeProcess(returncode=returncode, stdout=stdout.encode())

    async def factory(*args: object, **kwargs: object) -> _FakeProcess:
        return proc

    return factory


# -- parse_alsa_card ----------------------------------------------------------


def test_parse_card_from_hw_device() -> None:
    name = "snd_rpi_hifiberry_dacplus: HiFiBerry DAC+ HiFi pcm512x-hifi-0 (hw:1,0)"
    assert parse_alsa_card(name) == 1


def test_parse_card_from_hw0() -> None:
    name = "bcm2835 Headphones: - (hw:0,0)"
    assert parse_alsa_card(name) == 0


def test_parse_card_returns_none_for_virtual_device() -> None:
    assert parse_alsa_card("pipewire") is None
    assert parse_alsa_card("default") is None
    assert parse_alsa_card("pulse") is None
    assert parse_alsa_card("dmix") is None


# -- find_mixer_element -------------------------------------------------------


def test_find_mixer_element_digital(monkeypatch) -> None:
    """HiFiBerry DAC+ exposes 'Digital' as the volume control."""
    scontrols = (
        "Simple mixer control 'Analogue',0\n"
        "Simple mixer control 'Analogue Playback Boost',0\n"
        "Simple mixer control 'Auto Mute',0\n"
        "Simple mixer control 'Auto Mute Mono',0\n"
        "Simple mixer control 'Digital',0\n"
        "Simple mixer control 'DSP Program',0\n"
    )
    sget_with_pvolume = "  Capabilities: pvolume pswitch\n"
    sget_without_pvolume = "  Capabilities: enum\n"

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        if "scontrols" in argv:
            return _FakeProcess(stdout=scontrols.encode())
        # On real HiFiBerry DAC+ (PCM5122), both Analogue and Digital
        # have pvolume. The preference logic should pick Digital.
        if "Analogue" in argv or "Digital" in argv:
            return _FakeProcess(stdout=sget_with_pvolume.encode())
        return _FakeProcess(stdout=sget_without_pvolume.encode())

    async def exercise() -> str | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return await find_mixer_element(1)

    assert asyncio.run(exercise()) == "Digital"


def test_find_mixer_element_prefers_digital_over_analogue(monkeypatch) -> None:
    """When both Analogue and Digital have pvolume, Digital is preferred."""
    scontrols = "Simple mixer control 'Analogue',0\nSimple mixer control 'Digital',0\n"
    sget_with_pvolume = "  Capabilities: pvolume pswitch\n"

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        if "scontrols" in argv:
            return _FakeProcess(stdout=scontrols.encode())
        return _FakeProcess(stdout=sget_with_pvolume.encode())

    async def exercise() -> str | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return await find_mixer_element(1)

    assert asyncio.run(exercise()) == "Digital"


def test_find_mixer_element_master(monkeypatch) -> None:
    """HiFiBerry Amp+ uses 'Master' for volume."""
    scontrols = "Simple mixer control 'Channels',0\nSimple mixer control 'Master',0\n"
    sget_pvolume = "  Capabilities: pvolume pswitch\n"
    sget_no_pvolume = "  Capabilities: enum\n"

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        if "scontrols" in argv:
            return _FakeProcess(stdout=scontrols.encode())
        if "Master" in argv:
            return _FakeProcess(stdout=sget_pvolume.encode())
        return _FakeProcess(stdout=sget_no_pvolume.encode())

    async def exercise() -> str | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return await find_mixer_element(1)

    assert asyncio.run(exercise()) == "Master"


def test_find_mixer_element_none_when_no_controls(monkeypatch) -> None:
    """PCM5102A boards have no mixer controls."""

    async def exercise() -> str | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _amixer_exec(""))
        return await find_mixer_element(1)

    assert asyncio.run(exercise()) is None


def test_find_mixer_element_none_on_amixer_failure(monkeypatch) -> None:
    """Gracefully handle amixer failure (e.g., card not found)."""

    async def exercise() -> str | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _amixer_exec("", returncode=1))
        return await find_mixer_element(99)

    assert asyncio.run(exercise()) is None


def test_find_mixer_element_none_when_amixer_not_found(monkeypatch) -> None:
    """Gracefully handle amixer not being installed."""

    async def not_found(*args: object, **kwargs: object) -> NoReturn:
        raise FileNotFoundError("amixer")

    async def exercise() -> str | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", not_found)
        return await find_mixer_element(1)

    assert asyncio.run(exercise()) is None


def test_find_mixer_element_capability_scan_fallback(monkeypatch) -> None:
    """Falls back to capability scan for USB devices with non-standard names."""
    scontrols_output = (
        "Simple mixer control 'Mic',0\n"
        "Simple mixer control 'Mic',1\n"
        "Simple mixer control 'UMC202HD 192k Output',0\n"
        "Simple mixer control 'UMC202HD 192k Output',1\n"
    )
    sget_mic = "Simple mixer control 'Mic',0\n  Capabilities: cvolume cswitch\n"
    sget_output = (
        "Simple mixer control 'UMC202HD 192k Output',0\n"
        "  Capabilities: pvolume pswitch\n"
        "  Playback channels: Front Left - Front Right\n"
    )

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        if "scontrols" in argv:
            return _FakeProcess(stdout=scontrols_output.encode())
        # sget calls: return capabilities based on element name
        if "Mic" in argv:
            return _FakeProcess(stdout=sget_mic.encode())
        return _FakeProcess(stdout=sget_output.encode())

    async def exercise() -> str | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return await find_mixer_element(3)

    assert asyncio.run(exercise()) == "UMC202HD 192k Output"


def test_find_mixer_element_capability_scan_skips_capture_only(monkeypatch) -> None:
    """Capability scan skips elements that only have capture volume."""
    scontrols_output = "Simple mixer control 'Mic',0\n"
    sget_mic = "Simple mixer control 'Mic',0\n  Capabilities: cvolume cswitch\n"

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        if "scontrols" in argv:
            return _FakeProcess(stdout=scontrols_output.encode())
        return _FakeProcess(stdout=sget_mic.encode())

    async def exercise() -> str | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return await find_mixer_element(3)

    assert asyncio.run(exercise()) is None


# -- AlsaVolumeController.set_state ------------------------------------------


def test_set_state_calls_amixer_sset(monkeypatch) -> None:
    """set_state runs amixer sset with percentage and mute/unmute."""
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        calls.append(argv)
        return _FakeProcess()

    async def exercise() -> None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        ctrl = AlsaVolumeController(card=1, element="Digital")
        await ctrl.set_state(75, muted=False)

    asyncio.run(exercise())
    assert calls == [("amixer", "-M", "-c", "1", "sset", "Digital", "playback", "75%", "unmute")]


def test_set_state_muted(monkeypatch) -> None:
    """When muted, amixer is called with 'mute'."""
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        calls.append(argv)
        return _FakeProcess()

    async def exercise() -> None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        ctrl = AlsaVolumeController(card=1, element="Digital")
        await ctrl.set_state(50, muted=True)

    asyncio.run(exercise())
    assert calls == [("amixer", "-M", "-c", "1", "sset", "Digital", "playback", "50%", "mute")]


# -- AlsaVolumeController.get_state ------------------------------------------


def test_get_state_parses_amixer_output(monkeypatch) -> None:
    """get_state parses volume percentage and on/off from amixer sget."""
    amixer_output = (
        "Simple mixer control 'Digital',0\n"
        "  Capabilities: pvolume pswitch\n"
        "  Playback channels: Front Left - Front Right\n"
        "  Limits: Playback 0 - 207\n"
        "  Front Left: Playback 155 [74%] [-15.60dB] [on]\n"
        "  Front Right: Playback 155 [74%] [-15.60dB] [on]\n"
    )

    async def exercise() -> tuple[int, bool]:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _amixer_exec(amixer_output))
        ctrl = AlsaVolumeController(card=1, element="Digital")
        return await ctrl.get_state()

    volume, muted = asyncio.run(exercise())
    assert volume == 74
    assert muted is False


def test_get_state_detects_muted(monkeypatch) -> None:
    """get_state detects [off] as muted."""
    amixer_output = (
        "Simple mixer control 'PCM',0\n"
        "  Capabilities: pvolume pvolume-joined pswitch pswitch-joined\n"
        "  Playback channels: Mono\n"
        "  Limits: Playback -10239 - 400\n"
        "  Mono: Playback -4919 [50%] [-49.19dB] [off]\n"
    )

    async def exercise() -> tuple[int, bool]:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _amixer_exec(amixer_output))
        ctrl = AlsaVolumeController(card=0, element="PCM")
        return await ctrl.get_state()

    volume, muted = asyncio.run(exercise())
    assert volume == 50
    assert muted is True


def test_get_state_mono_channel(monkeypatch) -> None:
    """get_state handles mono devices (single channel line)."""
    amixer_output = (
        "Simple mixer control 'PCM',0\n"
        "  Capabilities: pvolume pvolume-joined pswitch pswitch-joined\n"
        "  Playback channels: Mono\n"
        "  Limits: Playback -10239 - 400\n"
        "  Mono: Playback 0 [96%] [0.00dB] [on]\n"
    )

    async def exercise() -> tuple[int, bool]:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _amixer_exec(amixer_output))
        ctrl = AlsaVolumeController(card=0, element="PCM")
        return await ctrl.get_state()

    volume, muted = asyncio.run(exercise())
    assert volume == 96
    assert muted is False


# -- AlsaVolumeController.start_monitoring ------------------------------------


def test_monitoring_detects_external_change(monkeypatch) -> None:
    """Monitor loop calls callback when volume changes between polls."""
    got_callback = asyncio.Event()
    callback_received: list[tuple[int, bool]] = []

    states = [
        (50, False),  # initial read
        (50, False),  # first poll — no change
        (75, False),  # second poll — changed -> triggers callback
    ]
    state_iter = iter(states)

    def on_change(volume: int, muted: bool) -> None:
        callback_received.append((volume, muted))
        got_callback.set()

    async def exercise() -> None:
        monkeypatch.setattr(_alsa_mod, "_POLL_INTERVAL_S", 0.0)
        ctrl = AlsaVolumeController(card=0, element="PCM")

        async def fake_get() -> tuple[int, bool]:
            try:
                return next(state_iter)
            except StopIteration:
                # Keep returning last state after sequence ends
                return (75, False)

        ctrl.get_state = fake_get  # type: ignore[assignment]

        await ctrl.start_monitoring(on_change)
        try:
            await asyncio.wait_for(got_callback.wait(), timeout=2.0)
        finally:
            await ctrl.stop_monitoring()

        assert callback_received == [(75, False)]

    asyncio.run(exercise())


# -- async_check_alsa_available -----------------------------------------------


def test_alsa_available_for_hw_device_with_mixer(monkeypatch) -> None:
    """Returns (card, element) for a hw: device with mixer controls."""
    scontrols = "Simple mixer control 'Digital',0\n"
    sget_pvolume = "  Capabilities: pvolume pswitch\n"

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        if "scontrols" in argv:
            return _FakeProcess(stdout=scontrols.encode())
        return _FakeProcess(stdout=sget_pvolume.encode())

    async def exercise() -> tuple[int, str] | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        device = SimpleNamespace(name="HiFiBerry DAC+: pcm512x (hw:1,0)", is_default=False)
        return await async_check_alsa_available(device)

    result = asyncio.run(exercise())
    assert result == (1, "Digital")


def test_alsa_not_available_for_virtual_device() -> None:
    """Returns None for virtual devices (no hw: in name)."""

    async def exercise() -> tuple[int, str] | None:
        device = SimpleNamespace(name="pipewire", is_default=False)
        return await async_check_alsa_available(device)

    assert asyncio.run(exercise()) is None


def test_alsa_not_available_when_no_mixer_controls(monkeypatch) -> None:
    """Returns None when card has no mixer elements (PCM5102A)."""

    async def exercise() -> tuple[int, str] | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _amixer_exec(""))
        device = SimpleNamespace(name="DAC Zero (hw:2,0)", is_default=False)
        return await async_check_alsa_available(device)

    assert asyncio.run(exercise()) is None


# -- Simulated HiFiBerry DAC+ HAT scenario -----------------------------------
# Reproduces the exact setup from the GitHub issues: a Raspberry Pi with a
# HiFiBerry DAC+ HAT on card 1, using the real device names and amixer output
# that users reported.


# Real amixer output from a HiFiBerry DAC+ (PCM5122 chip)
_HIFIBERRY_SCONTROLS = (
    "Simple mixer control 'Analogue',0\n"
    "Simple mixer control 'Analogue Playback Boost',0\n"
    "Simple mixer control 'Auto Mute',0\n"
    "Simple mixer control 'Auto Mute Mono',0\n"
    "Simple mixer control 'Digital',0\n"
    "Simple mixer control 'DSP Program',0\n"
)

_HIFIBERRY_SGET_74 = (
    "Simple mixer control 'Digital',0\n"
    "  Capabilities: pvolume pswitch\n"
    "  Playback channels: Front Left - Front Right\n"
    "  Limits: Playback 0 - 207\n"
    "  Front Left: Playback 155 [74%] [-15.60dB] [on]\n"
    "  Front Right: Playback 155 [74%] [-15.60dB] [on]\n"
)

# The exact PortAudio device name from the issue logs
_HIFIBERRY_DEVICE_NAME = "snd_rpi_hifiberry_dacplus: HiFiBerry DAC+ HiFi pcm512x-hifi-0 (hw:1,0)"


def test_hifiberry_dac_discovery(monkeypatch) -> None:
    """Full discovery flow for a HiFiBerry DAC+ HAT on card 1."""
    sget_with_pvolume = "  Capabilities: pvolume pswitch\n"
    sget_without_pvolume = "  Capabilities: enum\n"

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        if "scontrols" in argv:
            return _FakeProcess(stdout=_HIFIBERRY_SCONTROLS.encode())
        # Both Analogue and Digital have pvolume on real hardware.
        if "Analogue" in argv or "Digital" in argv:
            return _FakeProcess(stdout=sget_with_pvolume.encode())
        return _FakeProcess(stdout=sget_without_pvolume.encode())

    async def exercise() -> tuple[int, str] | None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        device = SimpleNamespace(name=_HIFIBERRY_DEVICE_NAME, is_default=False)
        return await async_check_alsa_available(device)

    result = asyncio.run(exercise())
    assert result == (1, "Digital")


def test_hifiberry_dac_set_and_get_volume(monkeypatch) -> None:
    """Set and read volume on a simulated HiFiBerry DAC+ HAT."""
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv: object, **kwargs: object) -> _FakeProcess:
        calls.append(argv)
        # Return sget-style output for get_state calls
        return _FakeProcess(stdout=_HIFIBERRY_SGET_74.encode())

    async def exercise() -> None:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        ctrl = AlsaVolumeController(card=1, element="Digital")

        # Server sends volume command -> controller sets ALSA mixer
        await ctrl.set_state(74, muted=False)
        assert calls[-1] == (
            "amixer",
            "-M",
            "-c",
            "1",
            "sset",
            "Digital",
            "playback",
            "74%",
            "unmute",
        )

        # Read back the volume
        volume, muted = await ctrl.get_state()
        assert calls[-1] == ("amixer", "-M", "-c", "1", "sget", "Digital")
        assert volume == 74
        assert muted is False

    asyncio.run(exercise())
