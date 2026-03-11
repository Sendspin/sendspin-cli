"""Daemon mode for running a Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import dataclass, fields as dataclass_fields
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, web
from aiosendspin.client import ClientListener, SendspinClient
from aiosendspin.models.core import (
    ClientGoodbyeMessage,
    ClientGoodbyePayload,
    ServerCommandPayload,
    StreamStartMessage,
)
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.visualizer import ClientHelloVisualizerSupport

try:
    from aiosendspin.models.visualizer import ClientHelloVisualizerSpectrum, VisualizerFrame
except ImportError:
    ClientHelloVisualizerSpectrum = None
    VisualizerFrame = Any
from aiosendspin_mpris import MPRIS_AVAILABLE, SendspinMpris
from aiosendspin.models.types import (
    GoodbyeReason,
    PlayerCommand,
    Roles,
)

from sendspin.audio import AudioDevice, detect_supported_audio_formats
from sendspin.audio_connector import AudioStreamHandler
from sendspin.hooks import run_hook
from sendspin.settings import ClientSettings
from sendspin.utils import create_task, get_device_info
from sendspin.visualizer_connector import VisualizerHandler

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
        self._static_delay_ms: float = 0.0
        self._connection_lock: asyncio.Lock | None = None
        self._server_url: str | None = None
        self._visualizer_handler: VisualizerHandler | None = None
        self._visualizer_batch_count: int = 0

    def _create_client(self, static_delay_ms: float = 0.0) -> SendspinClient:
        """Create a new SendspinClient instance."""
        assert self._audio_handler is not None
        client_roles = [Roles.PLAYER]
        visualizer_support = self._build_visualizer_support()
        if visualizer_support is not None:
            client_roles.append(Roles.VISUALIZER)
        if MPRIS_AVAILABLE and self._args.use_mpris:
            client_roles.extend([Roles.METADATA, Roles.CONTROLLER])

        supported_formats = detect_supported_audio_formats(self._args.audio_device.index)
        if self._args.preferred_format is not None:
            supported_formats = [f for f in supported_formats if f != self._args.preferred_format]
            supported_formats.insert(0, self._args.preferred_format)

        return SendspinClient(
            client_id=self._args.client_id,
            client_name=self._args.client_name,
            roles=client_roles,
            device_info=get_device_info(),
            player_support=ClientHelloPlayerSupport(
                supported_formats=supported_formats,
                buffer_capacity=32_000_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
            visualizer_support=visualizer_support,
            static_delay_ms=static_delay_ms,
            initial_volume=self._audio_handler.volume,
            initial_muted=self._audio_handler.muted,
        )

    @staticmethod
    def _build_visualizer_support() -> ClientHelloVisualizerSupport | None:
        """Build draft-r1 visualizer support payload for daemon mode."""
        support_fields = {f.name for f in dataclass_fields(ClientHelloVisualizerSupport)}
        if not {"types", "batch_max", "spectrum"}.issubset(support_fields):
            logger.warning(
                "Installed aiosendspin visualizer support model is outdated; "
                "daemon will continue without visualizer role"
            )
            return None
        return ClientHelloVisualizerSupport(
            buffer_capacity=65536,
            types=["loudness", "spectrum"],
            batch_max=8,
            spectrum=ClientHelloVisualizerSpectrum(
                n_disp_bins=48,
                scale="mel",
                f_min=20,
                f_max=20000,
                rate_max=30,
            ),
        )

    def _attach_visualizer_debug(self, client: SendspinClient) -> None:
        """Attach visualizer debug callbacks for daemon mode."""
        if not hasattr(client, "add_visualizer_listener"):
            logger.warning(
                "Installed aiosendspin client lacks visualizer listener API; "
                "visualizer debug disabled"
            )
            return
        # Raw binary tap: log every visualization binary payload before parsing.
        raw_handler = getattr(client, "_handle_visualization_data", None)
        if callable(raw_handler):

            def _wrapped_visualization_data(payload: bytes) -> None:
                logger.info(
                    "Visualizer raw binary received: bytes=%d first8=%s",
                    len(payload),
                    payload[:8].hex(),
                )
                raw_handler(payload)

            setattr(client, "_handle_visualization_data", _wrapped_visualization_data)
            logger.info("Visualizer raw binary tap attached")

        self._visualizer_batch_count = 0
        client.add_visualizer_listener(self._on_visualizer_batch)
        client.add_stream_start_listener(self._on_stream_start_debug)
        self._visualizer_handler = VisualizerHandler(on_frame=self._on_visualizer_due_frame)
        self._visualizer_handler.attach_client(client)

    def _on_visualizer_batch(self, frames: list[VisualizerFrame]) -> None:
        """Short log for each incoming visualizer binary batch."""
        self._visualizer_batch_count += 1
        if not frames:
            logger.info("Visualizer batch #%d received: empty", self._visualizer_batch_count)
            return
        latest = frames[-1]
        bins = len(latest.spectrum) if latest.spectrum is not None else 0
        logger.info(
            "Visualizer batch #%d received: frames=%d latest_ts=%d bins=%d loud=%s",
            self._visualizer_batch_count,
            len(frames),
            latest.timestamp_us,
            bins,
            latest.loudness,
        )

    def _on_stream_start_debug(self, message: StreamStartMessage) -> None:
        """Log stream/start payload shape for visualizer diagnostics."""
        has_player = message.payload.player is not None
        has_visualizer = message.payload.visualizer is not None
        vis_types = (
            message.payload.visualizer.get("types")
            if isinstance(message.payload.visualizer, dict)
            else None
        )
        logger.info(
            "Stream/start received: player=%s visualizer=%s types=%s",
            has_player,
            has_visualizer,
            vis_types,
        )

    def _on_visualizer_due_frame(self, frame: VisualizerFrame) -> None:
        """Print one line whenever a frame should be visualized (timestamp due)."""
        if frame.timestamp_us == 0 and frame.spectrum is None and frame.loudness is None:
            return
        bins = len(frame.spectrum) if frame.spectrum is not None else 0
        print(  # noqa: T201
            "[viz-due] "
            f"mono_us={int(time.monotonic() * 1_000_000)} "
            f"server_ts={frame.timestamp_us} bins={bins} loud={frame.loudness}"
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

        try:
            if self._args.url is not None:
                # Client-initiated connection mode
                await self._run_client_initiated(delay)
            else:
                # Server-initiated connection mode (listen for incoming connections)
                await self._run_server_initiated(delay)
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
            if self._settings:
                await self._settings.flush()
            logger.info("Daemon stopped")

        return 0

    def _on_volume_change(self, volume: int, muted: bool) -> None:
        """Handle volume changes from any source (server command, external, etc.)."""
        assert self._settings is not None
        assert self._audio_handler is not None

        if not self._audio_handler.uses_external_volume_controller:
            self._settings.update(player_volume=volume, player_muted=muted)

    async def _run_client_initiated(self, static_delay_ms: float) -> None:
        """Run in client-initiated mode, connecting to a specific URL."""
        assert self._args.url is not None
        assert self._audio_handler is not None
        self._client = self._create_client(static_delay_ms)
        if MPRIS_AVAILABLE and self._args.use_mpris:
            self._mpris = SendspinMpris(self._client)
            self._mpris.start()
        self._audio_handler.attach_client(self._client)
        self._attach_visualizer_debug(self._client)
        self._server_url = self._args.url
        self._client.add_server_command_listener(self._handle_server_command)
        await self._connection_loop(self._args.url)

    async def _run_server_initiated(self, static_delay_ms: float) -> None:
        """Run in server-initiated mode, listening for incoming connections."""
        logger.info(
            "Listening for server connections on port %d (mDNS: _sendspin._tcp.local.)",
            self._args.listen_port,
        )

        self._static_delay_ms = static_delay_ms  # Store for use in connection handler
        self._connection_lock = asyncio.Lock()

        self._listener = ClientListener(
            client_id=self._args.client_id,
            on_connection=self._handle_server_connection,
            port=self._args.listen_port,
            client_name=self._args.client_name,
        )
        await self._listener.start()

        # Keep running until cancelled
        while True:
            await asyncio.sleep(3600)

    async def _handle_disconnect(self, *, stop_mpris: bool = True) -> None:
        """Reset connection-scoped state and optionally stop MPRIS."""
        if self._visualizer_handler is not None:
            self._visualizer_handler.detach()
            self._visualizer_handler = None
        if stop_mpris and self._mpris is not None:
            self._mpris.stop()
            self._mpris = None
        if self._audio_handler is not None:
            await self._audio_handler.handle_disconnect()

    async def _handle_server_connection(self, ws: web.WebSocketResponse) -> None:
        """Handle an incoming server connection."""
        logger.info("Server connected")
        assert self._audio_handler is not None
        assert self._connection_lock is not None
        assert self._settings is not None

        # Lock ensures we wait for any in-progress handshake to complete
        # before disconnecting the previous server
        async with self._connection_lock:
            # Clean up any previous client
            if self._client is not None:
                logger.info("Disconnecting from previous server")
                await self._handle_disconnect()
                if self._client.connected:
                    try:
                        await self._client._send_message(  # noqa: SLF001
                            ClientGoodbyeMessage(
                                payload=ClientGoodbyePayload(reason=GoodbyeReason.ANOTHER_SERVER)
                            ).to_json()
                        )
                    except Exception:
                        logger.debug("Failed to send goodbye message", exc_info=True)
                await self._client.disconnect()

            # Create a new client for this connection
            client = self._create_client(self._static_delay_ms)
            self._client = client
            self._audio_handler.attach_client(client)
            self._attach_visualizer_debug(client)
            client.add_server_command_listener(self._handle_server_command)
            if MPRIS_AVAILABLE and self._args.use_mpris:
                self._mpris = SendspinMpris(client)
                self._mpris.start()

            try:
                await client.attach_websocket(ws)
            except TimeoutError:
                logger.warning("Handshake with server timed out")
                await self._handle_disconnect()
                if self._client is client:
                    self._client = None
                return
            except Exception:
                logger.exception("Error during server handshake")
                await self._handle_disconnect()
                if self._client is client:
                    self._client = None
                return

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
                # Keep MPRIS alive across reconnects in client-initiated mode.
                await self._handle_disconnect(stop_mpris=False)

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
