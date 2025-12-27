"""Sendspin server application."""

import asyncio
import errno
import logging
import signal
import socket
import uuid
from contextlib import suppress
from dataclasses import dataclass

import qrcode
from aiosendspin.server import (
    ClientAddedEvent,
    ClientRemovedEvent,
    SendspinEvent,
    SendspinServer,
    SendspinGroup,
)
from aiosendspin.server.stream import MediaStream

from .server import SendspinPlayerServer
from .source import decode_audio

logger = logging.getLogger(__name__)


def print_qr_code(url: str) -> None:
    """Print a QR code to the console."""
    qr = qrcode.QRCode(
        error_correction=qrcode.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def get_local_ip() -> str:
    """Get the local IP address of this machine on the LAN."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "localhost"


@dataclass
class ServeConfig:
    """Configuration for the serve command."""

    source: str
    port: int = 8927
    name: str = "Sendspin Server"


async def run_server(config: ServeConfig) -> int:
    """Run the Sendspin server with the given audio source."""
    event_loop = asyncio.get_event_loop()
    server_id = f"sendspin-cli-{uuid.uuid4().hex[:8]}"

    server = SendspinPlayerServer(
        loop=event_loop,
        server_id=server_id,
        server_name=config.name,
    )

    client_connected = asyncio.Event()
    active_group: SendspinGroup | None = None
    shutdown_requested = False

    def handle_sigint() -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        print("\nShutting down...")
        # Cancel any active playback to trigger shutdown
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    event_loop.add_signal_handler(signal.SIGINT, handle_sigint)

    async def on_server_event(server: SendspinServer, event: SendspinEvent) -> None:
        nonlocal active_group

        if isinstance(event, ClientAddedEvent):
            client = server.get_client(event.client_id)
            assert client is not None

            print("Client connected", event.client_id)

            if active_group is None:
                active_group = client.group
                client_connected.set()
                return

            await active_group.add_client(client)

        if isinstance(event, ClientRemovedEvent):
            if active_group is None:
                return

            print("Client disconnected", event.client_id)

            if not active_group.clients:
                active_group = None

    server.add_event_listener(on_server_event)

    # Find an available port
    port = config.port
    max_attempts = 10
    for attempt in range(max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", port))
                break
        except OSError as e:
            if e.errno == errno.EADDRINUSE and attempt < max_attempts - 1:
                port += 1
            else:
                raise
    else:
        raise OSError(f"Could not find available port after {max_attempts} attempts")

    await server.start_server(port=port, discover_clients=False)

    local_ip = get_local_ip()
    url = f"http://{local_ip}:{port}/"
    print(f"\nServer running at {url}")
    if local_ip == "localhost":
        print("Unable to print QR code because no LAN IP available\n")
        print("Open in browser to use the web player")
    else:
        print()
        print_qr_code(url)
        print()
        print("Scan QR to open in browser to use the web player")
    print("Or connect with any Sendspin client")
    print("Press Ctrl+C to quit\n")

    try:
        while not shutdown_requested:
            # Wait for a client to connect
            if not active_group:
                client_connected.clear()
                try:
                    await client_connected.wait()
                except asyncio.CancelledError:
                    if shutdown_requested:
                        break
                    raise

            assert active_group is not None

            # Decode and stream audio
            try:
                audio_source = await decode_audio(config.source)
                media_stream = MediaStream(
                    main_channel_source=audio_source.generator,
                    main_channel_format=audio_source.format,
                )
                await active_group.play_media(media_stream)
            except asyncio.CancelledError:
                if shutdown_requested:
                    break
                # A client disconnected - only reset if no active clients remain
                if all(client.closing for client in active_group.clients):
                    active_group = None
            except Exception as e:
                print(f"Playback error: {e}")
                logger.debug("Playback error", exc_info=True)

    finally:
        event_loop.remove_signal_handler(signal.SIGINT)
        with suppress(Exception):
            # Temp workaround until https://github.com/Sendspin/aiosendspin/pull/108
            for client in server.clients:
                await client.disconnect(retry_connection=False)
            await server.close()

    return 0
