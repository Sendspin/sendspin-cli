"""Command-line interface for running a Sendspin client."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import traceback
from collections.abc import Sequence
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, Protocol

from sendspin.alsa_volume import AVAILABLE as ALSA_AVAILABLE
from sendspin.alsa_volume import (
    AlsaVolumeController,
    async_check_alsa_available as alsa_volume_check_available,
)
from sendspin.hardware_volume import AVAILABLE as HW_VOLUME_AVAILABLE
from sendspin.hardware_volume import HardwareVolumeController
from sendspin.hardware_volume import UNAVAILABLE_REASON as HW_VOLUME_UNAVAILABLE_REASON
from sendspin.hardware_volume import async_check_available as hw_volume_check_available
from sendspin.hook_volume import HookVolumeController
from sendspin.settings import ClientSettings, get_client_settings, get_serve_settings
from sendspin.volume_controller import VolumeController

if TYPE_CHECKING:
    from aiosendspin.models.player import SupportedAudioFormat

    from sendspin.audio_devices import AudioDevice

LOGGER = logging.getLogger(__name__)

PORTAUDIO_NOT_FOUND_MESSAGE = """Error: PortAudio library not found.

Please install PortAudio for your system:
  • Debian/Ubuntu/Raspberry Pi: sudo apt-get install libportaudio2
  • macOS: brew install portaudio
  • Other systems: https://www.portaudio.com/"""

PLAYER_APP_SENTINEL = "player"
EXPLICIT_APPS = frozenset({PLAYER_APP_SENTINEL, "daemon", "serve"})
TOP_LEVEL_ACTIONS = frozenset({"-h", "--help", "--version"})


class ArgumentTarget(Protocol):
    """Minimal protocol for parser-like objects that accept arguments."""

    def add_argument(self, *name_or_flags: str, **kwargs: Any) -> argparse.Action:
        """Add an argument to the target."""


def arg_str_to_bool(v: str) -> bool:
    s = v.lower()
    if s == "true":
        return True
    if s == "false":
        return False
    raise argparse.ArgumentTypeError("Expected true or false")


def list_audio_devices() -> None:
    """List all available audio output devices."""
    try:
        from sendspin.audio_devices import query_devices
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            sys.exit(1)
        raise

    from sendspin.audio_devices import list_alsa_devices

    try:
        devices = query_devices()
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            sys.exit(1)
        raise

    print("Available audio output devices:")
    print()
    for device in devices:
        default_marker = " (default)" if device.is_default else ""
        print(
            f"  [{device.index}] {device.name}{default_marker}\n"
            f"       Channels: {device.output_channels}, "
            f"Sample rate: {device.sample_rate} Hz"
        )
    if devices:
        print(f"\nTo select an audio device:\n  sendspin --audio-device {devices[0].index}")

    if sys.platform.startswith("linux"):
        alsa_devices = list_alsa_devices()
        if alsa_devices:
            print("\nALSA devices (use by name with --audio-device):")
            print()
            for name, description in alsa_devices:
                print(f"  {name}")
                if description:
                    print(f"       {description}")


