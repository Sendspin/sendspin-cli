"""Tests for the TUI artwork render helper."""

from __future__ import annotations

from PIL import Image as PILImage

from sendspin.tui import artwork


def _dummy_image(size: tuple[int, int] = (16, 16)) -> PILImage.Image:
    return PILImage.new("RGB", size, color=(10, 10, 10))


def test_render_artwork_returns_none_for_none_image() -> None:
    assert artwork.render_artwork(None, generation=1, height_rows=4, width_cells=8) is None


def test_render_artwork_returns_same_renderable_for_same_generation_and_height() -> None:
    artwork.clear_cache()
    img = _dummy_image()
    first = artwork.render_artwork(img, generation=1, height_rows=4, width_cells=8)
    second = artwork.render_artwork(img, generation=1, height_rows=4, width_cells=8)
    assert first is not None
    assert first is second


def test_render_artwork_rebuilds_on_new_generation() -> None:
    artwork.clear_cache()
    img = _dummy_image()
    first = artwork.render_artwork(img, generation=1, height_rows=4, width_cells=8)
    second = artwork.render_artwork(img, generation=2, height_rows=4, width_cells=8)
    assert first is not second


def test_render_artwork_rebuilds_on_new_height() -> None:
    artwork.clear_cache()
    img = _dummy_image()
    first = artwork.render_artwork(img, generation=1, height_rows=4, width_cells=8)
    second = artwork.render_artwork(img, generation=1, height_rows=6, width_cells=8)
    assert first is not second


def test_render_artwork_rebuilds_on_new_width() -> None:
    artwork.clear_cache()
    img = _dummy_image()
    first = artwork.render_artwork(img, generation=1, height_rows=4, width_cells=8)
    second = artwork.render_artwork(img, generation=1, height_rows=4, width_cells=10)
    assert first is not second
