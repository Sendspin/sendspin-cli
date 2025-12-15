#!/usr/bin/env python3
"""Generate a calibration tone FLAC (wav fallback) file with repeated logarithmic chirps."""

import numpy as np

# Parameters
SAMPLE_RATE = 44100
CHIRP_INTERVAL = 0.5  # seconds between chirp starts
CHIRP_DURATION = 0.2  # seconds
TOTAL_DURATION = 60  # seconds
F_START = 100  # Hz
F_END = 15000  # Hz
AMPLITUDE = 0.8  # 0-1 range
OUTPUT_FILE = "calibration_tone.flac"


def generate_log_chirp(
    duration: float, f0: float, f1: float, sample_rate: int
) -> np.ndarray:
    """Generate a logarithmic frequency sweep (chirp)."""
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    # Logarithmic chirp formula
    k = (f1 / f0) ** (1 / duration)
    phase = 2 * np.pi * f0 * (k**t - 1) / np.log(k)
    return np.sin(phase)


def main() -> None:
    silence_duration = CHIRP_INTERVAL - CHIRP_DURATION
    num_chirps = int(TOTAL_DURATION / CHIRP_INTERVAL)
    print(f"Generating calibration tone: {num_chirps} chirps (alternating up/down)")
    print(f"  Chirp: {CHIRP_DURATION}s sweep {F_START}Hz <-> {F_END}Hz")
    print(f"  Interval: {CHIRP_INTERVAL}s (chirp every {int(CHIRP_INTERVAL * 1000)}ms)")

    # Generate upward chirp (low to high)
    chirp_up = generate_log_chirp(CHIRP_DURATION, F_START, F_END, SAMPLE_RATE)

    # Generate downward chirp (high to low)
    chirp_down = generate_log_chirp(CHIRP_DURATION, F_END, F_START, SAMPLE_RATE)

    # Apply fade in/out to avoid clicks (20ms each)
    fade_samples = int(0.02 * SAMPLE_RATE)
    fade_in = np.linspace(0, 1, fade_samples)
    fade_out = np.linspace(1, 0, fade_samples)

    chirp_up[:fade_samples] *= fade_in
    chirp_up[-fade_samples:] *= fade_out
    chirp_down[:fade_samples] *= fade_in
    chirp_down[-fade_samples:] *= fade_out

    # Scale amplitude
    chirp_up = chirp_up * AMPLITUDE
    chirp_down = chirp_down * AMPLITUDE

    # Generate silence
    silence = np.zeros(int(silence_duration * SAMPLE_RATE))

    # Build full sequence: alternating up/down chirps with silence
    segments = []
    for i in range(num_chirps):
        chirp = chirp_up if i % 2 == 0 else chirp_down
        segments.append(chirp)
        if i < num_chirps - 1:  # No silence after last chirp
            segments.append(silence)

    full_signal = np.concatenate(segments)

    # Pad to exact TOTAL_DURATION
    target_samples = int(TOTAL_DURATION * SAMPLE_RATE)
    if len(full_signal) < target_samples:
        padding = np.zeros(target_samples - len(full_signal))
        full_signal = np.concatenate([full_signal, padding])

    # Convert to 16-bit PCM
    audio_int16 = (full_signal * 32767).astype(np.int16)

    # Save as FLAC using soundfile
    try:
        import soundfile as sf

        sf.write(OUTPUT_FILE, audio_int16, SAMPLE_RATE, format="FLAC")
        print(f"Saved: {OUTPUT_FILE}")
    except ImportError:
        # Fallback to WAV if soundfile not available
        from scipy.io import wavfile

        output_wav = OUTPUT_FILE.replace(".flac", ".wav")
        wavfile.write(output_wav, SAMPLE_RATE, audio_int16)
        print(f"soundfile not installed, saved as WAV: {output_wav}")
        print("Install soundfile for FLAC: pip install soundfile")

    total_duration = len(full_signal) / SAMPLE_RATE
    print(f"Total duration: {total_duration:.1f}s")


if __name__ == "__main__":
    main()