def _add_player_runtime_options(target: ArgumentTarget, *, suppress_defaults: bool = False) -> None:
    """Add the interactive player's runtime options."""
    default: str | float | None
    default = argparse.SUPPRESS if suppress_defaults else None

    target.add_argument(
        "--url",
        default=default,
        help=("WebSocket URL of the Sendspin server. If omitted, discover via mDNS."),
    )
    target.add_argument(
        "--name",
        default=default,
        help="Friendly name for this client (defaults to hostname)",
    )
    target.add_argument(
        "--id",
        default=default,
        help="Unique identifier for this client (defaults to sendspin-cli-<hostname>)",
    )
    target.add_argument(
        "--log-level",
        default=default,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use (default: INFO)",
    )
    target.add_argument(
        "--static-delay-ms",
        type=float,
        default=default,
        help="Extra playback delay in milliseconds applied after clock sync",
    )
    target.add_argument(
        "--audio-device",
        type=str,
        default=default,
        help=(
            "Audio output device by index (e.g., 0, 1, 2), name prefix (e.g., 'MacBook'), "
            "or raw ALSA device name (e.g., 'dmixer', 'olohuone') for plugin devices like dmix. "
            "Use --list-audio-devices to see enumerated devices."
        ),
    )
    target.add_argument(
        "--audio-format",
        type=str,
        default=default,
        help=(
            "Preferred audio format as codec:sample_rate:bit_depth:channels "
            "(e.g., flac:48000:24:2). Verified against the audio device on startup."
        ),
    )
    target.add_argument(
        "--disable-mpris",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Disable MPRIS integration",
    )
    target.add_argument(
        "--hardware-volume",
        default=default,
        type=arg_str_to_bool,
        metavar="{true,false}",
        help="Enable or disable hardware/system volume control (daemon: on, TUI: off)",
    )
    target.add_argument(
        "--hook-start",
        type=str,
        default=default,
        help="Command to run when audio stream starts (receives SENDSPIN_* env vars)",
    )
    target.add_argument(
        "--hook-set-volume",
        type=str,
        default=default,
        help="Script to run for external volume control (receives effective volume 0-100)",
    )
    target.add_argument(
        "--hook-stop",
        type=str,
        default=default,
        help="Command to run when audio stream stops (receives SENDSPIN_* env vars)",
    )


