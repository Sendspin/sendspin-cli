from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiosendspin.models.core import ServerHelloPayload
from aiosendspin.models.types import ConnectionReason, Roles

from sendspin.settings import ClientSettings
from sendspin.tui.app import AppArgs, SendspinApp


class _FakeUI:
    def __init__(self) -> None:
        self.visualizer_enabled_calls: list[bool] = []

    def set_visualizer_enabled(self, enabled: bool) -> None:
        self.visualizer_enabled_calls.append(enabled)


def _make_app(tmp_path: Path) -> SendspinApp:
    args = AppArgs(
        audio_device=SimpleNamespace(index=0, name="Fake Device"),
        client_id="test-client",
        client_name="Test Client",
        settings=ClientSettings(_settings_file=tmp_path / "settings.json"),
        use_mpris=False,
    )
    return SendspinApp(args)


def _payload(active_roles: list[str]) -> ServerHelloPayload:
    return ServerHelloPayload(
        server_id="srv",
        name="srv",
        version=1,
        active_roles=active_roles,
        connection_reason=ConnectionReason.DISCOVERY,
    )


def test_server_hello_without_visualizer_role_hides_panel(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._visualizer_enabled = True
    app._ui = _FakeUI()

    app._handle_server_hello(_payload(active_roles=["player@v1", "controller@v1"]))

    assert app._ui.visualizer_enabled_calls == [False]


def test_server_hello_with_visualizer_role_leaves_panel(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._visualizer_enabled = True
    app._ui = _FakeUI()

    app._handle_server_hello(_payload(active_roles=[Roles.VISUALIZER.value, "player@v1"]))

    assert app._ui.visualizer_enabled_calls == []


def test_server_hello_ignored_when_visualizer_disabled(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._visualizer_enabled = False
    app._ui = _FakeUI()

    app._handle_server_hello(_payload(active_roles=["player@v1"]))

    assert app._ui.visualizer_enabled_calls == []
