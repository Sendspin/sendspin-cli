from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiosendspin.models.types import MediaCommand, PlaybackStateType, PlayerCommand

from sendspin.daemon.daemon import DaemonArgs, SendspinDaemon
from sendspin.settings import ClientSettings


class _FakeAudioHandler:
    def __init__(self, *, volume: int, muted: bool) -> None:
        self.volume = volume
        self.muted = muted
        self.calls: list[tuple[int, bool]] = []
        self.delay_changes: list[int] = []
        self.audio_released = False
        self.stream_active = False

    def set_volume(self, volume: int, *, muted: bool) -> None:
        self.calls.append((volume, muted))
        self.volume = volume
        self.muted = muted

    def notify_delay_change(self, delta_us: int) -> None:
        self.delay_changes.append(delta_us)

    def release_audio(self) -> None:
        self.audio_released = True
        self.stream_active = False

    def acquire_audio(self) -> None:
        self.audio_released = False


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
    daemon._update_control_metadata(
        SimpleNamespace(
            metadata=SimpleNamespace(
                title="Track",
                artist=UndefinedField(),
                album=None,
                artwork_url=UndefinedField(),
                progress=SimpleNamespace(
                    track_progress=12_500,
                    track_duration=UndefinedField(),
                    playback_speed=1000,
                ),
            )
        )
    )

    state = daemon._get_control_state()

    assert state["track"] == {"title": "Track"}
    assert state["volume"] == {"volume": 25, "muted": False}
    assert state["delay_ms"] == 0.0
    assert state["playback"]["speed"] == 1000
    assert state["playback"]["position"] == pytest.approx(12.5, abs=1e-3)


def test_control_set_delay_applies_static_delay_and_notifies_handler(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)
    daemon._static_delay_ms = 100.0

    client = SimpleNamespace(connected=True, static_delay_ms=100.0)

    def set_static_delay_ms(value: float) -> None:
        client.static_delay_ms = max(0.0, min(5000.0, value))

    client.set_static_delay_ms = set_static_delay_ms
    daemon._client = client  # type: ignore[assignment]

    async def run() -> None:
        await daemon._apply_control_command("set_delay", {"command": "set_delay", "delay_ms": 150})

    asyncio.run(run())

    assert daemon._static_delay_ms == 150.0
    assert daemon._audio_handler.delay_changes == [50_000]
    assert daemon._settings.static_delay_ms == 150.0


def test_control_release_and_acquire_audio_update_audio_status(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)
    daemon._audio_handler.stream_active = True

    async def run() -> dict[str, object] | None:
        await daemon._apply_control_command("release_audio", {"command": "release_audio"})
        status = await daemon._apply_control_command("audio_status", {"command": "audio_status"})
        await daemon._apply_control_command("acquire_audio", {"command": "acquire_audio"})
        return status

    status = asyncio.run(run())

    assert status == {"audio": {"released": True, "stream_active": False}}
    assert daemon._get_control_state()["audio"] == {"released": False, "stream_active": False}


def test_control_state_merges_partial_metadata_updates(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)
    daemon._update_control_metadata(
        SimpleNamespace(
            metadata=SimpleNamespace(
                title="Track One",
                artist="Artist One",
                album="Album One",
                artwork_url="https://example.invalid/art.jpg",
                progress=SimpleNamespace(
                    track_progress=10_000,
                    track_duration=180_000,
                    playback_speed=1000,
                ),
            )
        )
    )
    daemon._update_control_metadata(
        SimpleNamespace(
            metadata=SimpleNamespace(
                title="Track Two",
                artist=UndefinedField(),
                album=UndefinedField(),
                artwork_url=UndefinedField(),
                progress=SimpleNamespace(
                    track_progress=0,
                    track_duration=UndefinedField(),
                    playback_speed=UndefinedField(),
                ),
            )
        )
    )

    state = daemon._get_control_state()

    assert state["track"] == {
        "title": "Track Two",
        "artist": "Artist One",
        "album": "Album One",
        "artwork_url": "https://example.invalid/art.jpg",
    }
    assert state["volume"] == {"volume": 25, "muted": False}
    assert state["delay_ms"] == 0.0
    assert state["playback"]["speed"] == 1000
    assert state["playback"]["position"] == pytest.approx(0.0, abs=1e-3)


def test_control_state_clears_metadata_when_server_sends_none(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)
    daemon._update_control_metadata(
        SimpleNamespace(
            metadata=SimpleNamespace(
                title="Track",
                artist="Artist",
                album="Album",
                artwork_url=None,
                progress=SimpleNamespace(track_progress=10_000, track_duration=180_000, playback_speed=1000),
            )
        )
    )
    daemon._update_control_metadata(SimpleNamespace(metadata=None))

    state = daemon._get_control_state()

    assert state["track"] == {}
    assert state["volume"] == {"volume": 25, "muted": False}
    assert state["delay_ms"] == 0.0
    assert state["playback"] == {}


def test_control_state_clears_progress_when_server_sends_none(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)
    daemon._update_control_metadata(
        SimpleNamespace(
            metadata=SimpleNamespace(
                title="Track",
                artist="Artist",
                album="Album",
                artwork_url=None,
                progress=SimpleNamespace(track_progress=10_000, track_duration=180_000, playback_speed=1000),
            )
        )
    )
    daemon._update_control_metadata(
        SimpleNamespace(
            metadata=SimpleNamespace(
                title=UndefinedField(),
                artist=UndefinedField(),
                album=UndefinedField(),
                artwork_url=UndefinedField(),
                progress=None,
            )
        )
    )

    state = daemon._get_control_state()

    assert state["track"] == {"title": "Track", "artist": "Artist", "album": "Album"}
    assert state["volume"] == {"volume": 25, "muted": False}
    assert state["delay_ms"] == 0.0
    assert state["playback"]["duration"] == pytest.approx(180.0, abs=1e-3)
    assert state["playback"]["speed"] == 1000
    assert state["playback"]["position"] == pytest.approx(10.0, abs=1e-3)


def test_handle_metadata_updates_control_state(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path, settings_volume=25, settings_muted=False)
    daemon._audio_handler = _FakeAudioHandler(volume=25, muted=False)
    daemon._handle_metadata(
        SimpleNamespace(
        metadata=SimpleNamespace(
            title="Track",
            artist="Artist",
            album=None,
            artwork_url=UndefinedField(),
            progress=SimpleNamespace(
                track_progress=12_500,
                track_duration=UndefinedField(),
                playback_speed=1000,
            ),
        ),
        playback_state=PlaybackStateType.PLAYING,
    )
    )

    state = daemon._get_control_state()

    assert state["track"] == {"title": "Track", "artist": "Artist"}
    assert state["volume"] == {"volume": 25, "muted": False}
    assert state["delay_ms"] == 0.0
    assert state["playback"]["speed"] == 1000
    assert state["playback"]["position"] == pytest.approx(12.5, abs=1e-3)
