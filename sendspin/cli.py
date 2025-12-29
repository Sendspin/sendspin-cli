"""Command-line interface for running a Sendspin client."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sendspin.app import AppConfig, SendspinApp
from sendspin.discovery import discover_servers


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
        help="Run without the interactive terminal UI",
    )
    return parser.parse_args(argv)


def list_audio_devices() -> None:
    """List all available audio output devices."""
    import sounddevice  # Lazy import to avoid requiring PortAudio for other commands

    try:
        devices = sounddevice.query_devices()
        default_device = sounddevice.default.device[1]  # Output device index

        print("Available audio output devices:")
        print()
        for i, device in enumerate(devices):
            if device["max_output_channels"] > 0:
                default_marker = " (default)" if i == default_device else ""
                print(
                    f"  [{i}] {device['name']}{default_marker}\n"
                    f"       Channels: {device['max_output_channels']}, "
                    f"Sample rate: {device['default_samplerate']} Hz"
                )
        if devices:
            print("\nTo select an audio device:\n  sendspin --audio-device 0")

    except Exception as e:  # noqa: BLE001
        print(f"Error listing audio devices: {e}")
        sys.exit(1)


async def list_servers() -> None:
    """Discover and list all Sendspin servers on the network."""
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


def main() -> int:
    """Run the CLI client."""
    args = parse_args(sys.argv[1:])

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

        config = ServeConfig(
            source=source,
            port=args.port,
            name=args.name,
        )
        try:
            return asyncio.run(run_server(config))
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

    # Create config from CLI arguments
    config = AppConfig(
        url=args.url,
        client_id=args.id,
        client_name=args.name,
        static_delay_ms=args.static_delay_ms,
        audio_device=args.audio_device,
        log_level=args.log_level,
        headless=args.headless,
    )

    # Run the application
    app = SendspinApp(config)
    return asyncio.run(app.run())


if __name__ == "__main__":
    raise SystemExit(main())
