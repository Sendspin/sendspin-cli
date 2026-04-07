"""Shared test fixtures and configuration."""

from __future__ import annotations

import sys
import types


def pytest_configure(config) -> None:
    """Install a stub for sounddevice before any test module imports it.

    PortAudio is not available in CI, so we replace the sounddevice module
    with a minimal stub that satisfies the import-time requirements of
    sendspin.audio_devices (and related modules).
    """
    if "sounddevice" not in sys.modules:
        stub = types.ModuleType("sounddevice")

        class PortAudioError(Exception):
            pass

        stub.PortAudioError = PortAudioError  # type: ignore[attr-defined]
        stub.query_devices = lambda *a, **kw: []  # type: ignore[attr-defined]
        stub.check_output_settings = lambda *a, **kw: None  # type: ignore[attr-defined]
        stub.default = types.SimpleNamespace(device=(0, 0))  # type: ignore[attr-defined]
        sys.modules["sounddevice"] = stub
