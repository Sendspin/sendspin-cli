"""Visualizer rendering for the Sendspin TUI."""

from __future__ import annotations

import time

from rich.text import Text

# Unicode block characters for bar rendering (9 levels including space)
_BLOCKS = " ▁▂▃▄▅▆▇█"
_BLOCK_LEVELS = len(_BLOCKS) - 1  # 8

# Interpolation response speed in units per second.
_SMOOTH_RATE_PER_SECOND = 14.0

# Peak hold configuration
_PEAK_HOLD_SECONDS = 0.5
_PEAK_FALL_RATE = 0.375  # normalized units per second (≈6 rows/sec at 16 rows)

# Loudness-to-color tier stops: (loudness_threshold, (R, G, B))
_COLOR_TIERS: list[tuple[float, tuple[int, int, int]]] = [
    (0.00, (0x33, 0x55, 0x88)),  # steel blue
    (0.05, (0x33, 0x66, 0x88)),  # blue-teal
    (0.10, (0x33, 0x77, 0x77)),  # teal
    (0.20, (0x44, 0x88, 0x66)),  # sea green
    (0.35, (0x66, 0x88, 0x44)),  # olive
    (0.55, (0x88, 0x77, 0x33)),  # amber
    (0.75, (0x99, 0x55, 0x33)),  # warm brown
]


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert RGB to a hex color string for Rich."""
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp_rgb(c0: tuple[int, int, int], c1: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Linearly interpolate between two RGB colors."""
    return (
        int(c0[0] + (c1[0] - c0[0]) * t),
        int(c0[1] + (c1[1] - c0[1]) * t),
        int(c0[2] + (c1[2] - c0[2]) * t),
    )


