from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path

from sendspin.settings import ClientSettings
from sendspin.tui.app import AppState
from sendspin.tui.keyboard import keyboard_loop


class _FakeAudioHandler:
    def __init__(self) -> None:
        self.volume = 50
        self.muted = False

    def set_volume(self, volume: int, *, muted: bool) -> None:
        self.volume = volume
        self.muted = muted


class _FakeUI:
    def __init__(self) -> None:
        self.highlighted: list[str] = []

    def is_server_selector_visible(self) -> bool:
        return False

    def highlight_shortcut(self, shortcut: str) -> None:
        self.highlighted.append(shortcut)

    def hide_server_selector(self) -> None:
        return

    def move_server_selection(self, _delta: int) -> None:
        return


class _FakeClient:
    static_delay_ms = 0.0

    async def send_group_command(self, *_args: object, **_kwargs: object) -> None:
        return


def _make_settings(tmp_path: Path) -> ClientSettings:
    return ClientSettings(_settings_file=tmp_path / "settings.json")


def test_keyboard_loop_requests_shutdown_on_quit(monkeypatch: object, tmp_path: Path) -> None:
    keys: queue.Queue[str] = queue.Queue()
    keys.put("q")
    release_reader = threading.Event()

    def fake_readkey() -> str:
        try:
            return keys.get(timeout=0.05)
        except queue.Empty as err:
            release_reader.wait(timeout=0.05)
            raise KeyboardInterrupt from err

    monkeypatch.setattr("sendspin.tui.keyboard.readchar.readkey", fake_readkey)

    shutdown_calls: list[str] = []

    def request_shutdown() -> None:
        shutdown_calls.append("shutdown")
        release_reader.set()

    asyncio.run(
        asyncio.wait_for(
            keyboard_loop(
                _FakeClient(),
                AppState(),
                _FakeAudioHandler(),
                _FakeUI(),
                _make_settings(tmp_path),
                lambda: None,
                lambda: asyncio.sleep(0),
                request_shutdown,
            ),
            timeout=1.0,
        )
    )

    assert shutdown_calls == ["shutdown"]


def test_keyboard_loop_requests_shutdown_on_reader_failure(
    monkeypatch: object, tmp_path: Path
) -> None:
    def fake_readkey() -> str:
        raise OSError("stdin closed")

    monkeypatch.setattr("sendspin.tui.keyboard.readchar.readkey", fake_readkey)

    shutdown_calls: list[str] = []

    def request_shutdown() -> None:
        shutdown_calls.append("shutdown")

    asyncio.run(
        asyncio.wait_for(
            keyboard_loop(
                _FakeClient(),
                AppState(),
                _FakeAudioHandler(),
                _FakeUI(),
                _make_settings(tmp_path),
                lambda: None,
                lambda: asyncio.sleep(0),
                request_shutdown,
            ),
            timeout=1.0,
        )
    )

    assert shutdown_calls
