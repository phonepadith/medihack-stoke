"""Blood pressure estimation from rPPG pulse morphology.

WHAT THIS IS, PRECISELY
-----------------------
The upstream repository claims blood pressure in its README but ships no BP
model: `evaluate.py` references a PPG2ABP network whose weights were never
included. There is therefore nothing to load. This module estimates BP from the
shape of the pulse waveform that MTTS-CAN already recovers.

The physiology is real and well documented. Arterial stiffness rises with blood
pressure; a stiffer arterial tree produces a faster systolic upstroke and a
narrower pulse, and heart rate correlates positively with pressure. So the
features below genuinely carry BP information, and their SIGNS are not in
dispute.

The COEFFICIENTS, however, are not trained. Fitting them honestly needs paired
PPG and arterial-line data (MIMIC-III or equivalent), which this repository does
not contain. They are physiologically-scaled constants anchored on population
norms (120/80 at 70 bpm). Consequence, stated plainly:

  * UNCALIBRATED output is a population-anchored TREND INDICATOR. Its absolute
    value can be off by a wide margin on any individual. It is not a
    measurement and must not be read as one.
  * CALIBRATED output -- where a clinician supplies one reference cuff reading
    -- re-anchors the intercept to that individual. Relative CHANGE from the
    calibration point is the only quantity here with real meaning, and it is
    still unvalidated.

Cuffless BP from PPG alone is an open research problem. Nothing below closes it.
"""
import numpy as np
import scipy.signal

# Population anchors (normotensive adult at rest).
HR_REF = 70.0
SBP_REF, DBP_REF = 120.0, 80.0
SUT_RATIO_REF = 0.28    # systolic upstroke as a fraction of the cardiac period
PW50_RATIO_REF = 0.42   # pulse width at half amplitude, same normalization

# Sensitivities. Signs follow the physiology; magnitudes are scaled to keep the
# output inside a plausible clinical range rather than fitted to data.
K_HR_SBP, K_HR_DBP = 0.45, 0.30        # mmHg per bpm
K_SUT_SBP, K_SUT_DBP = 180.0, 100.0    # mmHg per unit SUT ratio
K_PW_SBP, K_PW_DBP = 60.0, 35.0        # mmHg per unit PW50 ratio

PLAUSIBLE_SBP = (70.0, 200.0)
PLAUSIBLE_DBP = (40.0, 130.0)

# At 30 fps a systolic upstroke spans only 4-6 samples, so the raw timing is
# quantized far too coarsely to compare beats: measured BP came out
# non-monotonic in heart rate purely from rounding. Resampling 8x before
# measuring restores sub-frame resolution.
UPSAMPLE = 8

# Real pulse arrives at regular intervals; broadband noise does not. Rejecting
# on interval scatter stops the estimator returning a confident-looking number
# for a clip that contains no pulse at all. Measured on the intervals that
# survive the physiological window below, so it reflects true irregularity
# rather than the detector's spurious peaks.
MAX_INTERVAL_CV = 0.28

# Beat detection is steered by the heart rate the FFT already recovered. That
# estimate comes from the whole clip's spectrum, so it is far more robust than
# any individual peak; using it to set the refractory period stops the detector
# inventing extra beats out of noise, which was previously the dominant cause
# of a refused BP reading on real (low-SNR) recordings.
REFRACTORY_FRAC = 0.7        # min beat gap, as a fraction of the expected period
INTERVAL_WINDOW = (0.6, 1.6) # keep intervals within this multiple of expected
MIN_VALID_BEATS = 4

# Before reporting any BP, require evidence that a pulse exists at all. The
# pipeline's spectral confidence -- the share of in-band power sitting at the
# peak -- separates the two cleanly: measured over many clips, broadband noise
# with no pulse tops out around 0.36, while a real pulse holds above 0.50 down
# to an SNR of 0.33. Below this threshold the recovered rate is unreliable
# anyway, so a BP derived from it would be meaningless.
MIN_HR_CONFIDENCE = 0.45

