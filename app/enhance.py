"""Colour and exposure helpers used by the retouching (app.retouch).

Plain image processing, no AI. White balance reduces only a SMALL colour cast measured on neutral
areas (strong stage / sunset colour is kept as intentional); exposure lifts the mid-tones a little
when the people (detected faces) are in shadow, while pure black and white stay where they are.
"""

import cv2
import numpy as np

LUMA = np.array([0.299, 0.587, 0.114], np.float32)


def _smoothstep(x, lo, hi):
    t = np.clip((x - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


def _sample(arr: np.ndarray) -> np.ndarray:
    step = max(1, int(np.sqrt(arr.shape[0] * arr.shape[1] / 250_000)))  # ~250k pixels for statistics
    return arr[::step, ::step]


def _white_balance(lab: np.ndarray) -> np.ndarray:
    """Shift a/b by part of the cast seen on neutral areas; leave strong (intentional) colour alone."""
    s = _sample(lab)
    L, a, b = s[..., 0] / 255, s[..., 1] - 128, s[..., 2] - 128
    overall_chroma = float(np.hypot(a, b).mean())
    neutral = (np.hypot(a, b) < 14) & (L > 0.2) & (L < 0.92)
    if neutral.mean() < 0.05 or overall_chroma > 20:
        return lab                                  # no reliable neutrals, or a colourful scene
    ca, cb = float(a[neutral].mean()), float(b[neutral].mean())
    cast = float(np.hypot(ca, cb))
    if cast < 1.5 or cast > 7:
        return lab                                  # already neutral, or strong mood light
    strength = 0.3 if cb > 0 else 0.6               # warm casts are kept more
    out = lab.copy()
    out[..., 1] = np.clip(lab[..., 1] - ca * strength, 0, 255)
    out[..., 2] = np.clip(lab[..., 2] - cb * strength, 0, 255)
    return out


def _tone(f: np.ndarray, face_boxes: list) -> np.ndarray:
    """Limited levels stretch and a mid-tone lift aimed at the faces (0 and 1 stay fixed)."""
    lum = _sample(f) @ LUMA
    lo, hi = np.percentile(lum, (0.5, 99.8))
    lo, hi = min(float(lo), 0.04), max(float(hi), 0.92)
    f = np.clip((f - lo) / (hi - lo), 0, 1)
    lum_full = f @ LUMA
    face_levels = []
    for x, y, w, h in face_boxes:
        crop = lum_full[y + h // 5: y + h - h // 5, x + w // 5: x + w - w // 5]
        if crop.size:
            face_levels.append(float(np.median(crop)))
    if face_levels:
        level, target, lift_max = float(np.median(face_levels)), 0.46, 0.84
    else:
        level = float(np.median(_sample(lum_full)))
        low_key = float((_sample(lum_full) < 0.1).mean()) > 0.45
        target, lift_max = (0.30, 0.94) if low_key else (0.40, 0.88)
    gamma = 1.0
    if level < target - 0.03:
        gamma = max(lift_max, np.log(target) / np.log(max(level, 1e-3)))
    elif level > 0.72:
        gamma = min(1.08, np.log(0.66) / np.log(level))
    return f ** gamma