def loudness_to_colors(
    loudness: float,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Map a 0.0-1.0 loudness value to (tip_rgb, base_rgb).

    Tip color is interpolated between tier stops.
    Base color is the tip at 25% brightness.
    """
    loudness = max(0.0, min(1.0, loudness))

    for i in range(len(_COLOR_TIERS) - 1):
        t0, c0 = _COLOR_TIERS[i]
        t1, c1 = _COLOR_TIERS[i + 1]
        if loudness <= t1:
            t = (loudness - t0) / (t1 - t0) if t1 > t0 else 0.0
            tip = _lerp_rgb(c0, c1, t)
            base = (tip[0] // 4, tip[1] // 4, tip[2] // 4)
            return tip, base

    tip = _COLOR_TIERS[-1][1]
    base = (tip[0] // 4, tip[1] // 4, tip[2] // 4)
    return tip, base


class VisualizerState:
    """Stores and smooths visualizer frame data for rendering."""

    def __init__(self) -> None:
        self._spectrum: list[float] = []
        self._spectrum_target: list[float] = []
        self._loudness: float = 0.0
        self._loudness_target: float = 0.0
        self._peaks: list[float] = []
        self._peak_hold_timers: list[float] = []
        self._last_step_monotonic = time.monotonic()

    def update(self, spectrum: list[int] | None, loudness: int | None) -> None:
        """Update with new frame data. Values are uint16 (0-65535)."""
        if spectrum is None and loudness is None:
            self.clear()
            return

        if spectrum is not None:
            self._spectrum_target = [v / 65535.0 for v in spectrum]
        if loudness is not None:
            self._loudness_target = loudness / 65535.0

    def clear(self) -> None:
        """Clear all state immediately."""
        self._spectrum = []
        self._spectrum_target = []
        self._loudness = 0.0
        self._loudness_target = 0.0
        self._peaks = []
        self._peak_hold_timers = []
        self._last_step_monotonic = time.monotonic()

    def _step(self) -> None:
        """Advance displayed values toward targets."""
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

        # Update peaks
        if len(self._peaks) != len(self._spectrum):
            self._peaks = list(self._spectrum)
            self._peak_hold_timers = [_PEAK_HOLD_SECONDS] * len(self._spectrum)
        else:
            for i, value in enumerate(self._spectrum):
                if value >= self._peaks[i]:
                    self._peaks[i] = value
                    self._peak_hold_timers[i] = _PEAK_HOLD_SECONDS
                elif self._peak_hold_timers[i] > 0:
                    remaining = self._peak_hold_timers[i]
                    self._peak_hold_timers[i] -= dt
                    if self._peak_hold_timers[i] <= 0:
                        decay_dt = dt - remaining
                        self._peaks[i] = max(0.0, self._peaks[i] - _PEAK_FALL_RATE * decay_dt)
                else:
                    self._peaks[i] = max(0.0, self._peaks[i] - _PEAK_FALL_RATE * dt)

    @property
    def is_active(self) -> bool:
        """Whether there is pending visualizer data to animate."""
        return bool(self._spectrum_target)

    def step(self) -> None:
        """Advance displayed values toward targets.

        Call once per render frame before reading spectrum/loudness/peaks.
        """
        self._step()

    def get_spectrum(self) -> list[float]:
        """Return the most recent normalized 0.0-1.0 spectrum values."""
        return list(self._spectrum)

    @property
    def loudness(self) -> float:
        """Return current loudness without per-frame decay."""
        return self._loudness

    def get_peaks(self) -> list[float]:
        """Return the current peak hold heights (0.0-1.0 per bin)."""
        return list(self._peaks)


def render_spectrum(
    magnitudes: list[float],
    width: int,
    height: int,
    loudness: float,
    peaks: list[float],
) -> list[Text]:
    """Render spectrum bars as Rich Text lines with loudness-driven color.

    Args:
        magnitudes: Normalized 0.0-1.0 values per frequency bin.
        width: Target width in characters.
        height: Number of text rows (each row = 8 block levels).
        loudness: Normalized 0.0-1.0 loudness for color selection.
        peaks: Normalized 0.0-1.0 peak hold heights per bin.

    Returns:
        List of Text objects, one per row (top to bottom).
    """
    if not magnitudes or width <= 0 or height <= 0:
        return [Text(" " * max(0, width)) for _ in range(max(0, height))]

    tip, base = loudness_to_colors(loudness)

    # Find frequency peak bin (for highlight color on its peak marker)
    freq_peak_bin = max(range(len(magnitudes)), key=lambda i: magnitudes[i])

    # Resample magnitudes and peaks to fit width
    n_bins = len(magnitudes)
    bars: list[float] = []
    bar_peaks: list[float] = []
    bar_is_freq_peak: list[bool] = []
    for i in range(width):
        start = i * n_bins / width
        end = (i + 1) * n_bins / width
        start_idx = int(start)
        end_idx = min(int(end), n_bins - 1)

        total = 0.0
        peak_max = 0.0
        is_freq_peak = False
        count = 0
        for j in range(start_idx, end_idx + 1):
            total += magnitudes[j]
            if peaks and j < len(peaks):
                peak_max = max(peak_max, peaks[j])
            if j == freq_peak_bin:
                is_freq_peak = True
            count += 1
        value = total / count if count > 0 else 0.0
        if value > 0.0:
            value = value**0.6
        bars.append(value)
        bar_peaks.append(peak_max**0.6 if peak_max > 0 else 0.0)
        bar_is_freq_peak.append(is_freq_peak)

    total_levels = height * _BLOCK_LEVELS
    rows: list[Text] = []

    # Pre-compute row colors (vertical gradient from base to tip)
    # Square root curve so bars brighten quickly from the base
    row_colors: list[str] = []
    for row in range(height):
        # Row 0 = top (brightest), row height-1 = bottom (darkest)
        linear_t = 1.0 - row / max(1, height - 1) if height > 1 else 1.0
        t = linear_t**0.5  # sqrt curve: more of the bar sits in brighter range
        rgb = _lerp_rgb(base, tip, t)
        row_colors.append(_rgb_to_hex(*rgb))

    # Peak marker colors
    peak_color = _rgb_to_hex(
        min(255, int(tip[0] * 1.4)),
        min(255, int(tip[1] * 1.4)),
        min(255, int(tip[2] * 1.4)),
    )
    freq_peak_color = "#ffffff"

    for row_idx in range(height):
        line = Text()
        row_bottom = (height - 1 - row_idx) * _BLOCK_LEVELS
        color = row_colors[row_idx]

        for bar_idx, value in enumerate(bars):
            level = value * total_levels
            if value > 0.0:
                level = max(level, 1.0)
            fill = level - row_bottom

            # Check for peak marker at this position
            peak_level = bar_peaks[bar_idx] * total_levels
            if peak_level > 0.0:
                peak_level = max(peak_level, 1.0)
            peak_row_pos = peak_level - row_bottom

            if 0 < peak_row_pos <= _BLOCK_LEVELS and fill < peak_row_pos:
                pc = freq_peak_color if bar_is_freq_peak[bar_idx] else peak_color
                line.append("▔", style=pc)
            elif fill >= _BLOCK_LEVELS:
                line.append(_BLOCKS[_BLOCK_LEVELS], style=color)
            elif fill <= 0:
                line.append(" ")
            else:
                line.append(_BLOCKS[int(fill)], style=color)

        rows.append(line)

    return rows
