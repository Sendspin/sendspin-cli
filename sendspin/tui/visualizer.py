"""Visualizer rendering for the Sendspin TUI."""

from __future__ import annotations

import time

from rich.text import Text

# Unicode block characters for bar rendering (9 levels including space)
_BLOCKS = " ▁▂▃▄▅▆▇█"
_BLOCK_LEVELS = len(_BLOCKS) - 1  # 8

# Color gradient across frequency bins: green -> cyan -> blue -> magenta
_GRADIENT = ["green", "green", "cyan", "cyan", "blue", "blue", "magenta", "magenta"]


# Interpolation response speed in units per second.
_SMOOTH_RATE_PER_SECOND = 14.0


class VisualizerState:
    """Stores and smooths visualizer frame data for rendering."""

    def __init__(self, *, smoothing_enabled: bool = True) -> None:
        self._spectrum: list[float] = []
        self._spectrum_target: list[float] = []
        self._loudness: float = 0.0
        self._loudness_target: float = 0.0
        self._smoothing_enabled = smoothing_enabled
        self._last_step_monotonic = time.monotonic()

    def update(self, spectrum: list[int] | None, loudness: int | None) -> None:
        """Update with new frame data. Values are uint16 (0-65535)."""
        # Explicit empty frames are used as stream clear/end signals.
        if spectrum is None and loudness is None:
            self.clear()
            return

        if spectrum is not None:
            self._spectrum_target = [v / 65535.0 for v in spectrum]
            if not self._smoothing_enabled:
                self._spectrum = list(self._spectrum_target)
        if loudness is not None:
            self._loudness_target = loudness / 65535.0
            if not self._smoothing_enabled:
                self._loudness = self._loudness_target

    def clear(self) -> None:
        """Clear all state immediately."""
        self._spectrum = []
        self._spectrum_target = []
        self._loudness = 0.0
        self._loudness_target = 0.0
        self._last_step_monotonic = time.monotonic()

    def set_smoothing_enabled(self, enabled: bool) -> None:
        """Enable or disable interpolation."""
        self._smoothing_enabled = enabled
        self._last_step_monotonic = time.monotonic()
        if not enabled:
            self._spectrum = list(self._spectrum_target)
            self._loudness = self._loudness_target

    @property
    def smoothing_enabled(self) -> bool:
        """Whether interpolation is enabled."""
        return self._smoothing_enabled

    def _step(self) -> None:
        """Advance displayed values toward targets."""
        if not self._smoothing_enabled:
            return

        now = time.monotonic()
        dt = max(0.0, now - self._last_step_monotonic)
        self._last_step_monotonic = now
        if dt <= 0.0:
            return

        alpha = min(1.0, dt * _SMOOTH_RATE_PER_SECOND)

        if len(self._spectrum) != len(self._spectrum_target):
            self._spectrum = list(self._spectrum_target)
        else:
            self._spectrum = [
                current + (target - current) * alpha
                for current, target in zip(self._spectrum, self._spectrum_target, strict=True)
            ]

        self._loudness = self._loudness + (self._loudness_target - self._loudness) * alpha

    def get_spectrum(self) -> list[float]:
        """Return the most recent normalized 0.0-1.0 spectrum values."""
        self._step()
        return list(self._spectrum)

    @property
    def loudness(self) -> float:
        """Return current loudness without per-frame decay."""
        self._step()
        return self._loudness


def render_spectrum(magnitudes: list[float], width: int, height: int = 2) -> list[Text]:
    """Render spectrum bars as Rich Text lines.

    Args:
        magnitudes: Normalized 0.0-1.0 values per frequency bin.
        width: Target width in characters.
        height: Number of text rows (each row = 8 block levels).

    Returns:
        List of Text objects, one per row (top to bottom).
    """
    if not magnitudes or width <= 0:
        return [Text(" " * width) for _ in range(height)]

    # Resample to fit width
    n_bins = len(magnitudes)
    bars: list[float] = []
    for i in range(width):
        # Map bar index to source bin range
        start = i * n_bins / width
        end = (i + 1) * n_bins / width
        start_idx = int(start)
        end_idx = min(int(end), n_bins - 1)

        # Average the bins in this range
        total = 0.0
        count = 0
        for j in range(start_idx, end_idx + 1):
            total += magnitudes[j]
            count += 1
        value = total / count if count > 0 else 0.0
        if value > 0.0:
            value = value**0.6
        bars.append(value)

    total_levels = height * _BLOCK_LEVELS
    rows: list[Text] = []

    for row in range(height):
        line = Text()
        # Row 0 = top, row height-1 = bottom
        row_bottom = (height - 1 - row) * _BLOCK_LEVELS

        for bar_idx, value in enumerate(bars):
            level = value * total_levels
            if value > 0.0:
                level = max(level, 1.0)
            fill = level - row_bottom

            if fill >= _BLOCK_LEVELS:
                char = _BLOCKS[_BLOCK_LEVELS]  # Full block
            elif fill <= 0:
                char = " "
            else:
                char = _BLOCKS[int(fill)]

            # Color based on position across frequency range
            gradient_idx = bar_idx * (len(_GRADIENT) - 1) // max(1, len(bars) - 1)
            color = _GRADIENT[min(gradient_idx, len(_GRADIENT) - 1)]
            line.append(char, style=color)

        rows.append(line)

    return rows


def render_peak_arrow(magnitudes: list[float], width: int) -> Text:
    """Render a marker pointing to the strongest frequency bar."""
    if width <= 0:
        return Text()
    line = Text(" " * width, style="dim")
    if not magnitudes:
        return line

    n_bins = len(magnitudes)
    peak_idx = max(range(n_bins), key=lambda i: magnitudes[i])
    col = min(width - 1, int(peak_idx * width / max(1, n_bins)))
    line = Text(" " * col)
    line.append("v", style="bold yellow")
    line.append(" " * max(0, width - col - 1))
    return line


def render_loudness_bar(loudness: float, width: int) -> Text:
    """Render a horizontal loudness bar."""
    if width <= 0:
        return Text()

    clamped = max(0.0, min(1.0, loudness))
    filled = int(clamped * width)
    line = Text()
    line.append("█" * filled, style="bold green")
    line.append("░" * max(0, width - filled), style="dim")
    return line