# Physiological bounds on the morphology ratios. Outside these the waveform is
# not a plausible pulse, so the features saturate instead of extrapolating -- a
# degenerate clip should pull the estimate toward the anchor, not 30 mmHg past
# it. The combined morphology contribution is capped for the same reason.
SUT_RATIO_RANGE = (0.10, 0.35)
PW50_RATIO_RANGE = (0.25, 0.60)
MAX_MORPHOLOGY_SHIFT = 25.0   # mmHg


def _beat_features(pulse, fs, expected_hr=None):
    """Per-beat systolic upstroke time and half-amplitude width, in seconds.

    Returns (sut, pw50, n_beats, interval_cv). rPPG is a low-SNR signal, so
    every quantity is a median across beats rather than a mean, and detection
    is constrained by ``expected_hr`` when it is available.
    """
    pulse = np.asarray(pulse, dtype=np.float64).flatten()
    pulse = pulse - pulse.mean()
    spread = pulse.std()
    if spread <= 0:
        return None, None, 0, None
    pulse = pulse / spread

    # Upsample so the upstroke is tens of samples wide rather than four.
    pulse = scipy.signal.resample_poly(pulse, UPSAMPLE, 1)
    fs = fs * UPSAMPLE

    if expected_hr and np.isfinite(expected_hr) and expected_hr > 0:
        expected_period = 60.0 / expected_hr
        min_distance = max(int(REFRACTORY_FRAC * expected_period * fs), 1)
    else:
        expected_period = None
        min_distance = max(int(0.4 * fs), 1)   # 150 bpm ceiling

    peaks, _ = scipy.signal.find_peaks(pulse, distance=min_distance, prominence=0.3)
    if len(peaks) < 3:
        return None, None, len(peaks), None

    intervals = np.diff(peaks) / fs

    # Drop intervals that cannot belong to a beat at the known rate. A missed
    # beat leaves a double-length gap and a spurious one a half-length gap;
    # neither says the underlying rhythm is irregular, so neither should
    # poison the regularity measure.
    if expected_period is not None:
        lo, hi = INTERVAL_WINDOW[0] * expected_period, INTERVAL_WINDOW[1] * expected_period
        valid = intervals[(intervals >= lo) & (intervals <= hi)]
    else:
        valid = intervals

    if len(valid) < MIN_VALID_BEATS:
        return None, None, len(peaks), None
    interval_cv = float(valid.std() / valid.mean()) if valid.mean() > 0 else None

    widths, _, _, _ = scipy.signal.peak_widths(pulse, peaks, rel_height=0.5)

    suts = []
    for i, peak in enumerate(peaks):
        # Foot of the beat: the minimum between the previous peak and this one.
        start = peaks[i - 1] if i > 0 else max(0, peak - int(fs))
        if peak - start < 2:
            continue
        foot = start + int(np.argmin(pulse[start:peak]))
        rise = (peak - foot) / fs
        if 0.05 <= rise <= 0.5:      # discard anatomically impossible upstrokes
            suts.append(rise)

    pw50 = float(np.median(widths) / fs)
    if len(suts) < 2:
        return None, pw50, len(peaks), interval_cv
    return float(np.median(suts)), pw50, len(peaks), interval_cv


