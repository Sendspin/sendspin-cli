"""Tests for the TUI artwork render helper."""

from __future__ import annotations

from unittest.mock import patch

from PIL import Image as PILImage

from sendspin.tui import artwork


def _dummy_image(size: tuple[int, int] = (16, 16)) -> PILImage.Image:
    return PILImage.new("RGB", size, color=(10, 10, 10))


def test_render_artwork_returns_none_for_none_image() -> None:
    assert artwork.render_artwork(None, generation=1, height_rows=4) is None


def test_render_artwork_returns_same_renderable_for_same_generation_and_height() -> None:
    artwork.clear_cache()
    img = _dummy_image()
    first = artwork.render_artwork(img, generation=1, height_rows=4)
    second = artwork.render_artwork(img, generation=1, height_rows=4)
    assert first is not None
    assert first is second


def test_render_artwork_rebuilds_on_new_generation() -> None:
    artwork.clear_cache()
    img = _dummy_image()
    first = artwork.render_artwork(img, generation=1, height_rows=4)
    second = artwork.render_artwork(img, generation=2, height_rows=4)
    assert first is not second


def test_render_artwork_rebuilds_on_new_height() -> None:
    artwork.clear_cache()
    img = _dummy_image()
    first = artwork.render_artwork(img, generation=1, height_rows=4)
    second = artwork.render_artwork(img, generation=1, height_rows=6)
    assert first is not second


def test_detect_support_false_when_no_graphics_protocol() -> None:
    with patch.object(artwork, "_probe_graphics_protocol", return_value=None):
        assert artwork.detect_support() is False


def test_detect_support_true_when_kitty_or_sixel() -> None:
    with patch.object(artwork, "_probe_graphics_protocol", return_value="kitty"):
        assert artwork.detect_support() is True
    with patch.object(artwork, "_probe_graphics_protocol", return_value="sixel"):
        assert artwork.detect_support() is True
