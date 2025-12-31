"""Client listener wrapper for multi-listener support.

aiosendspin only supports set_*_listener (single listener per event type).
This module provides add_*_listener methods that allow multiple listeners
per event type by wrapping them into a single combined listener.

Temporary until https://github.com/Sendspin/aiosendspin/pull/112 is merged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from aiosendspin.client import PCMFormat, SendspinClient
    from aiosendspin.models.core import (
        GroupUpdateServerPayload,
        ServerCommandPayload,
        ServerStatePayload,
        StreamStartMessage,
    )
    from aiosendspin.models.types import Roles

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Type aliases for listener signatures
MetadataListener = Callable[["ServerStatePayload"], None]
GroupUpdateListener = Callable[["GroupUpdateServerPayload"], None]
ControllerStateListener = Callable[["ServerStatePayload"], None]
ServerCommandListener = Callable[["ServerCommandPayload"], None]
AudioChunkListener = Callable[[int, bytes, "PCMFormat"], None]
StreamStartListener = Callable[["StreamStartMessage"], None]
StreamEndListener = Callable[[list["Roles"] | None], None]
StreamClearListener = Callable[[list["Roles"] | None], None]
DisconnectListener = Callable[[], None]


class ClientListenerManager:
    """Manages multiple listeners per event type for a SendspinClient.

    This class wraps a SendspinClient and provides add_*_listener methods
    that allow registering multiple callbacks. When attach() is called,
    it sets combined listeners on the client that dispatch to all registered
    callbacks.
    """

    def __init__(self) -> None:
        """Initialize the listener manager."""
        self._metadata_listeners: list[MetadataListener] = []
        self._group_update_listeners: list[GroupUpdateListener] = []
        self._controller_state_listeners: list[ControllerStateListener] = []
        self._server_command_listeners: list[ServerCommandListener] = []
        self._audio_chunk_listeners: list[AudioChunkListener] = []
        self._stream_start_listeners: list[StreamStartListener] = []
        self._stream_end_listeners: list[StreamEndListener] = []
        self._stream_clear_listeners: list[StreamClearListener] = []
        self._disconnect_listeners: list[DisconnectListener] = []

    def add_metadata_listener(self, listener: MetadataListener) -> Callable[[], None]:
        """Add a metadata listener. Returns unsubscribe function."""
        self._metadata_listeners.append(listener)
        return lambda: self._metadata_listeners.remove(listener)

    def add_group_update_listener(self, listener: GroupUpdateListener) -> Callable[[], None]:
        """Add a group update listener. Returns unsubscribe function."""
        self._group_update_listeners.append(listener)
        return lambda: self._group_update_listeners.remove(listener)

    def add_controller_state_listener(
        self, listener: ControllerStateListener
    ) -> Callable[[], None]:
        """Add a controller state listener. Returns unsubscribe function."""
        self._controller_state_listeners.append(listener)
        return lambda: self._controller_state_listeners.remove(listener)

    def add_server_command_listener(self, listener: ServerCommandListener) -> Callable[[], None]:
        """Add a server command listener. Returns unsubscribe function."""
        self._server_command_listeners.append(listener)
        return lambda: self._server_command_listeners.remove(listener)

    def add_audio_chunk_listener(self, listener: AudioChunkListener) -> Callable[[], None]:
        """Add an audio chunk listener. Returns unsubscribe function."""
        self._audio_chunk_listeners.append(listener)
        return lambda: self._audio_chunk_listeners.remove(listener)

    def add_stream_start_listener(self, listener: StreamStartListener) -> Callable[[], None]:
        """Add a stream start listener. Returns unsubscribe function."""
        self._stream_start_listeners.append(listener)
        return lambda: self._stream_start_listeners.remove(listener)

    def add_stream_end_listener(self, listener: StreamEndListener) -> Callable[[], None]:
        """Add a stream end listener. Returns unsubscribe function."""
        self._stream_end_listeners.append(listener)
        return lambda: self._stream_end_listeners.remove(listener)

    def add_stream_clear_listener(self, listener: StreamClearListener) -> Callable[[], None]:
        """Add a stream clear listener. Returns unsubscribe function."""
        self._stream_clear_listeners.append(listener)
        return lambda: self._stream_clear_listeners.remove(listener)

    def add_disconnect_listener(self, listener: DisconnectListener) -> Callable[[], None]:
        """Add a disconnect listener. Returns unsubscribe function."""
        self._disconnect_listeners.append(listener)
        return lambda: self._disconnect_listeners.remove(listener)

    def attach(self, client: SendspinClient) -> None:
        """Attach all registered listeners to the client.

        Sets combined listeners on the client that dispatch to all
        registered callbacks for each event type.
        """
        # Metadata listener (async)
        if self._metadata_listeners:

            def on_metadata(payload: ServerStatePayload) -> None:
                for listener in self._metadata_listeners:
                    try:
                        listener(payload)
                    except Exception:
                        logger.exception("Error in metadata listener")

            client.set_metadata_listener(on_metadata)

        # Group update listener (async)
        if self._group_update_listeners:

            def on_group_update(payload: GroupUpdateServerPayload) -> None:
                for listener in self._group_update_listeners:
                    try:
                        listener(payload)
                    except Exception:
                        logger.exception("Error in group update listener")

            client.set_group_update_listener(on_group_update)

        # Controller state listener (async)
        if self._controller_state_listeners:

            def on_controller_state(payload: ServerStatePayload) -> None:
                for listener in self._controller_state_listeners:
                    try:
                        listener(payload)
                    except Exception:
                        logger.exception("Error in controller state listener")

            client.set_controller_state_listener(on_controller_state)

        # Server command listener (async)
        if self._server_command_listeners:

            def on_server_command(payload: ServerCommandPayload) -> None:
                for listener in self._server_command_listeners:
                    try:
                        listener(payload)
                    except Exception:
                        logger.exception("Error in server command listener")

            client.set_server_command_listener(on_server_command)

        # Audio chunk listener (sync)
        if self._audio_chunk_listeners:

            def on_audio_chunk(server_timestamp_us: int, audio_data: bytes, fmt: PCMFormat) -> None:
                for listener in self._audio_chunk_listeners:
                    try:
                        listener(server_timestamp_us, audio_data, fmt)
                    except Exception:
                        logger.exception("Error in audio chunk listener")

            client.set_audio_chunk_listener(on_audio_chunk)

        # Stream start listener (sync)
        if self._stream_start_listeners:

            def on_stream_start(message: StreamStartMessage) -> None:
                for listener in self._stream_start_listeners:
                    try:
                        listener(message)
                    except Exception:
                        logger.exception("Error in stream start listener")

            client.set_stream_start_listener(on_stream_start)

        # Stream end listener (sync)
        if self._stream_end_listeners:

            def on_stream_end(roles: list[Roles] | None) -> None:
                for listener in self._stream_end_listeners:
                    try:
                        listener(roles)
                    except Exception:
                        logger.exception("Error in stream end listener")

            client.set_stream_end_listener(on_stream_end)

        # Stream clear listener (sync)
        if self._stream_clear_listeners:

            def on_stream_clear(roles: list[Roles] | None) -> None:
                for listener in self._stream_clear_listeners:
                    try:
                        listener(roles)
                    except Exception:
                        logger.exception("Error in stream clear listener")

            client.set_stream_clear_listener(on_stream_clear)

        # Disconnect listener (sync)
        if self._disconnect_listeners:

            def on_disconnect() -> None:
                for listener in self._disconnect_listeners:
                    try:
                        listener()
                    except Exception:
                        logger.exception("Error in disconnect listener")

            client.set_disconnect_listener(on_disconnect)