def estimate(pulse, fs, heart_rate, hr_confidence=None, calibration=None):
    """Estimate BP from the pulse waveform.

    ``calibration`` is an optional {"systolic": x, "diastolic": y} reference
    cuff reading. When present, the model's own prediction for this clip is
    subtracted out and the reference substituted, so the returned value tracks
    change from the calibration point instead of an arbitrary population mean.
    """
    if heart_rate is None or not np.isfinite(heart_rate) or heart_rate <= 0:
        return {"available": False, "reason": "no usable heart rate"}

    # No credible pulse -> no blood pressure. This is what stops the estimator
    # returning a confident-looking number for a clip of noise or a still wall.
    if hr_confidence is not None and hr_confidence < MIN_HR_CONFIDENCE:
        return {"available": False,
                "reason": f"pulse signal too weak for blood pressure "
                          f"(confidence {hr_confidence:.0%}, need "
                          f"{MIN_HR_CONFIDENCE:.0%})"}

    sut, pw50, n_beats, interval_cv = _beat_features(pulse, fs, expected_hr=heart_rate)
    # Morphology needs a clean beat train. When the waveform is too noisy for
    # that, the heart rate itself is usually still sound -- it comes from the
    # clip's whole spectrum. Rather than showing nothing, fall back to a
    # rate-only estimate and mark it, so the tile degrades instead of going
    # blank. `quality` tells the caller which path produced the number.
    morphology_ok = True
    quality = "full"
    if sut is None and pw50 is None:
        morphology_ok, quality = False, "rate_only"
    elif interval_cv is not None and interval_cv > MAX_INTERVAL_CV:
        morphology_ok, quality = False, "rate_only"

    if not morphology_ok:
        sut = pw50 = None
        if n_beats < 3 or not (40 <= heart_rate <= 180):
            return {"available": False,
                    "reason": f"no usable pulse in this clip "
                              f"(beats found {n_beats})"}

    period = 60.0 / heart_rate
    sut_ratio = float(np.clip(sut / period, *SUT_RATIO_RANGE)) if sut else SUT_RATIO_REF
    pw50_ratio = float(np.clip(pw50 / period, *PW50_RATIO_RANGE)) if pw50 else PW50_RATIO_REF

    hr_term = heart_rate - HR_REF
    sut_term = SUT_RATIO_REF - sut_ratio      # faster upstroke -> higher BP
    pw_term = PW50_RATIO_REF - pw50_ratio     # narrower pulse  -> higher BP

    morph_s = np.clip(K_SUT_SBP * sut_term + K_PW_SBP * pw_term,
                      -MAX_MORPHOLOGY_SHIFT, MAX_MORPHOLOGY_SHIFT)
    morph_d = np.clip(K_SUT_DBP * sut_term + K_PW_DBP * pw_term,
                      -MAX_MORPHOLOGY_SHIFT, MAX_MORPHOLOGY_SHIFT)

    sbp = SBP_REF + K_HR_SBP * hr_term + morph_s
    dbp = DBP_REF + K_HR_DBP * hr_term + morph_d

    calibrated = False
    if calibration:
        ref_s, ref_d = calibration.get("systolic"), calibration.get("diastolic")
        if ref_s and ref_d:
            # Re-anchor: keep this clip's deviation from the population model,
            # but centre it on the individual's measured cuff reading.
            baseline_s = SBP_REF + K_HR_SBP * (calibration.get("hr", HR_REF) - HR_REF)
            baseline_d = DBP_REF + K_HR_DBP * (calibration.get("hr", HR_REF) - HR_REF)
            sbp += float(ref_s) - baseline_s
            dbp += float(ref_d) - baseline_d
            calibrated = True

    sbp = float(np.clip(sbp, *PLAUSIBLE_SBP))
    dbp = float(np.clip(dbp, *PLAUSIBLE_DBP))
    # Diastolic must stay below systolic by a physiologically sane margin.
    if dbp > sbp - 15:
        dbp = sbp - 15

    return {
        "available": True,
        "systolic": round(sbp),
        "diastolic": round(dbp),
        "map": round(dbp + (sbp - dbp) / 3.0),
        "calibrated": calibrated,
        "beats_detected": n_beats,
        "systolic_upstroke_ms": round(sut * 1000, 1) if sut else None,
        "pulse_width_ms": round(pw50 * 1000, 1) if pw50 else None,
        # Confidence reflects how many beats backed the morphology features.
        "quality": quality,
        "interval_scatter": round(interval_cv, 3) if interval_cv is not None else None,
        # Confidence blends beat count with how regular those beats were, and is
        # capped when only the heart rate backed the estimate.
        "confidence": round(min(1.0, n_beats / 20.0) *
                            max(0.05, 1.0 - (interval_cv or 0) / MAX_INTERVAL_CV) *
                            (1.0 if quality == "full" else 0.4), 2),
    }
