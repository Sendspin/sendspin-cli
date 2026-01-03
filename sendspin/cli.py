"""Command-line interface for running a Sendspin client."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sendspin.audio import AudioDevice

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

    # Daemon subcommand
    daemon_parser = subparsers.add_parser(
        "daemon", help="Run Sendspin client in daemon mode (no UI)"
    )
    daemon_parser.add_argument(
        "--url",
        default=None,
        help=("WebSocket URL of the Sendspin server. If omitted, discover via mDNS."),
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
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use",
    )
    daemon_parser.add_argument(
        "--static-delay-ms",
        type=float,
        default=0.0,
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
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use",
    )
    parser.add_argument(
        "--static-delay-ms",
        type=float,
        default=0.0,
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
        if servers:
            print(f"\nTo connect to a server:\n  sendspin --url {servers[0].url}")
    except Exception as e:  # noqa: BLE001
        print(f"Error discovering servers: {e}")
        sys.exit(1)


class CLIError(Exception):
    """CLI error with exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


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

    if device_arg is None:
        device = next((d for d in devices if d.is_default), None)
    elif device_arg.isnumeric():
        device_id = int(device_arg)
        device = next((d for d in devices if d.index == device_id), None)
    else:
        # Find first output device whose name starts with the prefix
        device = next((d for d in devices if d.name.startswith(device_arg)), None)

    if device is None:
        dev_type = "Default" if device_arg is None else "Specified"
        raise CLIError(f"{dev_type} audio device not found.")

    return device


def _run_daemon_mode(args: argparse.Namespace) -> int:
    """Run the client in daemon mode (no UI)."""
    from sendspin.daemon.daemon import DaemonConfig, SendspinDaemon

    daemon_config = DaemonConfig(
        audio_device=_resolve_audio_device(args.audio_device),
        url=args.url,
        client_id=args.id,
        client_name=args.name,
        static_delay_ms=args.static_delay_ms,
    )

    daemon = SendspinDaemon(daemon_config)
    return asyncio.run(daemon.run())


def main() -> int:
    """Run the CLI client."""
    import logging

    args = parse_args(sys.argv[1:])

    logging.basicConfig(level=getattr(logging, args.log_level))

    # Handle serve subcommand
    if args.command == "serve":
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
        )
        try:
            return asyncio.run(run_server(serve_config))
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"Server error: {e}")
            import traceback

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
        return _run_client_mode(args)
    except CLIError as e:
        print(f"Error: {e}")
        return e.exit_code
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            return 1
        raise


def _run_client_mode(args: argparse.Namespace) -> int:
    """Run the client in TUI or daemon mode."""
    # Handle daemon subcommand
    if args.command == "daemon":
        return _run_daemon_mode(args)

    # Handle deprecated --headless flag
    if args.headless:
        print("Warning: --headless is deprecated. Use 'sendspin daemon' instead.")
        print("Routing to daemon mode...\n")
        return _run_daemon_mode(args)

    from sendspin.tui.app import AppConfig, SendspinApp

    app_config = AppConfig(
        audio_device=_resolve_audio_device(args.audio_device),
        url=args.url,
        client_id=args.id,
        client_name=args.name,
        static_delay_ms=args.static_delay_ms,
    )

    app = SendspinApp(app_config)
    return asyncio.run(app.run())


if __name__ == "__main__":
    raise SystemExit(main())
