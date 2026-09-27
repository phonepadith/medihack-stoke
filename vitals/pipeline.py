"""MTTS-CAN inference: heart rate, respiratory rate, and an SpO2 estimate.

The upstream ``predict_video.py`` printed its numbers and blocked on a
matplotlib window; nothing was returned to a caller. This wraps the same math
in a function a web request can use, and fixes the frequency axis, which
upstream hardcoded to 30 fps regardless of the clip's actual frame rate.
"""
import os
import threading

import numpy as np
import scipy.signal

from .bloodpressure import estimate as estimate_bp
from .preprocess import preprocess_video, detrend

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

IMG_SIZE = 36
FRAME_DEPTH = 10
WEIGHTS = os.environ.get("MTTS_CAN_WEIGHTS", "mtts_can.hdf5")

# Physiological pass bands, matching upstream.
PULSE_BAND = (0.75, 2.5)    # 45-150 bpm
RESP_BAND = (0.08, 0.5)     # 4.8-30 breaths/min

_model = None
_model_lock = threading.Lock()


def get_model():
    """Load MTTS-CAN once and reuse it. TensorFlow import is deferred so the
    web process can start and serve the UI before the graph is built."""
    global _model
    with _model_lock:
        if _model is None:
            from .model import MTTS_CAN
            m = MTTS_CAN(FRAME_DEPTH, 32, 64, (IMG_SIZE, IMG_SIZE, 3))
            m.load_weights(WEIGHTS)
            _model = m
    return _model


def _bandpass(signal, fs, band):
    low, high = band
    nyq = fs / 2.0
    # Guard against a low frame rate pushing the upper edge past Nyquist.
    high = min(high, nyq * 0.95)
    if low >= high:
        return signal
    b, a = scipy.signal.butter(1, [low / nyq, high / nyq], btype="bandpass")
    padlen = min(3 * max(len(a), len(b)), len(signal) - 1)
    return scipy.signal.filtfilt(b, a, np.double(signal), padlen=padlen)


def _dominant_rate(signal, fs, band):
    """Peak frequency inside ``band``, in cycles per minute, plus a crude SNR.

    SNR here is the share of in-band spectral power sitting in the peak bin and
    its neighbours. A clean pulse concentrates its energy; a clip of someone
    fidgeting spreads it out. The UI uses it to flag low-confidence readings.
    """
    signal = np.asarray(signal).flatten()
    signal = signal - signal.mean()
    windowed = signal * np.hanning(len(signal))

    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(len(signal), 1.0 / fs)

    mask = (freqs >= band[0]) & (freqs <= band[1])
    if not mask.any():
        return float("nan"), 0.0

    band_power = spectrum[mask] ** 2
    peak_local = int(np.argmax(band_power))
    peak_idx = int(np.flatnonzero(mask)[peak_local])

    total = band_power.sum()
    neighbourhood = band_power[max(0, peak_local - 1):peak_local + 2].sum()
    snr = float(neighbourhood / total) if total > 0 else 0.0

    return float(freqs[peak_idx] * 60.0), snr


def _spo2(rgb_means):
    """Ratio-of-ratios SpO2 heuristic ported from ``oxygensaturation.py``.

    EXPERIMENTAL. The upstream constant (100 - 5R) is uncalibrated -- there is
    no reference oximeter data behind it in this repository -- so this number
    is a signal trend, not a clinical measurement.
    """
    red, blue = rgb_means[:, 0], rgb_means[:, 2]
    mean_r, mean_b = red.mean(), blue.mean()
    if mean_r <= 0 or mean_b <= 0:
        return None, 0.0

    std_r, std_b = red.std(ddof=1), blue.std(ddof=1)
    if std_b <= 0:
        return None, 0.0

    ratio = (std_r / mean_r) / (std_b / mean_b)
    value = 100.0 - 5.0 * ratio
    # A physically meaningless result means the heuristic failed on this clip;
    # report that rather than clamping it into a plausible-looking number.
    if not np.isfinite(value) or value < 70 or value > 100:
        return None, float(ratio)
    return float(value), float(ratio)


def _downsample(signal, points=600):
    signal = np.asarray(signal, dtype=np.float64)
    if len(signal) > points:
        idx = np.linspace(0, len(signal) - 1, points).astype(int)
        signal = signal[idx]
    lo, hi = signal.min(), signal.max()
    if hi - lo < 1e-12:
        return [0.0] * len(signal)
    return np.round((signal - lo) / (hi - lo) * 2 - 1, 4).tolist()


def analyze(video_path, batch_size=100, calibration=None):
    dXsub, fps, rgb_means, n_frames = preprocess_video(video_path, dim=IMG_SIZE)

    # MTTS-CAN's temporal shift needs a whole number of 10-frame groups.
    usable = (dXsub.shape[0] // FRAME_DEPTH) * FRAME_DEPTH
    if usable < FRAME_DEPTH:
        raise ValueError("clip too short after face detection")
    dXsub = dXsub[:usable]

    model = get_model()
    pred = model.predict(
        (dXsub[:, :, :, :3], dXsub[:, :, :, -3:]), batch_size=batch_size, verbose=0
    )

    pulse = _bandpass(detrend(np.cumsum(pred[0]), 100), fps, PULSE_BAND)
    resp = _bandpass(detrend(np.cumsum(pred[1]), 100), fps, RESP_BAND)

    hr, hr_snr = _dominant_rate(pulse, fps, PULSE_BAND)
    rr, rr_snr = _dominant_rate(resp, fps, RESP_BAND)
    spo2, ratio = _spo2(rgb_means)
    bp = estimate_bp(pulse, fps, hr, hr_confidence=hr_snr, calibration=calibration)

    return {
        "heart_rate": round(hr, 1),
        "heart_rate_confidence": round(hr_snr, 3),
        "respiratory_rate": round(rr, 1),
        "respiratory_rate_confidence": round(rr_snr, 3),
        "spo2": round(spo2, 1) if spo2 is not None else None,
        "spo2_ratio": round(ratio, 4),
        "blood_pressure": bp,
        "fps": round(fps, 2),
        "frames_analyzed": int(usable),
        "duration_seconds": round(usable / fps, 1),
        "pulse_waveform": _downsample(pulse),
        "resp_waveform": _downsample(resp),
    }