def _add_player_actions(target: ArgumentTarget, *, suppress_defaults: bool = False) -> None:
    """Add actions that should also work with the player app."""
    target.add_argument(
        "--list-audio-devices",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="List available audio output devices and exit",
    )
    target.add_argument(
        "--list-servers",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Discover and list available Sendspin servers on the network",
    )
    target.add_argument(
        "--list-clients",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Discover and list available Sendspin clients on the network",
    )
    target.add_argument(
        "--headless",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help=argparse.SUPPRESS,
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser."""
    parser = argparse.ArgumentParser(
        prog="sendspin",
        description="Sendspin CLI",
    )

    # Keep top-level actions separate from the TUI player's runtime options.
    parser._optionals.title = "Actions"
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('sendspin')}",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        title="Apps",
        help="Available apps (default: player)",
    )

    player_parser = subparsers.add_parser(
        PLAYER_APP_SENTINEL,
        description="Run the interactive player app.",
        help="Run the interactive player app (default)",
    )
    player_parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('sendspin')}",
    )
    _add_player_runtime_options(player_parser)
    _add_player_actions(player_parser)

    # Serve subcommand
    serve_parser = subparsers.add_parser("serve", help="Start a Sendspin server")
    serve_parser.add_argument(
        "source",
        nargs="?",
        default=None,
        help="Audio source: local file path or URL (http/https)",
    )
    serve_parser.add_argument(
        "--source-format",
        default=None,
        help="ffmpeg container format for source audio",
    )
    serve_parser.add_argument(
        "--demo",
        action="store_true",
        help="Use a demo audio stream (retro dance music)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: 8927)",
    )
    serve_parser.add_argument(
        "--name",
        default=None,
        help="Server name for mDNS discovery (default: Sendspin Server)",
    )
    serve_parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use (default: INFO)",
    )
    serve_parser.add_argument(
        "--client",
        action="append",
        dest="clients",
        default=[],
        help="Client URL to connect to (can be specified multiple times)",
    )

    # Daemon subcommand
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Run Sendspin client in daemon mode (no UI)",
        description=(
            "Run as a headless audio player. By default, listens for incoming server "
            "connections and advertises via mDNS (_sendspin._tcp.local.). "
            "Use --url to connect to a specific server instead."
        ),
    )
    daemon_parser.add_argument(
        "--url",
        default=None,
        help=(
            "WebSocket URL of the Sendspin server to connect to. "
            "If omitted, listen for incoming server connections via mDNS."
        ),
    )
    daemon_parser.add_argument(
        "--port",
        type=int,
        default=None,
        dest="listen_port",
        help="Port to listen on for incoming server connections (default: 8928)",
    )
    daemon_parser.add_argument(
        "--name",
        default=None,
        help="Friendly name for this client (defaults to hostname)",
    )
    daemon_parser.add_argument(
        "--id",
        default=None,
        help="Unique identifier for this client (defaults to sendspin-cli-<hostname>)",
    )
    daemon_parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use (default: INFO)",
    )
    daemon_parser.add_argument(
        "--static-delay-ms",
        type=float,
        default=None,
        help="Extra playback delay in milliseconds applied after clock sync",
    )
    daemon_parser.add_argument(
        "--audio-device",
        type=str,
        default=None,
        help=(
            "Audio output device by index (e.g., 0, 1, 2) or name prefix (e.g., 'MacBook'). "
            "Use --list-audio-devices to see available devices."
        ),
    )
    daemon_parser.add_argument(
        "--audio-format",
        type=str,
        default=None,
        help=(
            "Preferred audio format as codec:sample_rate:bit_depth:channels "
            "(e.g., flac:48000:24:2). Verified against the audio device on startup."
        ),
    )
    daemon_parser.add_argument(
        "--settings-dir",
        type=str,
        default=None,
        help="Directory to store settings (default: ~/.config/sendspin)",
    )
    daemon_parser.add_argument(
        "--disable-mpris",
        action="store_true",
        help="Disable MPRIS integration",
    )
    daemon_parser.add_argument(
        "--hardware-volume",
        default=None,
        type=arg_str_to_bool,
        metavar="{true,false}",
        help="Enable or disable hardware/system volume control (daemon: on, TUI: off)",
    )
    daemon_parser.add_argument(
        "--hook-start",
        type=str,
        default=None,
        help="Command to run when audio stream starts (receives SENDSPIN_* env vars)",
    )
    daemon_parser.add_argument(
        "--hook-set-volume",
        type=str,
        default=None,
        help="Script to run for external volume control (receives effective volume 0-100)",
    )
    daemon_parser.add_argument(
        "--hook-stop",
        type=str,
        default=None,
        help="Command to run when audio stream stops (receives SENDSPIN_* env vars)",
    )

    return parser


def _inject_default_app(argv: Sequence[str]) -> list[str]:
    """Insert the default player app when no explicit app was requested."""
    parsed_argv = list(argv)
    if not parsed_argv:
        return [PLAYER_APP_SENTINEL]

    if parsed_argv[0] in EXPLICIT_APPS | TOP_LEVEL_ACTIONS:
        return parsed_argv

    return [PLAYER_APP_SENTINEL, *parsed_argv]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the Sendspin client."""
    parsed_argv = _inject_default_app(sys.argv[1:] if argv is None else argv)
    return _build_parser().parse_args(parsed_argv)


async def list_servers() -> None:
    """Discover and list all Sendspin servers on the network."""
    from sendspin.discovery import discover_servers

    try:
        servers = await discover_servers(discovery_time=3.0)
        if not servers:
            print("No Sendspin servers found.")
            return

        print(f"\nFound {len(servers)} server(s):")
        print()
        for server in servers:
            print(f"  {server.name}")
            print(f"    URL:  {server.url}")
            print(f"    Host: {server.host}:{server.port}")
    except Exception as e:  # noqa: BLE001
        print(f"Error discovering servers: {e}")
        sys.exit(1)


async def list_clients() -> None:
    """Discover and list all Sendspin clients on the network."""
    from sendspin.discovery import discover_clients

    try:
        clients = await discover_clients(discovery_time=3.0)
        if not clients:
            print("No Sendspin clients found.")
            return

        print(f"\nFound {len(clients)} client(s):")
        print()
        for client in clients:
            print(f"  {client.name}")
            print(f"    URL:  {client.url}")
            print(f"    Host: {client.host}:{client.port}")
    except Exception as e:  # noqa: BLE001
        print(f"Error discovering clients: {e}")
        sys.exit(1)


class CLIError(Exception):
    """CLI error with exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _resolve_client_info(client_id: str | None, client_name: str | None) -> tuple[str, str]:
    """Determine client ID and name, using hostname as fallback."""
    if client_id is not None and client_name is not None:
        return client_id, client_name

    hostname = socket.gethostname()
    if not hostname:
        raise CLIError("Unable to determine hostname. Please specify --id and/or --name", 1)

    return (
        client_id or f"sendspin-cli-{hostname}",
        client_name or hostname,
    )


def _resolve_preferred_format(
    format_arg: str | None, device: AudioDevice
) -> SupportedAudioFormat | None:
    """Resolve the preferred audio format, if specified."""
    if format_arg is None:
        return None

    from sendspin.audio_devices import parse_audio_format, validate_audio_format

    fmt = parse_audio_format(format_arg)

    if not validate_audio_format(fmt, device):
        raise ValueError(
            f"Audio format '{format_arg}' is not supported by device "
            f"'{device.name}' ({device.device_id})."
        )

    LOGGER.info("Using preferred audio format: %s", format_arg)
    return fmt


async def _run_serve_mode(args: argparse.Namespace) -> int:
    """Run the server mode."""
    from sendspin.serve import ServeConfig, run_server

    # Load settings for serve mode
    settings = await get_serve_settings()

    # Apply settings defaults
    if args.port is None:
        args.port = settings.listen_port or 8927
    if args.name is None:
        args.name = settings.name or "Sendspin Server"
    if args.log_level is None:
        args.log_level = settings.log_level or "INFO"

    # Set up logging
    logging.basicConfig(level=getattr(logging, args.log_level))

    # Determine audio source: CLI > --demo > settings
    if args.demo:
        source = "http://retro.dancewave.online/retrodance.mp3"
        print(f"Demo mode enabled, serving URL {source}")
    elif args.source:
        source = args.source
    elif settings.source:
        source = settings.source
        print(f"Using source from settings: {source}")
    else:
        print("Error: either provide a source or use --demo")
        return 1

    serve_config = ServeConfig(
        source=source,
        source_format=args.source_format or settings.source_format,
        port=args.port,
        name=args.name,
        clients=args.clients or settings.clients,
    )
    return await run_server(serve_config)


async def _run_daemon_mode(
    args: argparse.Namespace,
    settings: ClientSettings,
    audio_device: AudioDevice,
    volume_controller: VolumeController | None,
) -> int:
    """Run the client in daemon mode (no UI)."""
    from sendspin.daemon.daemon import DaemonArgs, SendspinDaemon

    client_id, client_name = _resolve_client_info(args.id, args.name)

    daemon_args = DaemonArgs(
        audio_device=audio_device,
        url=args.url,
        client_id=client_id,
        client_name=client_name,
        settings=settings,
        static_delay_ms=args.static_delay_ms,
        listen_port=args.listen_port,
        use_mpris=args.use_mpris,
        preferred_format=_resolve_preferred_format(args.audio_format, audio_device),
        volume_controller=volume_controller,
        hook_start=args.hook_start,
        hook_stop=args.hook_stop,
    )

    daemon = SendspinDaemon(daemon_args)
    return await daemon.run()


def main() -> int:
    """Run the CLI client."""
    args = parse_args(sys.argv[1:])

    # Handle serve subcommand
    if args.command == "serve":
        try:
            return asyncio.run(_run_serve_mode(args))
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"Server error: {e}")
            traceback.print_exc()
            return 1

    if args.command == PLAYER_APP_SENTINEL:
        # Handle player-only actions before starting async runtime.
        if args.list_audio_devices:
            list_audio_devices()
            return 0

        if args.list_servers:
            asyncio.run(list_servers())
            return 0

        if args.list_clients:
            asyncio.run(list_clients())
            return 0

    try:
        return asyncio.run(_run_client_mode(args))
    except CLIError as e:
        print(f"Error: {e}")
        return e.exit_code
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            return 1
        raise


async def _run_client_mode(args: argparse.Namespace) -> int:
    """Run the client in TUI or daemon mode."""
    from sendspin.audio_devices import resolve_audio_device

    # Handle deprecated --headless flag early so all downstream logic
    # can simply check args.command == "daemon".
    if getattr(args, "headless", False):
        print("Warning: --headless is deprecated. Use 'sendspin daemon' instead.")
        print("Routing to daemon mode...\n")
        args.command = "daemon"

    is_daemon = args.command == "daemon"
    settings_dir = getattr(args, "settings_dir", None)
    settings = await get_client_settings("daemon" if is_daemon else "tui", settings_dir)

    # Apply settings as defaults for CLI arguments (CLI > settings > hard-coded)
    url_from_settings = False
    if args.url is None and settings.last_server_url:
        args.url = settings.last_server_url
        url_from_settings = True
    if args.name is None:
        args.name = settings.name
    if args.id is None:
        args.id = settings.client_id
    if args.audio_device is None:
        args.audio_device = settings.audio_device
    if args.static_delay_ms is None and settings.static_delay_ms != 0.0:
        args.static_delay_ms = settings.static_delay_ms
    if args.log_level is None:
        args.log_level = settings.log_level or "INFO"
    if is_daemon and getattr(args, "listen_port", None) is None:
        args.listen_port = settings.listen_port or 8928
    args.use_mpris = not args.disable_mpris and settings.use_mpris
    if args.audio_format is None:
        args.audio_format = settings.audio_format
    if args.hardware_volume is None:
        if settings.use_hardware_volume is not None:
            args.hardware_volume = settings.use_hardware_volume
        else:
            args.hardware_volume = is_daemon and (HW_VOLUME_AVAILABLE or ALSA_AVAILABLE)
    if args.hook_set_volume is None:
        args.hook_set_volume = settings.hook_set_volume
    if not args.hook_set_volume and args.hardware_volume and not HW_VOLUME_AVAILABLE:
        # ALSA volume control (via amixer) does not require PulseAudio, so
        # only error out when we are also not on Linux (where ALSA is available).
        if not ALSA_AVAILABLE:
            raise CLIError(
                f"Hardware volume control is not available on this system. "
                f"{HW_VOLUME_UNAVAILABLE_REASON or 'Use --hardware-volume false to disable.'}"
            )
    if args.hook_start is None:
        args.hook_start = settings.hook_start
    if args.hook_stop is None:
        args.hook_stop = settings.hook_stop

    # Set up logging: daemon uses stderr, TUI writes to sendspin.log
    # so log output doesn't interfere with the Rich display.
    log_level = getattr(logging, args.log_level)
    if is_daemon:
        logging.basicConfig(level=log_level)
    else:
        if log_level > logging.DEBUG:
            log_level = logging.WARNING
        handler = logging.FileHandler(os.path.join(os.getcwd(), "sendspin.log"), delay=True)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.basicConfig(level=log_level, handlers=[handler])

    audio_device = resolve_audio_device(args.audio_device)

    volume_controller: VolumeController | None = None
    if args.hook_set_volume:
        LOGGER.info("Using hook-based external volume control via %s", args.hook_set_volume)
        volume_controller = HookVolumeController(args.hook_set_volume, settings)
    elif args.hardware_volume:
        # Try ALSA direct control first (works for hw: devices without PulseAudio).
        alsa_info = await alsa_volume_check_available(audio_device)
        if alsa_info is not None:
            card, element = alsa_info
            LOGGER.info(
                "Using ALSA mixer volume control: card %d, element %r",
                card,
                element,
            )
            volume_controller = AlsaVolumeController(card=card, element=element)
        elif await hw_volume_check_available(audio_device):
            # Fall back to PulseAudio for virtual devices or when ALSA has no mixer.
            volume_controller = HardwareVolumeController(audio_device)
        else:
            LOGGER.warning(
                "No volume control available for device %r "
                "(no ALSA mixer controls and PulseAudio/PipeWire not reachable), "
                "falling back to software volume control",
                audio_device.name,
            )
            args.hardware_volume = False

    # Handle daemon subcommand
    if args.command == "daemon":
        return await _run_daemon_mode(args, settings, audio_device, volume_controller)

    from sendspin.tui.app import AppArgs, SendspinApp

    client_id, client_name = _resolve_client_info(args.id, args.name)

    app_args = AppArgs(
        audio_device=audio_device,
        url=args.url,
        url_from_settings=url_from_settings,
        client_id=client_id,
        client_name=client_name,
        settings=settings,
        static_delay_ms=args.static_delay_ms,
        use_mpris=args.use_mpris,
        preferred_format=_resolve_preferred_format(args.audio_format, audio_device),
        volume_controller=volume_controller,
        hook_start=args.hook_start,
        hook_stop=args.hook_stop,
    )

    app = SendspinApp(app_args)
    return await app.run()


if __name__ == "__main__":
    raise SystemExit(main())
