"""Tests for audio device resolution, especially ALSA CARD=name handling."""

from __future__ import annotations

import os
from unittest.mock import mock_open

import pytest

import sendspin.audio_devices as _devices_mod
from sendspin.audio_devices import (
    AudioDevice,
    _ALSA_CARD_DEV_RE,
    _get_alsa_card_number,
    _try_alsa_card_device,
    resolve_audio_device,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device(
    index: int, name: str, *, channels: int = 2, sample_rate: float = 44100.0
) -> AudioDevice:
    return AudioDevice(
        index=index,
        name=name,
        output_channels=channels,
        sample_rate=sample_rate,
        is_default=False,
    )


# Simulated PortAudio device list matching the issue's hardware
_HIFIBERRY_DEVICE = _make_device(
    2,
    "snd_rpi_hifiberry_dacplus: HiFiBerry DAC+ HiFi pcm512x-hifi-0 (hw:1,0)",
)
_LOOPBACK_0_DEVICE = _make_device(0, "Loopback: PCM (hw:0,0)", channels=32)
_LOOPBACK_1_DEVICE = _make_device(1, "Loopback: PCM (hw:0,1)", channels=32)
_SYSDEFAULT_DEVICE = _make_device(3, "sysdefault", channels=128, sample_rate=48000.0)
_DEFAULT_DEVICE = _make_device(13, "default (default)", channels=128, sample_rate=48000.0)

_ALL_DEVICES = [
    _LOOPBACK_0_DEVICE,
    _LOOPBACK_1_DEVICE,
    _HIFIBERRY_DEVICE,
    _SYSDEFAULT_DEVICE,
    _DEFAULT_DEVICE,
]


# ---------------------------------------------------------------------------
# _ALSA_CARD_DEV_RE
# ---------------------------------------------------------------------------


def test_regex_matches_hw_card_dev() -> None:
    m = _ALSA_CARD_DEV_RE.fullmatch("hw:CARD=sndrpihifiberry,DEV=0")
    assert m is not None
    assert m.group(1) == "sndrpihifiberry"
    assert m.group(2) == "0"


def test_regex_matches_plughw_card_dev() -> None:
    m = _ALSA_CARD_DEV_RE.fullmatch("plughw:CARD=Loopback,DEV=1")
    assert m is not None
    assert m.group(1) == "Loopback"
    assert m.group(2) == "1"


def test_regex_matches_card_without_dev() -> None:
    m = _ALSA_CARD_DEV_RE.fullmatch("sysdefault:CARD=sndrpihifiberry")
    assert m is not None
    assert m.group(1) == "sndrpihifiberry"
    assert m.group(2) is None


def test_regex_does_not_match_simple_name() -> None:
    assert _ALSA_CARD_DEV_RE.fullmatch("dmix") is None
    assert _ALSA_CARD_DEV_RE.fullmatch("sysdefault") is None
    assert _ALSA_CARD_DEV_RE.fullmatch("hw:0,0") is None


# ---------------------------------------------------------------------------
# _get_alsa_card_number
# ---------------------------------------------------------------------------


def test_get_alsa_card_number_found(monkeypatch) -> None:
    card_ids = {0: "Loopback", 1: "sndrpihifiberry"}

    def fake_listdir(path: str) -> list[str]:
        if path == "/proc/asound":
            return ["card0", "card1", "seq"]
        raise FileNotFoundError(path)

    def fake_open(path, *args, **kwargs):
        parts = path.replace("\\", "/").split("/")
        card_dir = parts[-2]
        card_num = int(card_dir[4:])
        content = card_ids.get(card_num, "unknown") + "\n"
        m = mock_open(read_data=content)()
        return m

    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr("builtins.open", fake_open)

    assert _get_alsa_card_number("sndrpihifiberry") == 1
    assert _get_alsa_card_number("Loopback") == 0


def test_get_alsa_card_number_not_found(monkeypatch) -> None:
    def fake_listdir(path: str) -> list[str]:
        if path == "/proc/asound":
            return ["card0"]
        raise FileNotFoundError(path)

    def fake_open(path, *args, **kwargs):
        m = mock_open(read_data="Loopback\n")()
        return m

    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr("builtins.open", fake_open)

    assert _get_alsa_card_number("nonexistent") is None


def test_get_alsa_card_number_proc_missing(monkeypatch) -> None:
    monkeypatch.setattr(os, "listdir", lambda _: (_ for _ in ()).throw(OSError("no /proc")))
    assert _get_alsa_card_number("anything") is None


# ---------------------------------------------------------------------------
# _try_alsa_card_device
# ---------------------------------------------------------------------------


def test_try_alsa_card_device_hw(monkeypatch) -> None:
    monkeypatch.setattr(
        _devices_mod, "_get_alsa_card_number", lambda name: 1 if name == "sndrpihifiberry" else None
    )

    result = _try_alsa_card_device("hw:CARD=sndrpihifiberry,DEV=0", _ALL_DEVICES)
    assert result is _HIFIBERRY_DEVICE


def test_try_alsa_card_device_loopback(monkeypatch) -> None:
    monkeypatch.setattr(
        _devices_mod, "_get_alsa_card_number", lambda name: 0 if name == "Loopback" else None
    )

    result = _try_alsa_card_device("hw:CARD=Loopback,DEV=0", _ALL_DEVICES)
    assert result is _LOOPBACK_0_DEVICE


def test_try_alsa_card_device_plughw(monkeypatch) -> None:
    monkeypatch.setattr(
        _devices_mod, "_get_alsa_card_number", lambda name: 1 if name == "sndrpihifiberry" else None
    )

    result = _try_alsa_card_device("plughw:CARD=sndrpihifiberry,DEV=0", _ALL_DEVICES)
    assert result is _HIFIBERRY_DEVICE


def test_try_alsa_card_device_no_card_match(monkeypatch) -> None:
    monkeypatch.setattr(_devices_mod, "_get_alsa_card_number", lambda _: None)

    result = _try_alsa_card_device("hw:CARD=nonexistent,DEV=0", _ALL_DEVICES)
    assert result is None


def test_try_alsa_card_device_simple_name(monkeypatch) -> None:
    """Non-CARD= names are ignored by _try_alsa_card_device."""
    monkeypatch.setattr(_devices_mod, "_get_alsa_card_number", lambda _: 0)

    result = _try_alsa_card_device("dmix", _ALL_DEVICES)
    assert result is None


# ---------------------------------------------------------------------------
# resolve_audio_device integration
# ---------------------------------------------------------------------------


def test_resolve_audio_device_hw_card_name(monkeypatch) -> None:
    """hw:CARD=X,DEV=Y is resolved to the matching PortAudio device on Linux."""
    monkeypatch.setattr(_devices_mod, "query_devices", lambda: _ALL_DEVICES)
    monkeypatch.setattr(
        _devices_mod, "_get_alsa_card_number", lambda name: 1 if name == "sndrpihifiberry" else None
    )
    monkeypatch.setattr(_devices_mod.sys, "platform", "linux")

    device = resolve_audio_device("hw:CARD=sndrpihifiberry,DEV=0")
    assert device is _HIFIBERRY_DEVICE


def test_resolve_audio_device_by_index(monkeypatch) -> None:
    monkeypatch.setattr(_devices_mod, "query_devices", lambda: _ALL_DEVICES)

    device = resolve_audio_device("2")
    assert device is _HIFIBERRY_DEVICE


def test_resolve_audio_device_by_name_prefix(monkeypatch) -> None:
    monkeypatch.setattr(_devices_mod, "query_devices", lambda: _ALL_DEVICES)

    device = resolve_audio_device("snd_rpi_hifiberry")
    assert device is _HIFIBERRY_DEVICE


def test_resolve_audio_device_unknown_raises(monkeypatch) -> None:
    monkeypatch.setattr(_devices_mod, "query_devices", lambda: _ALL_DEVICES)
    monkeypatch.setattr(_devices_mod, "_get_alsa_card_number", lambda _: None)
    monkeypatch.setattr(_devices_mod, "_try_alsa_device", lambda _: None)
    monkeypatch.setattr(_devices_mod.sys, "platform", "linux")

    with pytest.raises(ValueError, match="not found"):
        resolve_audio_device("hw:CARD=nonexistent,DEV=0")
