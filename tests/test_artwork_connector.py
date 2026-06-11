"""Tests for ArtworkHandler."""

from __future__ import annotations

import io
import logging

from PIL import Image as PILImage

from sendspin.artwork_connector import ArtworkHandler


class _FakeClient:
    def __init__(self) -> None:
        self.artwork_listeners: list[object] = []
        self.stream_end_listeners: list[object] = []
        self.stream_clear_listeners: list[object] = []

    def add_artwork_listener(self, callback: object) -> object:
        return self._add(self.artwork_listeners, callback)

    def add_stream_end_listener(self, callback: object) -> object:
        return self._add(self.stream_end_listeners, callback)

    def add_stream_clear_listener(self, callback: object) -> object:
        return self._add(self.stream_clear_listeners, callback)

    @staticmethod
    def _add(callbacks: list[object], callback: object) -> object:
        callbacks.append(callback)
        return lambda: callbacks.remove(callback)


def _make_png_bytes(
    size: tuple[int, int] = (32, 32), color: tuple[int, int, int] = (255, 0, 0)
) -> bytes:
    img = PILImage.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_channel_0_bytes_decode_to_pil_image() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_artwork = client.artwork_listeners[0]

    on_artwork(0, _make_png_bytes(size=(48, 48)))

    assert len(received) == 1
    image = received[0]
    assert image is not None
    assert image.size == (48, 48)


def test_empty_payload_emits_none() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_artwork = client.artwork_listeners[0]

    on_artwork(0, b"")

    assert received == [None]


def test_corrupt_bytes_log_and_emit_none(caplog: object) -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_artwork = client.artwork_listeners[0]

    with caplog.at_level(logging.WARNING):  # type: ignore[attr-defined]
        on_artwork(0, b"not a valid png")

    assert received == [None]
    assert any("artwork" in record.message.lower() for record in caplog.records)  # type: ignore[attr-defined]


def test_channels_1_to_3_are_ignored() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_artwork = client.artwork_listeners[0]

    on_artwork(1, _make_png_bytes())
    on_artwork(2, _make_png_bytes())
    on_artwork(3, _make_png_bytes())

    assert received == []


def test_stream_clear_with_artwork_role_emits_none() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_clear = client.stream_clear_listeners[0]

    on_clear(["artwork"])

    assert received == [None]


def test_stream_clear_with_unrelated_role_does_nothing() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_clear = client.stream_clear_listeners[0]

    on_clear(["player"])

    assert received == []


def test_stream_clear_with_none_clears() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_clear = client.stream_clear_listeners[0]

    on_clear(None)

    assert received == [None]


def test_stream_end_with_artwork_role_emits_none() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_end = client.stream_end_listeners[0]

    on_end(["artwork"])

    assert received == [None]


def test_stream_end_with_none_clears() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)
    on_end = client.stream_end_listeners[0]

    on_end(None)

    assert received == [None]


def test_detach_is_silent_and_unsubscribes() -> None:
    received: list[PILImage.Image | None] = []
    handler = ArtworkHandler(on_image=received.append)
    client = _FakeClient()
    handler.attach_client(client)

    handler.detach()

    assert received == []  # detach must not fire callbacks
    assert client.artwork_listeners == []
    assert client.stream_end_listeners == []
    assert client.stream_clear_listeners == []
