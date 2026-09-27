"""Video preprocessing for MTTS-CAN inference.

Derived from the upstream ``upload_video/inference_preprocess.py``, with the
server-hostile parts removed: no ``plt.show()`` mid-pipeline, a face box that
survives frames where the cascade misses, and a sparse detrend so memory does
not grow with the square of the clip length.
"""
import cv2
import numpy as np
from scipy.sparse import spdiags, eye
from scipy.sparse.linalg import spsolve

# Detect a face every N frames and reuse the box in between. The cascade is the
# slow part of preprocessing and a face does not move much in 1/6th of a second.
DETECT_EVERY = 5
WORK_HEIGHT = 300


def _largest_face(cascade, gray):
    faces = cascade.detectMultiScale(gray, 1.3, 5)
    if len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def preprocess_video(path, dim=36, max_seconds=30):
    """Return (dXsub, fps, rgb_means, n_frames_used).

    ``dXsub`` is the (N-1, dim, dim, 6) stack MTTS-CAN expects: normalized
    frame differences in the motion branch, standardized frames in the
    appearance branch. ``rgb_means`` is the per-frame mean R/G/B of the face
    crop at full resolution, which the SpO2 heuristic consumes.
    """
    vid = cv2.VideoCapture(path)
    if not vid.isOpened():
        raise ValueError("could not open video (unsupported container or codec)")

    fps = vid.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps <= 1:  # 0, NaN, or nonsense
        fps = 30.0
    max_frames = int(fps * max_seconds)

    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )

    frames, rgb_means = [], []
    box = None
    detected_any = False
    i = 0

    while i < max_frames:
        ok, img = vid.read()
        if not ok:
            break

        h, w = img.shape[:2]
        scale = WORK_HEIGHT / float(h)
        img = cv2.resize(img, (int(w * scale), WORK_HEIGHT), interpolation=cv2.INTER_AREA)

        if i % DETECT_EVERY == 0 or box is None:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            found = _largest_face(cascade, gray)
            if found is not None:
                box = found
                detected_any = True

        if box is None:
            i += 1
            continue  # no face seen yet; skip until one appears

        x, y, bw, bh = box
        roi = img[y:y + bh, x:x + bw]
        if roi.size == 0:
            i += 1
            continue

        roi_rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
        # Full-resolution channel means, vectorized. Upstream looped over every
        # pixel in Python, which took minutes per clip.
        rgb_means.append(roi_rgb.reshape(-1, 3).mean(axis=0))

        small = cv2.resize(roi_rgb, (dim, dim), interpolation=cv2.INTER_AREA)
        frames.append(small.astype(np.float32) / 255.0)
        i += 1

    vid.release()

    if not detected_any:
        raise ValueError("no face detected in the video")
    if len(frames) < 30:
        raise ValueError(
            f"only {len(frames)} usable frames with a visible face; need at least 30"
        )

    Xsub = np.asarray(frames, dtype=np.float32)
    rgb_means = np.asarray(rgb_means, dtype=np.float64)

    # Motion branch: normalized successive differences.
    diff = Xsub[1:] - Xsub[:-1]
    total = Xsub[1:] + Xsub[:-1]
    dXsub = np.divide(diff, total, out=np.zeros_like(diff), where=total > 1e-7)
    std = np.std(dXsub)
    if std > 0:
        dXsub /= std

    # Appearance branch: standardized frames, aligned to the difference stack.
    Xsub = (Xsub - np.mean(Xsub)) / (np.std(Xsub) + 1e-8)
    Xsub = Xsub[:-1]

    return np.concatenate((dXsub, Xsub), axis=3), float(fps), rgb_means, len(frames)


def detrend(signal, Lambda):
    """Smoothness-priors detrending (Tarvainen et al., 2002).

    Upstream materialized an N x N identity and inverted it, which is O(N^2)
    memory and O(N^3) time; a 20 s clip at 30 fps would allocate gigabytes.
    This solves the same system sparsely.
    """
    n = signal.shape[0]
    ones = np.ones(n)
    D = spdiags(
        np.array([ones, -2 * ones, ones]), np.array([0, 1, 2]), n - 2, n
    ).tocsc()
    A = (eye(n, format="csc") + (Lambda ** 2) * (D.T @ D)).tocsc()
    return signal - spsolve(A, signal)
