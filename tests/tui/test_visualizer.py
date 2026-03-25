"""Tests for the TUI visualizer rendering."""

import time
from unittest.mock import patch

from sendspin.tui.visualizer import VisualizerState, loudness_to_colors, render_spectrum


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
