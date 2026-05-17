from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiosendspin.models.types import MediaCommand, PlaybackStateType, PlayerCommand

from sendspin.daemon.daemon import DaemonArgs, SendspinDaemon
from sendspin.settings import ClientSettings


class _FakeAudioHandler:
    def __init__(self, *, volume: int, muted: bool) -> None:
        self.volume = volume
        self.muted = muted
        self.calls: list[tuple[int, bool]] = []
        self.delay_changes: list[int] = []

    def set_volume(self, volume: int, *, muted: bool) -> None:
        self.calls.append((volume, muted))
        self.volume = volume
        self.muted = muted

    def notify_delay_change(self, delta_us: int) -> None:
        self.delay_changes.append(delta_us)


class _FakeClient:
    connected = True

    def __init__(self) -> None:
        self.commands: list[MediaCommand] = []

    async def send_group_command(self, command: MediaCommand) -> None:
        self.commands.append(command)


class UndefinedField:
    pass


def _make_daemon(tmp_path: Path, *, settings_volume: int, settings_muted: bool) -> SendspinDaemon:
    settings = ClientSettings(
        _settings_file=tmp_path / "settings.json",
        player_volume=settings_volume,
        player_muted=settings_muted,
    )
    args = DaemonArgs(
        audio_device=SimpleNamespace(index=0, name="Fake Device"),
        client_id="test-client",
        client_name="Test Client",
        settings=settings,
        use_mpris=False,
    )
    return SendspinDaemon(args)


def test_volume_command_uses_audio_handler_muted_state_for_external_volume(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=True)
    daemon._audio_handler = _FakeAudioHandler(volume=41, muted=False)

    payload = SimpleNamespace(
        player=SimpleNamespace(command=PlayerCommand.VOLUME, volume=67, mute=None)
    )

    daemon._handle_server_command(payload)

    assert daemon._audio_handler.calls == [(67, False)]


def test_mute_command_uses_audio_handler_volume_state_for_external_volume(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=12, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=53, muted=False)

    payload = SimpleNamespace(
        player=SimpleNamespace(command=PlayerCommand.MUTE, volume=None, mute=True)
    )

    daemon._handle_server_command(payload)

    assert daemon._audio_handler.calls == [(53, True)]


def test_set_static_delay_uses_applied_tracker_for_delta(tmp_path: Path) -> None:
    """Sync delta is computed from `_static_delay_ms`, not stale settings.

    Reproduces the CLI-override case: settings stays at 0 while the client was
    initialized to 500 from `--static-delay-ms`. A server-initiated delay change
    to 200 must produce delta = -300ms (200 - 500), not -200ms (200 - 0).
    """
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)
    daemon._static_delay_ms = 500.0
    # aiosendspin auto-applies before the callback fires, so the client already
    # reports the new value.
    daemon._client = SimpleNamespace(static_delay_ms=200.0)  # type: ignore[assignment]

    payload = SimpleNamespace(
        player=SimpleNamespace(
            command=PlayerCommand.SET_STATIC_DELAY,
            volume=None,
            mute=None,
            static_delay_ms=200,
        )
    )

    # `settings.update` schedules a debounced save via asyncio; wrap in a loop.
    async def run() -> None:
        daemon._handle_server_command(payload)

    asyncio.run(run())

    assert daemon._audio_handler.delay_changes == [-300_000]
    assert daemon._static_delay_ms == 200.0
    assert daemon._settings.static_delay_ms == 200.0


def test_control_set_volume_updates_audio_handler(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)

    async def run() -> None:
        await daemon._apply_control_command("set_volume", {"command": "set_volume", "volume": 67, "muted": True})

    asyncio.run(run())

    assert daemon._audio_handler.calls == [(67, True)]


def test_control_next_sends_group_command(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    client = _FakeClient()
    daemon._client = client  # type: ignore[assignment]

    async def run() -> None:
        await daemon._apply_control_command("next", {"command": "next"})

    asyncio.run(run())

    assert client.commands == [MediaCommand.NEXT]


def test_control_toggle_uses_playback_state(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    client = _FakeClient()
    daemon._client = client  # type: ignore[assignment]
    daemon._playback_state = PlaybackStateType.PLAYING

    async def run() -> None:
        await daemon._apply_control_command("toggle_play_pause", {"command": "toggle_play_pause"})

    asyncio.run(run())

    assert client.commands == [MediaCommand.PAUSE]


def test_control_state_skips_undefined_metadata_fields(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)
    daemon._last_state_payload = SimpleNamespace(
        metadata=SimpleNamespace(
            title="Track",
            artist=UndefinedField(),
            album=None,
            artwork_url=UndefinedField(),
            progress=SimpleNamespace(
                track_progress=12_500,
                track_duration=UndefinedField(),
                playback_speed=1,
            ),
        )
    )

    state = daemon._get_control_state()

    assert state == {
        "track": {"title": "Track"},
        "playback": {"position": 12.5, "speed": 1},
        "volume": {"volume": 25, "muted": False},
    }
