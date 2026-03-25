from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiosendspin.models.types import PlayerCommand

from sendspin.settings import ClientSettings
from sendspin.tui.app import AppArgs, AppState, SendspinApp
from sendspin.tui.keyboard import CommandHandler


class _FakeAudioHandler:
    def __init__(self, *, volume: int, muted: bool) -> None:
        self.volume = volume
        self.muted = muted
        self.calls: list[tuple[int, bool]] = []

    def set_volume(self, volume: int, *, muted: bool) -> None:
        self.calls.append((volume, muted))
        self.volume = volume
        self.muted = muted


class _FakeUI:
    def __init__(self) -> None:
        self.events: list[str] = []

    def add_event(self, event: str) -> None:
        self.events.append(event)


def _make_settings(tmp_path: Path) -> ClientSettings:
    return ClientSettings(_settings_file=tmp_path / "settings.json")


def _make_app(tmp_path: Path) -> SendspinApp:
    args = AppArgs(
        audio_device=SimpleNamespace(index=0, name="Fake Device"),
        client_id="test-client",
        client_name="Test Client",
        settings=_make_settings(tmp_path),
        use_mpris=False,
    )
    return SendspinApp(args)


def test_tui_volume_command_uses_audio_handler_muted_state(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._audio_handler = _FakeAudioHandler(volume=41, muted=False)
    app._ui = _FakeUI()
    app._state.player_muted = True

    payload = SimpleNamespace(
        player=SimpleNamespace(command=PlayerCommand.VOLUME, volume=67, mute=None)
    )

    app._handle_server_command(payload)

    assert app._audio_handler.calls == [(67, False)]


def test_tui_mute_command_uses_audio_handler_volume_state(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._audio_handler = _FakeAudioHandler(volume=53, muted=False)
    app._ui = _FakeUI()
    app._state.player_volume = 12

    payload = SimpleNamespace(
        player=SimpleNamespace(command=PlayerCommand.MUTE, volume=None, mute=True)
    )

    app._handle_server_command(payload)

    assert app._audio_handler.calls == [(53, True)]


def test_keyboard_volume_change_uses_audio_handler_state(tmp_path: Path) -> None:
    state = AppState(player_volume=10, player_muted=True)
    audio_handler = _FakeAudioHandler(volume=41, muted=False)
    ui = _FakeUI()
    handler = CommandHandler(
        get_client=lambda: SimpleNamespace(),
        state=state,
        audio_handler=audio_handler,
        ui=ui,
        settings=_make_settings(tmp_path),
    )

    handler.change_player_volume(5)

    assert audio_handler.calls == [(46, False)]


def test_keyboard_toggle_mute_uses_audio_handler_state(tmp_path: Path) -> None:
    state = AppState(player_volume=10, player_muted=True)
    audio_handler = _FakeAudioHandler(volume=41, muted=False)
    ui = _FakeUI()
    handler = CommandHandler(
        get_client=lambda: SimpleNamespace(),
        state=state,
        audio_handler=audio_handler,
        ui=ui,
        settings=_make_settings(tmp_path),
    )

    handler.toggle_player_mute()

    assert audio_handler.calls == [(41, True)]
