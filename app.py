"""Vitals monitor service: rPPG heart rate / respiratory rate from face video.

Replaces the upstream ``app.py``, which could not start: it imported a
nonexistent ``opencv`` package, registered two view functions under the same
name, and unpickled a ``classifier.pkl`` that is not in the repository.
"""
import mimetypes
import os
import shutil
import subprocess
import tempfile
import traceback

from flask import Flask, jsonify, request, send_from_directory

from vitals.explain import ExplainError, explain
from vitals.pipeline import analyze, get_model
from vitals.stroke import score as stroke_score

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
ALLOWED = {".mp4", ".webm", ".avi", ".mov", ".mkv", ".m4v", ".ogg"}

app = Flask(__name__, static_folder="static", static_url_path="")
# Flask's default guess for .woff2 is application/octet-stream. Browsers accept
# that, but the correct type lets them cache and sniff the font properly.
mimetypes.add_type("font/woff2", ".woff2")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def _normalize(src, suffix):
    """Transcode to constant-frame-rate H.264 when ffmpeg is available.

    Browser MediaRecorder output is variable-frame-rate WebM whose reported FPS
    is often wrong or missing; every rate we derive depends on that number, so
    a bad value silently skews the result rather than failing loudly.
    """
    if suffix == ".mp4" or not shutil.which("ffmpeg"):
        return src, None
    dst = src + ".norm.mp4"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-r", "30", "-vsync", "cfr",
         "-c:v", "libx264", "-preset", "ultrafast", "-an", dst],
        capture_output=True, timeout=180,
    )
    if proc.returncode != 0 or not os.path.exists(dst):
        return src, None  # fall back to the original and let OpenCV try
    return dst, dst


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "ffmpeg": bool(shutil.which("ffmpeg"))})


@app.post("/api/warmup")
def warmup():
    """Build the TensorFlow graph before the first real request, so the first
    clinician to upload a clip does not pay the ~10 s model load."""
    try:
        get_model()
        return jsonify({"status": "ready"})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.post("/api/analyze")
def analyze_video():
    upload = request.files.get("video")
    if upload is None or not upload.filename:
        return jsonify({"error": "no video file in request"}), 400

    suffix = os.path.splitext(upload.filename)[1].lower() or ".mp4"
    if suffix not in ALLOWED:
        return jsonify({"error": f"unsupported file type {suffix}"}), 400

    # Optional cuff reference: re-anchors the BP estimate to this patient.
    calibration = None
    try:
        ref_s = request.form.get("cal_systolic", type=float)
        ref_d = request.form.get("cal_diastolic", type=float)
        if ref_s and ref_d:
            if not (70 <= ref_s <= 250 and 40 <= ref_d <= 150 and ref_d < ref_s):
                return jsonify({"error": "calibration values outside a plausible range"}), 400
            calibration = {
                "systolic": ref_s,
                "diastolic": ref_d,
                "hr": request.form.get("cal_hr", type=float) or 70.0,
            }
    except (TypeError, ValueError):
        return jsonify({"error": "calibration values must be numeric"}), 400

    tmpdir = tempfile.mkdtemp(prefix="vitals-")
    raw = os.path.join(tmpdir, "clip" + suffix)
    try:
        upload.save(raw)
        path, _ = _normalize(raw, suffix)
        result = analyze(path, calibration=calibration)
        result["source"] = upload.filename
        return jsonify(result)
    except ValueError as exc:
        # Preprocessing rejected the clip (no face, too short, unreadable).
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": f"inference failed: {exc}"}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _flag(form, name):
    return str(form.get(name, "")).lower() in ("1", "true", "yes", "on")


@app.post("/api/stroke-risk")
def stroke_risk():
    """Score stroke risk, then optionally have the LLM put it into Lao.

    The score is computed here, deterministically. The model is only ever asked
    to describe what was already computed -- see vitals/explain.py.
    """
    form = request.form if request.form else (request.get_json(silent=True) or {})
    get = form.get if hasattr(form, "get") else (lambda k, d=None: None)

    age = get("age")
    try:
        age = float(age) if age not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "age must be numeric"}), 400
    if age is not None and not (0 < age < 130):
        return jsonify({"error": "age outside a plausible range"}), 400

    result = stroke_score(
        age=age,
        female=_flag(form, "female"),
        atrial_fibrillation=_flag(form, "atrial_fibrillation"),
        hypertension=_flag(form, "hypertension"),
        diabetes=_flag(form, "diabetes"),
        chf=_flag(form, "chf"),
        vascular=_flag(form, "vascular"),
        prior_stroke=_flag(form, "prior_stroke"),
        smoking=_flag(form, "smoking"),
    )

    # Measured vitals from the most recent analysis, so the narrative can cover
    # the whole picture rather than the risk factors alone.
    vitals = {}
    for field, cast in (("heart_rate", float), ("respiratory_rate", float),
                        ("spo2", float), ("blood_pressure", str)):
        raw = get(field)
        if raw not in (None, "", "None"):
            try:
                vitals[field] = cast(raw)
            except (TypeError, ValueError):
                pass

    # The explanation is a bonus, never a precondition: if the gateway is down
    # or the model misbehaves, the numeric result still stands on its own.
    if _flag(form, "explain"):
        try:
            result["explanation"] = explain(result, vitals=vitals or None)
        except ExplainError as exc:
            result["explanation_error"] = str(exc)
    return jsonify(result)


@app.get("/api/llm-status")
def llm_status():
    """Whether the FreeLLMAPI gateway is reachable and has a usable model."""
    import urllib.error, urllib.request
    from vitals.explain import GATEWAY
    base = GATEWAY.rsplit("/v1/", 1)[0]
    try:
        with urllib.request.urlopen(base + "/api/ping", timeout=5) as r:
            ok = r.status == 200
        return jsonify({"gateway": base, "reachable": ok,
                        "configured": bool(os.environ.get("LLM_API_KEY"))})
    except (urllib.error.URLError, OSError) as exc:
        return jsonify({"gateway": base, "reachable": False, "error": str(exc)})


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": f"file exceeds {MAX_UPLOAD_MB} MB limit"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
