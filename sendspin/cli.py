"""Command-line interface for running a Sendspin client."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import traceback
from collections.abc import Sequence
from importlib.metadata import version
from typing import TYPE_CHECKING

from sendspin.settings import SettingsManager, SettingsMode, get_settings_manager

if TYPE_CHECKING:
    from sendspin.audio import AudioDevice

LOGGER = logging.getLogger(__name__)

PORTAUDIO_NOT_FOUND_MESSAGE = """Error: PortAudio library not found.

Please install PortAudio for your system:
  • Debian/Ubuntu/Raspberry Pi: sudo apt-get install libportaudio2
  • macOS: brew install portaudio
  • Other systems: https://www.portaudio.com/"""


def list_audio_devices() -> None:
    """List all available audio output devices."""
    try:
        from sendspin.audio import query_devices
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            sys.exit(1)
        raise

    try:
        devices = query_devices()

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
            print("\nTo select an audio device:\n  sendspin --audio-device 0")

    except Exception as e:  # noqa: BLE001
        print(f"Error listing audio devices: {e}")
        sys.exit(1)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the Sendspin client."""
    parser = argparse.ArgumentParser(description="Sendspin CLI")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('sendspin')}",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Serve subcommand
    serve_parser = subparsers.add_parser("serve", help="Start a Sendspin server")
    serve_parser.add_argument(
        "source",
        nargs="?",
        default=None,
        help="Audio source: local file path or URL (http/https)",
    )
    serve_parser.add_argument(
        "--demo",
        action="store_true",
        help="Use a demo audio stream (retro dance music)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8927,
        help="Port to listen on (default: 8927)",
    )
    serve_parser.add_argument(
        "--name",
        default="Sendspin Server",
        help="Server name for mDNS discovery",
    )
    serve_parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use",
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
        help="Port to listen on for incoming server connections (default: 8927)",
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

    # Default behavior (client mode) - existing arguments
    parser.add_argument(
        "--url",
        default=None,
        help=("WebSocket URL of the Sendspin server. If omitted, discover via mDNS."),
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Friendly name for this client (defaults to hostname)",
    )
    parser.add_argument(
        "--id",
        default=None,
        help="Unique identifier for this client (defaults to sendspin-cli-<hostname>)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use (default: INFO)",
    )
    parser.add_argument(
        "--static-delay-ms",
        type=float,
        default=None,
        help="Extra playback delay in milliseconds applied after clock sync",
    )
    parser.add_argument(
        "--audio-device",
        type=str,
        default=None,
        help=(
            "Audio output device by index (e.g., 0, 1, 2) or name prefix (e.g., 'MacBook'). "
            "Use --list-audio-devices to see available devices."
        ),
    )
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="List available audio output devices and exit",
    )
    parser.add_argument(
        "--list-servers",
        action="store_true",
        help="Discover and list available Sendspin servers on the network",
    )
    parser.add_argument(
        "--disable-mpris",
        action="store_true",
        help="Disable MPRIS integration",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="(DEPRECATED: use 'sendspin daemon' instead) Run without the interactive terminal UI",
    )
    return parser.parse_args(argv)


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


class CLIError(Exception):
    """CLI error with exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _apply_settings_defaults(args: argparse.Namespace, settings: SettingsManager) -> None:
    """Apply settings as defaults for CLI arguments that weren't provided.

    CLI arguments take precedence over settings. Settings take precedence over
    hard-coded defaults.

    Args:
        args: Parsed CLI arguments (modified in place).
        settings: Loaded settings manager to use as defaults.
    """
    # Apply settings as defaults for unset CLI arguments
    if args.url is None:
        args.url = settings.last_server_url
    if args.name is None:
        args.name = settings.client_name
    if args.id is None:
        args.id = settings.client_id
    if args.audio_device is None:
        args.audio_device = settings.audio_device
    if args.static_delay_ms is None and settings.static_delay_ms != 0.0:
        args.static_delay_ms = settings.static_delay_ms

    # Log level defaults to settings value or INFO
    if args.log_level is None:
        args.log_level = settings.log_level or "INFO"

    # Daemon-only: listen port defaults to settings value or 8927
    if args.command == "daemon" and args.port is None:
        args.port = settings.listen_port or 8927

    # MPRIS: CLI flag overrides config
    args.use_mpris = not getattr(args, "disable_mpris", False) and settings.use_mpris


