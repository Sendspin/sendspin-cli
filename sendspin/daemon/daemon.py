"""Daemon mode for running a Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import asdict, dataclass
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, web
from aiosendspin.client import ClientListener, SendspinClient
from aiosendspin.models.core import GroupUpdateServerPayload, ServerCommandPayload, ServerStatePayload
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin_mpris import MPRIS_AVAILABLE, SendspinMpris
from aiosendspin.models.types import (
    ConnectionReason,
    GoodbyeReason,
    MediaCommand,
    PlaybackStateType,
    PlayerCommand,
    Roles,
)

from sendspin.audio_devices import AudioDevice, detect_supported_audio_formats
from sendspin.audio_connector import AudioStreamHandler
from sendspin.hooks import run_hook
from sendspin.settings import ClientSettings
from sendspin.utils import create_task, get_device_info

if TYPE_CHECKING:
    from sendspin.volume_controller import VolumeController

logger = logging.getLogger(__name__)


@dataclass
class DaemonArgs:
    """Configuration for the Sendspin daemon."""

    audio_device: AudioDevice
    client_id: str
    client_name: str
    settings: ClientSettings
    url: str | None = None
    static_delay_ms: float | None = None
    listen_port: int = 8928
    use_mpris: bool = True
    preferred_format: SupportedAudioFormat | None = None
    volume_controller: VolumeController | None = None
    hook_start: str | None = None
    hook_stop: str | None = None
    manufacturer: str | None = None
    product_name: str | None = None
    interface: str | None = None
    log_metadata: bool = False
    control_api: bool = False
    control_host: str = "127.0.0.1"
    control_port: int = 59999


class SendspinDaemon:
    """Sendspin daemon - headless audio player mode.

    When a URL is provided, the daemon connects to that server (client-initiated).
    When no URL is provided, the daemon listens for incoming server connections
    and advertises itself via mDNS (server-initiated connections).
    """

    def __init__(self, args: DaemonArgs) -> None:
        """Initialize the daemon."""
        self._args = args
        self._client: SendspinClient | None = None
        self._listener: ClientListener | None = None
        self._audio_handler: AudioStreamHandler | None = None
        self._settings = args.settings
        self._mpris: SendspinMpris | None = None
        # Currently-applied static delay in milliseconds, mirroring
        # `SendspinClient.static_delay_ms`. Tracked separately from settings
        # because CLI overrides aren't persisted to settings, so
        # `settings.static_delay_ms` can lag the value actually given to the client.
        self._static_delay_ms: float = 0.0
        self._connection_lock: asyncio.Lock | None = None
        self._server_url: str | None = None
        self._group_update_unsubscribe: Callable[[], None] | None = None
        self._server_command_unsubscribe: Callable[[], None] | None = None
        self._control_runner: web.AppRunner | None = None
        self._last_state_payload: ServerStatePayload | None = None
        self._control_track: dict[str, Any] = {}
        self._control_playback: dict[str, Any] = {}
        self._playback_state: PlaybackStateType | None = None
        self._control_metadata_updated_at: float | None = None

    def _create_client(self) -> SendspinClient:
        """Create a new SendspinClient instance."""
        assert self._audio_handler is not None
        client_roles = [Roles.PLAYER]
        if MPRIS_AVAILABLE and self._args.use_mpris:
            client_roles.extend([Roles.METADATA, Roles.CONTROLLER])
        if (self._args.log_metadata or self._args.control_api) and not self._args.use_mpris:
            client_roles.extend([Roles.METADATA])


        supported_formats = detect_supported_audio_formats(self._args.audio_device)
        if self._args.preferred_format is not None:
            supported_formats = [f for f in supported_formats if f != self._args.preferred_format]
            supported_formats.insert(0, self._args.preferred_format)

        return SendspinClient(
            client_id=self._args.client_id,
            client_name=self._args.client_name,
            roles=client_roles,
            device_info=get_device_info(
                manufacturer=self._args.manufacturer,
                product_name=self._args.product_name,
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=supported_formats,
                buffer_capacity=32_000_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
            static_delay_ms=self._static_delay_ms,
            state_supported_commands=[PlayerCommand.SET_STATIC_DELAY],
            initial_volume=self._audio_handler.volume,
            initial_muted=self._audio_handler.muted,
        )

    async def run(self) -> int:
        """Run the daemon."""
        logger.info("Starting Sendspin daemon: %s", self._args.client_id)
        loop = asyncio.get_running_loop()

        # Store reference to current task so it can be cancelled on shutdown
        main_task = asyncio.current_task()
        assert main_task is not None

        def signal_handler() -> None:
            logger.debug("Received interrupt signal, shutting down...")
            main_task.cancel()

        # Register signal handlers
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

        # CLI arg overrides settings for static delay
        delay = (
            self._args.static_delay_ms
            if self._args.static_delay_ms is not None
            else self._settings.static_delay_ms
        )
        self._static_delay_ms = max(0.0, min(5000.0, delay))

        self._audio_handler = AudioStreamHandler(
            audio_device=self._args.audio_device,
            volume=self._settings.player_volume,
            muted=self._settings.player_muted,
            on_event=self._on_stream_event,
            on_format_change=self._handle_format_change,
            on_volume_change=self._on_volume_change,
            volume_controller=self._args.volume_controller,
        )
        await self._audio_handler.read_initial_volume()
        await self._audio_handler.start_volume_monitor()
        if self._args.control_api:
            await self._start_control_api()

        try:
            if self._args.url is not None:
                # Client-initiated connection mode
                await self._run_client_initiated()
            else:
                # Server-initiated connection mode (listen for incoming connections)
                await self._run_server_initiated()
        except asyncio.CancelledError:
            logger.debug("Daemon cancelled")
        finally:
            if self._mpris is not None:
                self._mpris.stop()
                self._mpris = None
            if self._audio_handler is not None:
                await self._audio_handler.shutdown()
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            if self._listener is not None:
                await self._listener.stop()
                self._listener = None
            await self._stop_control_api()
            if self._settings:
                await self._settings.flush()
            logger.info("Daemon stopped")

        return 0

    async def _start_control_api(self) -> None:
        """Start the local HTTP control API."""
        app = web.Application()
        app.add_routes(
            [
                web.post("/control", self._handle_control_request),
                web.get("/state", self._handle_state_request),
            ]
        )
        self._control_runner = web.AppRunner(app, access_log=None)
        await self._control_runner.setup()
        site = web.TCPSite(self._control_runner, self._args.control_host, self._args.control_port)
        await site.start()
        logger.info(
            "Sendspin control API listening on http://%s:%d",
            self._args.control_host,
            self._args.control_port,
        )

    async def _stop_control_api(self) -> None:
        """Stop the local HTTP control API."""
        if self._control_runner is None:
            return
        await self._control_runner.cleanup()
        self._control_runner = None

    async def _handle_control_request(self, request: web.Request) -> web.Response:
        """Handle Kodi control API commands."""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "Payload must be a JSON object"}, status=400)

        command = payload.get("command")
        if not isinstance(command, str):
            return web.json_response({"error": "Missing command"}, status=400)

        try:
            await self._apply_control_command(command, payload)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=500)

        return web.json_response({"ok": True})

    async def _apply_control_command(self, command: str, payload: dict[str, Any]) -> None:
        """Apply a validated control command."""
        match command:
            case "play":
                await self._send_media_command(MediaCommand.PLAY)
            case "pause":
                await self._send_media_command(MediaCommand.PAUSE)
            case "toggle_play_pause":
                media_command = (
                    MediaCommand.PAUSE
                    if self._playback_state == PlaybackStateType.PLAYING
                    else MediaCommand.PLAY
                )
                await self._send_media_command(media_command)
            case "next":
                await self._send_media_command(MediaCommand.NEXT)
            case "previous":
                await self._send_media_command(MediaCommand.PREVIOUS)
            case "set_volume":
                self._set_control_volume(payload)
            case "set_delay":
                self._set_control_delay(payload)
            case "seek":
                raise ValueError("seek is not supported by this Sendspin daemon yet")
            case _:
                raise ValueError(f"Unknown command: {command}")

    def _set_control_delay(self, payload: dict[str, Any]) -> None:
        """Apply a local static delay command from the control API."""
        if self._audio_handler is None:
            raise RuntimeError("Audio handler is not initialized")
        if self._client is None or not getattr(self._client, "connected", False):
            raise RuntimeError("No connected Sendspin server")

        raw_delay = payload.get("delay_ms")
        if not isinstance(raw_delay, (int, float)):
            raise ValueError("delay_ms must be a number")

        new_delay = float(raw_delay)
        if not 0 <= new_delay <= 5000:
            raise ValueError("delay_ms must be between 0 and 5000")

        old_delay = self._client.static_delay_ms
        self._client.set_static_delay_ms(new_delay)
        actual_delta_us = int((self._client.static_delay_ms - old_delay) * 1000)
        if actual_delta_us != 0:
            self._audio_handler.notify_delay_change(actual_delta_us)

        self._static_delay_ms = self._client.static_delay_ms
        self._settings.update(static_delay_ms=self._static_delay_ms)

    async def _send_media_command(self, command: MediaCommand) -> None:
        """Send a group media command to the connected Sendspin server."""
        if self._client is None or not self._client.connected:
            raise RuntimeError("No connected Sendspin server")
        await self._client.send_group_command(command)

    def _set_control_volume(self, payload: dict[str, Any]) -> None:
        """Apply a local player volume command from the control API."""
        if self._audio_handler is None:
            raise RuntimeError("Audio handler is not initialized")

        raw_volume = payload.get("volume")
        if not isinstance(raw_volume, int):
            raise ValueError("volume must be an integer")
        if not 0 <= raw_volume <= 100:
            raise ValueError("volume must be between 0 and 100")

        muted = bool(payload.get("muted", False))
        self._audio_handler.set_volume(raw_volume, muted=muted)

    async def _handle_state_request(self, _request: web.Request) -> web.Response:
        """Return the daemon state expected by the Kodi add-on."""
        return web.json_response(self._get_control_state())

    def _get_defined_attr(self, obj: object, name: str) -> Any:
        """Return an attribute unless aiosendspin marks it as undefined."""
        value = getattr(obj, name, None)
        if self._is_undefined_field(value):
            return None
        return value

    @staticmethod
    def _is_undefined_field(value: object) -> bool:
        return value.__class__.__name__ == "UndefinedField"

    def _json_safe_value(self, value: Any) -> Any:
        """Convert protocol model values into JSON-safe primitives."""
        if value is None or self._is_undefined_field(value):
            return None
        if isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, list | tuple):
            return [item for item in (self._json_safe_value(item) for item in value) if item is not None]
        if isinstance(value, dict):
            return {
                key: item
                for key, item in ((key, self._json_safe_value(item)) for key, item in value.items())
                if item is not None
            }
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, str | int | float | bool):
            return enum_value
        return str(value)

    def _get_control_state(self) -> dict[str, Any]:
        """Build the current JSON-serializable control state."""
        playback = dict(self._control_playback)
        if "speed" not in playback and self._playback_state is not None:
            playback["speed"] = 0 if self._playback_state == PlaybackStateType.PAUSED else 1000
        
        if "position" in playback and self._control_metadata_updated_at is not None:
            current_speed = playback.get("speed", 0)
            
            # Defensive check: if daemon state is explicitly paused, override speed to 0
            if self._playback_state == PlaybackStateType.PAUSED:
                current_speed = 0

            if current_speed > 0:
                elapsed_seconds = time.monotonic() - self._control_metadata_updated_at
                realtime_position = playback["position"] + (elapsed_seconds * (current_speed / 1000.0))
                
                logger.debug(
                    "[ControlAPI] Extrapolating state -> Baseline Pos: %.3fs, Elapsed: %.3fs, Speed: %s, Calculated Real-time: %.3fs",
                    playback["position"], elapsed_seconds, current_speed, realtime_position
                )

                # Cap progress at total duration if duration is available
                if "duration" in playback:
                    if realtime_position > playback["duration"]:
                        logger.debug(
                            "[ControlAPI] Extrapolated position (%.3fs) exceeded track duration (%.3fs). Capping.",
                            realtime_position, playback["duration"]
                        )
                    realtime_position = min(realtime_position, playback["duration"])
                    
                playback["position"] = realtime_position
            else:
                logger.debug("[ControlAPI] Extrapolation paused (speed=0). Static Baseline: %.3fs", playback["position"])
        else:
            logger.debug(
                "[ControlAPI] Skipping extrapolation -> Has baseline position: %s, Has baseline timestamp: %s",
                "position" in playback, self._control_metadata_updated_at is not None
            )

        volume: dict[str, Any] = {}
        if self._audio_handler is not None:
            volume = {
                "volume": self._audio_handler.volume,
                "muted": self._audio_handler.muted,
            }

        delay_ms = (
            self._client.static_delay_ms
            if self._client is not None and getattr(self._client, "connected", False)
            else self._static_delay_ms
        )

        return {
            "track": dict(self._control_track),
            "playback": playback,
            "volume": volume,
            "delay_ms": delay_ms,
        }

    def _update_control_metadata(self, payload: ServerStatePayload) -> None:
        """Merge partial metadata updates into the state exposed to the control API."""
        metadata = payload.metadata
        if metadata is None:
            logger.debug("[ControlAPI] Received null metadata frame. Wiping control state.")
            self._control_track.clear()
            self._control_playback.clear()
            self._control_metadata_updated_at = None
            return
        if self._is_undefined_field(metadata):
            return

        # 1. Track track/song identity changes to force progress resets
        track_changed = False
        for key in ("title", "artist", "album", "artwork_url"):
            value = self._json_safe_value(self._get_defined_attr(metadata, key))
            if value is not None:
                if key in ("title", "artist") and self._control_track.get(key) != value:
                    logger.debug(
                        "[ControlAPI] Track change detected via %s: %r -> %r", 
                        key, self._control_track.get(key), value
                    )
                    track_changed = True
                self._control_track[key] = value

        if track_changed:
            logger.debug("[ControlAPI] Resetting metadata baseline anchors for new track.")
            self._control_playback["position"] = 0.0
            self._control_metadata_updated_at = time.monotonic()
            self._control_playback.pop("duration", None)

        # 2. Process progress updates safely without scrubbing data on partial updates
        progress = self._get_defined_attr(metadata, "progress")
        if progress is None:
            logger.debug("[ControlAPI] Partial packet: No progress block included. Preserving existing baseline.")
            return

        track_progress = self._get_defined_attr(progress, "track_progress")
        track_duration = self._get_defined_attr(progress, "track_duration")
        playback_speed = self._get_defined_attr(progress, "playback_speed")

        logger.debug(
            "[ControlAPI] Incoming Progress Block -> raw_progress: %s, raw_duration: %s, raw_speed: %s",
            track_progress, track_duration, playback_speed
        )

        if isinstance(track_progress, int | float):
            self._control_playback["position"] = track_progress / 1000.0
            self._control_metadata_updated_at = time.monotonic()
            logger.debug(
                "[ControlAPI] Anchor Updated -> Position: %.3fs, Local System Anchor: %.3f", 
                self._control_playback["position"], self._control_metadata_updated_at
            )
        elif track_changed:
            # Re-ensure track changes have a fallback starting point if raw_progress is missing
            self._control_playback["position"] = 0.0
            self._control_metadata_updated_at = time.monotonic()

        if isinstance(track_duration, int | float):
            self._control_playback["duration"] = track_duration / 1000.0
            logger.debug("[ControlAPI] Anchor Updated -> Duration: %.3fs", self._control_playback["duration"])
            
        if isinstance(playback_speed, int | float):
            # Keep protocol speed in 1000-scale for consistent state reporting.
            self._control_playback["speed"] = playback_speed
            logger.debug("[ControlAPI] Anchor Updated -> Speed: %s", self._control_playback["speed"])

    def _on_volume_change(self, volume: int, muted: bool) -> None:
        """Handle volume changes from any source (server command, external, etc.)."""
        assert self._settings is not None
        assert self._audio_handler is not None

        if not self._audio_handler.uses_external_volume_controller:
            self._settings.update(player_volume=volume, player_muted=muted)

    async def _run_client_initiated(self) -> None:
        """Run in client-initiated mode, connecting to a specific URL."""
        assert self._args.url is not None
        assert self._audio_handler is not None
        client = self._create_client()
        if self._args.log_metadata or self._args.control_api:
            self._setup_metadata_listeners()
        self._server_url = self._args.url
        self._attach_client(client)
        await self._connection_loop(self._args.url)

    async def _run_server_initiated(self) -> None:
        """Run in server-initiated mode, listening for incoming connections."""
        logger.info(
            "Listening for server connections on port %d (mDNS: _sendspin._tcp.local.)",
            self._args.listen_port,
        )

        self._connection_lock = asyncio.Lock()

        self._listener = ClientListener(
            client_id=self._args.client_id,
            on_connection=self._handle_server_connection,
            port=self._args.listen_port,
            client_name=self._args.client_name,
            host=self._args.interface if self._args.interface is not None else "0.0.0.0",
        )
        await self._listener.start()

        # Keep running until cancelled
        while True:
            await asyncio.sleep(3600)

    def _attach_client(self, client: SendspinClient) -> None:
        """Attach listeners, audio handler, and MPRIS to a client."""
        assert self._audio_handler is not None
        self._client = client
        self._audio_handler.attach_client(client)
        self._server_command_unsubscribe = client.add_server_command_listener(
            self._handle_server_command
        )
        self._group_update_unsubscribe = client.add_group_update_listener(self._on_group_update)
        if MPRIS_AVAILABLE and self._args.use_mpris:
            self._mpris = SendspinMpris(client)
            self._mpris.start()

    def _detach_client(self) -> None:
        """Detach listeners, audio handler, and MPRIS from the current client."""
        if self._server_command_unsubscribe is not None:
            self._server_command_unsubscribe()
            self._server_command_unsubscribe = None
        if self._group_update_unsubscribe is not None:
            self._group_update_unsubscribe()
            self._group_update_unsubscribe = None
        if self._mpris is not None:
            self._mpris.stop()
            self._mpris = None
        if self._audio_handler is not None:
            self._audio_handler.detach_client()

    async def _handle_disconnect(self) -> None:
        """Reset audio state after a connection drop."""
        if self._audio_handler is not None:
            await self._audio_handler.handle_disconnect()

    def _should_switch_to_new_server(
        self, old_client: SendspinClient, new_client: SendspinClient
    ) -> bool:
        """Decide whether to switch to a new server per the multi-server spec.

        Assumes both clients have completed their handshake.
        """
        assert new_client.server_info is not None

        # Old client may have disconnected before we acquired the lock.
        if old_client.server_info is None:
            return True

        if new_client.server_info.server_id == old_client.server_info.server_id:
            return True

        new_reason = new_client.server_info.connection_reason
        old_reason = old_client.server_info.connection_reason

        if new_reason == ConnectionReason.PLAYBACK:
            return True
        if old_reason == ConnectionReason.PLAYBACK:
            return False

        # Both 'discovery' — prefer last played server.
        if self._settings.last_played_server_id == new_client.server_info.server_id:
            return True

        return False

    def _on_group_update(self, payload: GroupUpdateServerPayload) -> None:
        """Track last played server for multi-server arbitration."""
        if payload.playback_state != PlaybackStateType.PLAYING:
            return
        if self._client is None or self._client.server_info is None:
            return
        server_id = self._client.server_info.server_id
        if self._settings.last_played_server_id != server_id:
            self._settings.update(last_played_server_id=server_id)

    async def _handle_server_connection(self, ws: web.WebSocketResponse) -> None:
        """Handle an incoming server connection."""
        logger.info("Server connected")
        assert self._audio_handler is not None
        assert self._connection_lock is not None
        assert self._settings is not None

        # Lock ensures we wait for any in-progress handshake to complete
        # before disconnecting the previous server
        async with self._connection_lock:
            old_client = self._client

            # Per spec: always complete the handshake before deciding which
            # server to keep.
            client = self._create_client()

            try:
                await client.attach_websocket(ws)
            except TimeoutError:
                logger.warning("Handshake with server timed out")
                return
            except Exception:
                logger.exception("Error during server handshake")
                return

            # Decide which server to keep.
            if old_client is not None:
                if self._should_switch_to_new_server(old_client, client):
                    assert client.server_info is not None
                    logger.info(
                        "Switching to server '%s' (%s)",
                        client.server_info.name,
                        client.server_info.connection_reason.value,
                    )
                    self._detach_client()
                    await self._handle_disconnect()
                    await old_client.send_goodbye(GoodbyeReason.ANOTHER_SERVER)
                    await old_client.disconnect()
                else:
                    assert old_client.server_info is not None
                    assert client.server_info is not None
                    logger.info(
                        "Keeping server '%s', rejecting '%s' (%s)",
                        old_client.server_info.name,
                        client.server_info.name,
                        client.server_info.connection_reason.value,
                    )
                    await client.send_goodbye(GoodbyeReason.ANOTHER_SERVER)
                    await client.disconnect()
                    return

            self._attach_client(client)
            if self._args.log_metadata or self._args.control_api:
                self._setup_metadata_listeners()

        # Handshake complete, release lock so new connections can proceed
        # Now wait for disconnect (outside the lock)
        try:
            disconnect_event = asyncio.Event()
            unsubscribe = client.add_disconnect_listener(disconnect_event.set)
            await disconnect_event.wait()
            unsubscribe()
            logger.info("Server disconnected")
        except Exception:
            logger.exception("Error waiting for server disconnect")
        finally:
            # Only cleanup if we're still the active client (not replaced by new connection)
            if self._client is client:
                self._detach_client()
                await self._handle_disconnect()

    async def _connection_loop(self, url: str) -> None:
        """Run the connection loop with automatic reconnection (client-initiated mode)."""
        assert self._client is not None
        assert self._audio_handler is not None
        assert self._settings is not None
        error_backoff = 1.0
        max_backoff = 300.0

        while True:
            try:
                await self._client.connect(url)
                error_backoff = 1.0

                # Wait for disconnect
                disconnect_event: asyncio.Event = asyncio.Event()
                unsubscribe = self._client.add_disconnect_listener(disconnect_event.set)
                await disconnect_event.wait()
                unsubscribe()

                # Connection dropped
                logger.info("Disconnected from server")
                await self._handle_disconnect()

                logger.info("Reconnecting to %s", url)

            except (TimeoutError, OSError, ClientError) as e:
                logger.warning(
                    "Connection error (%s), retrying in %.0fs",
                    type(e).__name__,
                    error_backoff,
                )

                await asyncio.sleep(error_backoff)
                error_backoff = min(error_backoff * 2, max_backoff)

            except Exception:
                logger.exception("Unexpected error during connection")
                break

    def _handle_server_command(self, payload: ServerCommandPayload) -> None:
        """Handle server commands for player volume/mute control."""
        if payload.player is None or self._settings is None:
            return

        assert self._audio_handler is not None
        player_cmd = payload.player

        if player_cmd.command == PlayerCommand.VOLUME and player_cmd.volume is not None:
            self._audio_handler.set_volume(player_cmd.volume, muted=self._audio_handler.muted)
            logger.info("Server set player volume: %d%%", player_cmd.volume)
        elif player_cmd.command == PlayerCommand.MUTE and player_cmd.mute is not None:
            self._audio_handler.set_volume(self._audio_handler.volume, muted=player_cmd.mute)
            logger.info("Server %s player", "muted" if player_cmd.mute else "unmuted")
        elif (
            player_cmd.command == PlayerCommand.SET_STATIC_DELAY
            and player_cmd.static_delay_ms is not None
        ):
            # Client library already applied the delay change;
            # notify audio worker so sync correction adjusts timing gradually
            assert self._client is not None
            new_delay_ms = self._client.static_delay_ms
            delta_us = int((new_delay_ms - self._static_delay_ms) * 1000)
            if delta_us != 0:
                self._audio_handler.notify_delay_change(delta_us)
            self._static_delay_ms = new_delay_ms
            self._settings.update(static_delay_ms=new_delay_ms)
            logger.info("Server set delay: %dms", player_cmd.static_delay_ms)

    def _handle_format_change(
        self, codec: str | None, sample_rate: int, bit_depth: int, channels: int
    ) -> None:
        """Log audio format changes."""
        logger.info(
            "Audio format: %s %dHz/%d-bit/%dch",
            codec or "PCM",
            sample_rate,
            bit_depth,
            channels,
        )

    def _on_stream_event(self, event: str) -> None:
        """Handle stream lifecycle events by running hooks."""
        
        # Implicitly sync control API state with the physical audio hardware state
        if event == "start":
            logger.debug("[ControlAPI] Hardware audio started. Forcing PLAYING state.")
            self._playback_state = PlaybackStateType.PLAYING
            self._control_playback["speed"] = 1.0
            if "position" in self._control_playback:
                # Re-anchor the system timer so extrapolation resumes seamlessly from current position
                self._control_metadata_updated_at = time.monotonic()
                
        elif event == "stop":
            logger.debug("[ControlAPI] Hardware audio stopped. Forcing PAUSED state.")
            self._playback_state = PlaybackStateType.PAUSED
            
            # If we were previously playing, snapshot the exact position where it stopped
            current_speed = self._control_playback.get("speed", 1.0)
            if current_speed > 0 and "position" in self._control_playback and self._control_metadata_updated_at is not None:
                elapsed = time.monotonic() - self._control_metadata_updated_at
                self._control_playback["position"] += elapsed * current_speed
                
                if "duration" in self._control_playback:
                    self._control_playback["position"] = min(
                        self._control_playback["position"], 
                        self._control_playback["duration"]
                    )
            
            self._control_playback["speed"] = 0.0
            self._control_metadata_updated_at = time.monotonic()

        hook = self._args.hook_start if event == "start" else self._args.hook_stop
        if not hook:
            return
        
        server_info = self._client.server_info if self._client else None
        create_task(
            run_hook(
                hook,
                event=event,
                server_id=server_info.server_id if server_info else None,
                server_name=server_info.name if server_info else None,
                server_url=self._server_url,
                client_id=self._args.client_id,
                client_name=self._args.client_name,
            )
        )

    def _setup_metadata_listeners(self) -> None:
        """Register event handlers with the Sendspin client."""
        if self._client is None:
            logger.warning("Cannot register metadata listener: client not initialized")
            return

        self._client.add_metadata_listener(self._handle_metadata)

        logger.info("Successfully registered metadata listener")

    def _handle_metadata(self, payload: ServerStatePayload) -> None:
        """Logs the raw content of any server payload to stdout, stripping empty/undefined fields."""
        self._last_state_payload = payload
        self._update_control_metadata(payload)
        playback_state = getattr(payload, "playback_state", None)
        if playback_state is not None:
            self._playback_state = playback_state

        if not getattr(self._args, "log_metadata", False):
            return

        create_task(self._log_metadata_delayed(payload))

    async def _log_metadata_delayed(self, payload: ServerStatePayload) -> None:
        """Calculates the delay and waits for the specific audio sync point."""
        try:
            raw_data = asdict(payload)

            meta = raw_data.get("metadata", {})
            server_ts = meta.get("timestamp") or raw_data.get("timestamp")

            if server_ts and self._client and self._client.is_time_synchronized():
                target_time_us = self._client.compute_play_time(server_ts)

                # Convert microseconds to seconds for asyncio.sleep and time.monotonic
                target_time_s = target_time_us / 1_000_000.0

                # 2. Wait until the local clock hits the target moment
                wait_time = target_time_s - time.monotonic()
                if 0 < wait_time < 10:  # Sane bounds check
                    await asyncio.sleep(wait_time)

            # --- Data Cleaning Logic ---
            def clean_empty(value: Any) -> Any:
                if isinstance(value, dict):
                    cleaned = {k: clean_empty(v) for k, v in value.items()}
                    return {k: v for k, v in cleaned.items() if v not in (None, {}, [])}
                if hasattr(value, "__class__") and value.__class__.__name__ == "UndefinedField":
                    return None
                return value

            event_type = payload.__class__.__name__
            final_data = clean_empty(raw_data)

            if final_data:
                logger.info(f"METADATA:{event_type}:{final_data}")

        except Exception as e:
            logger.error(f"Metadata Sync Error: {e}")
