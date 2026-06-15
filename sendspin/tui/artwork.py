"""Artwork rendering helpers for the Sendspin TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual_image.renderable import Image as TIImage
from textual_image.renderable import SixelImage, TGPImage

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage
    from rich.console import RenderableType

_cache: tuple[tuple[int, int, int], "RenderableType"] | None = None


def clear_cache() -> None:
    """Drop the cached renderable."""
    global _cache  # noqa: PLW0603
    _cache = None


def render_artwork(
    image: "PILImage | None",
    generation: int,
    height_rows: int,
    width_cells: int,
) -> "RenderableType | None":
    """Return a Rich renderable for the given image, cached by (generation, height_rows, width_cells).

    Returns None when image is None so the layout can collapse the image column.
    """
    global _cache  # noqa: PLW0603
    if image is None:
        return None
    key = (generation, height_rows, width_cells)
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    renderable = TIImage(image, width=width_cells, height=height_rows)
    _cache = (key, renderable)
    return renderable


def detect_support() -> bool:
    """True when a real terminal graphics protocol (Kitty or Sixel) is available.

    textual-image runs its terminal probe at module import. This function just
    inspects the resolved Image class. Halfcell and Unicode fallbacks return False.
    """
    return TIImage is SixelImage or TIImage is TGPImage
