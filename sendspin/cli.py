"""Command-line interface for running a Sendspin client."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence


def list_audio_devices() -> None:
    """List all available audio output devices."""
    from sendspin.audio import query_devices

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
        "--calibrate-sync",
        action="store_true",
        help="Enable sync calibration mode: mute output and use mic to measure timing offset",
    )
    parser.add_argument(
        "--mic-device",
        type=int,
        default=None,
        help="Microphone device ID for sync calibration (use --list-audio-devices to see devices)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without the interactive terminal UI",
    )
    return parser.parse_args(argv)


def list_audio_devices() -> None:
    """List all available audio devices (output and input)."""
    import sounddevice  # Lazy import to avoid requiring PortAudio for other commands

    try:
        devices = sounddevice.query_devices()
        default_output = sounddevice.default.device[1]  # Output device index
        default_input = sounddevice.default.device[0]  # Input device index

        print("Available audio OUTPUT devices:")
        print("-" * 80)
        for i, device in enumerate(devices):
            if device["max_output_channels"] > 0:
                default_marker = " (default)" if i == default_output else ""
                print(
                    f"  [{i}] {device['name']}{default_marker}\n"
                    f"       Channels: {device['max_output_channels']}, "
                    f"Sample rate: {device['default_samplerate']} Hz"
                )

        print("\nAvailable audio INPUT devices (for --mic-device):")
        print("-" * 80)
        for i, device in enumerate(devices):
            if device["max_input_channels"] > 0:
                default_marker = " (default)" if i == default_input else ""
                print(
                    f"  [{i}] {device['name']}{default_marker}\n"
                    f"       Channels: {device['max_input_channels']}, "
                    f"Sample rate: {device['default_samplerate']} Hz"
                )

        if devices:
            print("\nTo select an audio device:\n  sendspin --audio-device 0")
    except Exception as e:  # noqa: BLE001
        print(f"Error listing audio devices: {e}")
        sys.exit(1)


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

    from sendspin.audio import query_devices
    from sendspin.app import AppConfig, SendspinApp

    # Resolve audio device if specified
    audio_device = None
    devices = query_devices()
    if args.audio_device is None:
        audio_device = next((d for d in devices if d.is_default), None)
    elif args.audio_device.isnumeric():
        device_id = int(args.audio_device)
        for dev in devices:
            if dev.index == device_id:
                audio_device = dev
                break
    else:
        # Otherwise, find first output device whose name starts with the prefix
        for dev in devices:
            if dev.name.startswith(args.audio_device):
                audio_device = dev
                break

    if audio_device is None:
        dev_type = "Default" if args.audio_device is None else "Specified"
        print(f"Error: {dev_type} audio device not found.")
        return 1

    # Create config from CLI arguments
    app_config = AppConfig(
        url=args.url,
        client_id=args.id,
        client_name=args.name,
        static_delay_ms=args.static_delay_ms,
        audio_device=audio_device,
        headless=args.headless,
        calibrate_sync=args.calibrate_sync,
        mic_device=args.mic_device,
    )

    # Run the application
    app = SendspinApp(app_config)
    return asyncio.run(app.run())


if __name__ == "__main__":
    raise SystemExit(main())
