"""Tests for the TUI visualizer rendering."""

import time
from unittest.mock import patch

from aiosendspin.models.visualizer import BeatTiming

from sendspin.tui.visualizer import (
    BeatState,
    VisualizerState,
    loudness_to_colors,
    render_beat_strip,
    render_spectrum,
)


# --- loudness_to_colors tests ---


def test_loudness_zero_returns_first_tier() -> None:
    tip, base = loudness_to_colors(0.0)
    assert tip == (0x33, 0x55, 0x88)
    assert base == (0x33 // 4, 0x55 // 4, 0x88 // 4)


def test_loudness_full_returns_last_tier() -> None:
    tip, base = loudness_to_colors(1.0)
    assert tip == (0x99, 0x55, 0x33)
    assert base == (0x99 // 4, 0x55 // 4, 0x33 // 4)


def test_loudness_at_tier_boundary() -> None:
    tip, _base = loudness_to_colors(0.20)
    # At 20% we hit sea green exactly
    assert tip == (0x44, 0x88, 0x66)


def test_loudness_between_tiers_interpolates() -> None:
    tip, _base = loudness_to_colors(0.025)
    # Halfway between steel blue and blue-teal
    assert 0x33 <= tip[0] <= 0x33
    assert 0x55 < tip[1] < 0x66
    assert 0x77 < tip[2] <= 0x88


# --- VisualizerState peak hold tests ---


def test_peaks_snap_to_bar_height() -> None:
    state = VisualizerState()
    state.update([32768, 65535, 16384], loudness=32768)
    state.step()
    spectrum = state.get_spectrum()
    peaks = state.get_peaks()
    assert len(peaks) == len(spectrum)
    assert peaks == spectrum


def test_peaks_hold_when_bars_drop() -> None:
    state = VisualizerState()
    state.update([65535, 65535], loudness=32768)
    state.step()
    _ = state.get_spectrum()
    initial_peaks = state.get_peaks()

    state.update([0, 0], loudness=32768)
    state.step()
    _ = state.get_spectrum()
    peaks_after_drop = state.get_peaks()
    assert peaks_after_drop[0] >= initial_peaks[0] * 0.9


def test_peaks_decay_after_hold() -> None:
    state = VisualizerState()
    state.update([65535, 65535], loudness=32768)
    state.step()
    _ = state.get_spectrum()
    _ = state.get_peaks()

    state.update([0, 0], loudness=32768)

    base = time.monotonic()
    call_count = 0

    def advancing_monotonic() -> float:
        nonlocal call_count
        call_count += 1
        return base + 1.0 + call_count * 0.001

    with patch("sendspin.tui.visualizer.time") as mock_time:
        mock_time.monotonic.side_effect = advancing_monotonic
        state.step()
        peaks = state.get_peaks()
    assert peaks[0] < 0.9


def test_peaks_cleared_on_clear() -> None:
    state = VisualizerState()
    state.update([65535], loudness=32768)
    state.step()
    _ = state.get_spectrum()
    _ = state.get_peaks()
    state.clear()
    assert state.get_peaks() == []


# --- render_spectrum tests ---


def test_render_spectrum_returns_correct_row_count() -> None:
    magnitudes = [0.5] * 10
    peaks = [0.8] * 10
    rows = render_spectrum(magnitudes, width=20, height=8, loudness=0.5, peaks=peaks)
    assert len(rows) == 8


def test_render_spectrum_empty_magnitudes() -> None:
    rows = render_spectrum([], width=20, height=4, loudness=0.5, peaks=[])
    assert len(rows) == 4
    for row in rows:
        assert row.plain.strip() == ""


def test_render_spectrum_peak_marker_character() -> None:
    magnitudes = [0.5]
    peaks = [0.9]
    rows = render_spectrum(magnitudes, width=1, height=8, loudness=0.5, peaks=peaks)
    all_chars = "".join(row.plain for row in rows)
    assert "▔" in all_chars


def test_palette_anchors_override_color_tiers() -> None:
    low = (10, 20, 30)
    high = (200, 100, 50)

    tip_low, base_low = loudness_to_colors(0.0, palette_low=low, palette_high=high)
    assert tip_low == low
    assert base_low == (low[0] // 4, low[1] // 4, low[2] // 4)

    tip_high, _ = loudness_to_colors(1.0, palette_low=low, palette_high=high)
    assert tip_high == high

    tip_mid, _ = loudness_to_colors(0.5, palette_low=low, palette_high=high)
    assert tip_mid == (105, 60, 40)


def test_render_spectrum_bg_color_paints_empty_cells() -> None:
    rows = render_spectrum([0.0], width=2, height=2, loudness=0.0, peaks=[0.0], bg_color="#abcdef")
    for row in rows:
        assert str(row.style) == "on #abcdef"


def test_render_spectrum_freq_peak_color_styles_peak_marker() -> None:
    magnitudes = [0.5]
    peaks = [0.9]
    rows = render_spectrum(
        magnitudes,
        width=1,
        height=8,
        loudness=0.5,
        peaks=peaks,
        freq_peak_color="#ff00ff",
    )
    marker_styles = [
        str(span.style)
        for row in rows
        for span in row.spans
        if row.plain[span.start : span.end] == "▔"
    ]
    assert marker_styles == ["#ff00ff"]


def test_render_spectrum_beat_pulse_brightens_tip() -> None:
    """beat_pulse>0 should brighten the tip color (top row) without going full white."""
    magnitudes = [1.0]
    peaks = [0.0]
    no_pulse = render_spectrum(
        magnitudes, width=1, height=2, loudness=0.5, peaks=peaks, beat_pulse=0.0
    )
    full_pulse = render_spectrum(
        magnitudes, width=1, height=2, loudness=0.5, peaks=peaks, beat_pulse=1.0
    )
    no_top_style = no_pulse[0].spans[0].style if no_pulse[0].spans else ""
    full_top_style = full_pulse[0].spans[0].style if full_pulse[0].spans else ""
    assert no_top_style != full_top_style
    # Cap of 0.5 means peak pulse must not reach pure white (#ffffff).
    assert "#ffffff" not in str(full_top_style)


# --- BeatState tests ---


def test_beat_state_idle_pulse_is_zero() -> None:
    state = BeatState()
    assert state.pulse_intensity() == 0.0
    assert state.is_active is False


def test_beat_state_pulse_decays_to_zero() -> None:
    state = BeatState()
    state.record_beat(BeatTiming(0))
    assert state.pulse_intensity() > 0.5

    with patch("sendspin.tui.visualizer.time") as mock_time:
        # 1 second after — way past decay window
        mock_time.monotonic.return_value = time.monotonic() + 1.0
        assert state.pulse_intensity() == 0.0


def test_beat_state_set_schedule_marks_active() -> None:
    state = BeatState()
    state.set_schedule([BeatTiming(100), BeatTiming(200), BeatTiming(300)])
    assert state.is_active is True
    assert [b.timestamp_us for b in state.upcoming()] == [100, 200, 300]


def test_beat_state_clear_resets() -> None:
    state = BeatState()
    state.record_beat(BeatTiming(0))
    state.set_schedule([BeatTiming(1), BeatTiming(2)])
    state.clear()
    assert state.is_active is False
    assert state.upcoming() == []
    assert state.recent() == []


def test_beat_state_recent_windowed() -> None:
    """Recent beats outside the visible window are pruned."""
    now = [10_000_000_000]
    state = BeatState(now_us=lambda: now[0])
    state.record_beat(BeatTiming(now[0]))
    # Advance clock past the strip window
    now[0] += 10_000_000  # 10s
    state.record_beat(BeatTiming(now[0]))
    assert len(state.recent()) == 1
    assert state.recent()[0].timestamp_us == now[0]


# --- render_beat_strip tests ---


def test_render_beat_strip_playhead_in_center() -> None:
    line = render_beat_strip(width=21, now_us=0, recent=[], upcoming=[], loudness=0.5, pulse=0.0)
    # Idle pulse uses the thin playhead glyph.
    assert line.plain[10] == "│"


def test_render_beat_strip_playhead_grows_on_pulse() -> None:
    mid = render_beat_strip(width=21, now_us=0, recent=[], upcoming=[], loudness=0.5, pulse=0.3)
    peak = render_beat_strip(width=21, now_us=0, recent=[], upcoming=[], loudness=0.5, pulse=1.0)
    assert mid.plain[10] == "┃"
    assert peak.plain[10] == "█"


def test_render_beat_strip_past_and_future_dots() -> None:
    half_s = 4.0
    line = render_beat_strip(
        width=21,
        now_us=0,
        recent=[BeatTiming(-int(half_s * 0.5 * 1_000_000))],
        upcoming=[BeatTiming(int(half_s * 0.5 * 1_000_000))],
        loudness=0.5,
        pulse=0.0,
    )
    # Past beat lands ~25% to the left of center, future beat ~25% right.
    assert line.plain.count("●") == 2
    assert line.plain[5] == "●"
    assert line.plain[15] == "●"


def test_render_beat_strip_downbeat_renders_square() -> None:
    """Downbeats render as a square block (■) instead of a circle (●)."""
    line = render_beat_strip(
        width=21,
        now_us=0,
        recent=[BeatTiming(-2_000_000, is_downbeat=True)],
        upcoming=[BeatTiming(2_000_000, is_downbeat=False)],
        loudness=0.5,
        pulse=0.0,
    )
    assert line.plain.count("■") == 1
    assert line.plain.count("●") == 1
    assert line.plain[5] == "■"
    assert line.plain[15] == "●"


def test_render_beat_strip_downbeat_wins_overlap() -> None:
    """When a regular and a downbeat fall on the same cell, the downbeat keeps it."""
    line = render_beat_strip(
        width=21,
        now_us=0,
        recent=[BeatTiming(-2_000_000, is_downbeat=True)],
        upcoming=[BeatTiming(-2_000_000, is_downbeat=False)],
        loudness=0.5,
        pulse=0.0,
    )
    assert line.plain[5] == "■"
    assert line.plain.count("●") == 0


def test_render_beat_strip_beats_outside_window_dropped() -> None:
    line = render_beat_strip(
        width=21,
        now_us=0,
        recent=[BeatTiming(-100_000_000)],  # 100s in the past
        upcoming=[BeatTiming(100_000_000)],
        loudness=0.5,
        pulse=0.0,
    )
    assert line.plain.count("●") == 0
    assert line.plain.count("■") == 0