def _resolve_audio_device(device_arg: str | None) -> AudioDevice:
    """Resolve audio device from CLI argument.

    Args:
        device_arg: Device specifier (index number, name prefix, or None for default).

    Returns:
        The resolved AudioDevice.

    Raises:
        CLIError: If the device cannot be found.
    """
    from sendspin.audio import query_devices

    devices = query_devices()

    # Find device by: default, index, or name prefix
    if device_arg is None:
        device = next((d for d in devices if d.is_default), None)
    elif device_arg.isnumeric():
        device_id = int(device_arg)
        device = next((d for d in devices if d.index == device_id), None)
    else:
        device = next((d for d in devices if d.name.startswith(device_arg)), None)

    if device is None:
        kind = "Default" if device_arg is None else "Specified"
        raise CLIError(f"{kind} audio device not found.")

    LOGGER.info("Using audio device %d: %s", device.index, device.name)
    return device


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


async def _run_daemon_mode(args: argparse.Namespace, settings: SettingsManager) -> int:
    """Run the client in daemon mode (no UI)."""
    from sendspin.daemon.daemon import DaemonArgs, SendspinDaemon

    client_id, client_name = _resolve_client_info(args.id, args.name)

    daemon_args = DaemonArgs(
        audio_device=_resolve_audio_device(args.audio_device),
        url=args.url,
        client_id=client_id,
        client_name=client_name,
        settings=settings,
        static_delay_ms=args.static_delay_ms,
        listen_port=args.port,
        use_mpris=args.use_mpris,
    )

    daemon = SendspinDaemon(daemon_args)
    return await daemon.run()


def main() -> int:
    """Run the CLI client."""
    args = parse_args(sys.argv[1:])

    # Handle serve subcommand (doesn't use settings)
    if args.command == "serve":
        logging.basicConfig(level=getattr(logging, args.log_level or "INFO"))

        from sendspin.serve import ServeConfig, run_server

        # Determine audio source
        if args.demo:
            source = "http://retro.dancewave.online/retrodance.mp3"
            print(f"Demo mode enabled, serving URL {source}")
        elif args.source:
            source = args.source
        else:
            print("Error: either provide a source or use --demo")
            return 1

        serve_config = ServeConfig(
            source=source,
            port=args.port,
            name=args.name,
            clients=args.clients or None,
        )
        try:
            return asyncio.run(run_server(serve_config))
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"Server error: {e}")
            traceback.print_exc()
            return 1

    # Handle --list-audio-devices before starting async runtime
    if args.list_audio_devices:
        list_audio_devices()
        return 0

    if args.list_servers:
        asyncio.run(list_servers())
        return 0

    try:
        return asyncio.run(_run_client_mode(args))
    except CLIError as e:
        print(f"Error: {e}")
        return e.exit_code
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            return 1
        raise


async def _run_client_mode(args: argparse.Namespace) -> int:
    """Run the client in TUI or daemon mode."""
    # Determine mode and load settings
    is_daemon = args.command == "daemon" or getattr(args, "headless", False)
    mode = SettingsMode.DAEMON if is_daemon else SettingsMode.TUI
    settings_dir = getattr(args, "settings_dir", None)
    settings = await get_settings_manager(mode, settings_dir)

    # Apply settings as defaults for CLI arguments
    _apply_settings_defaults(args, settings)

    # Set up logging with resolved log level
    logging.basicConfig(level=getattr(logging, args.log_level))

    # Handle daemon subcommand
    if args.command == "daemon":
        return await _run_daemon_mode(args, settings)

    # Handle deprecated --headless flag
    if args.headless:
        print("Warning: --headless is deprecated. Use 'sendspin daemon' instead.")
        print("Routing to daemon mode...\n")
        return await _run_daemon_mode(args, settings)

    from sendspin.tui.app import AppArgs, SendspinApp

    client_id, client_name = _resolve_client_info(args.id, args.name)

    app_args = AppArgs(
        audio_device=_resolve_audio_device(args.audio_device),
        url=args.url,
        client_id=client_id,
        client_name=client_name,
        settings=settings,
        static_delay_ms=args.static_delay_ms,
        use_mpris=args.use_mpris,
    )

    app = SendspinApp(app_args)
    return await app.run()


if __name__ == "__main__":
    raise SystemExit(main())
