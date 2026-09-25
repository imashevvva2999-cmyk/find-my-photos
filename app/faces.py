"""Face detection and face recognition with OpenCV.

- YuNet finds faces in a photo (returns a box + 5 landmarks per face).
- SFace turns each face into an "embedding": 128 numbers that describe the face.
  Two photos of the same person give embeddings that point in a similar direction,
  so we compare faces with cosine similarity (1.0 = identical, ~0 = unrelated).
"""
import threading
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from . import images
from .config import MODELS_DIR, settings

cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)

DETECTOR_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
RECOGNIZER_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"

MAX_DETECT_SIDE = 2560   # large photos are scaled down to this before detection
MIN_FACE_PX = 24         # ignore faces smaller than this (too blurry to recognise)
CLOSE_UP_SIDES = (480, 320)  # smaller sizes tried when no face is found (close-ups)
DETECT_SCORE = 0.8       # how sure YuNet must be that something is a face

# The match threshold (cosine similarity) is a setting: MATCH_THRESHOLD, default 0.363,
# the value the SFace authors publish for 1:1 verification. It is NOT calibrated for
# searching a whole event, so every result is presented as a "possible match".

ImageError = images.ImageRejected  # kept for older callers


@dataclass
class Face:
    box: tuple[int, int, int, int]  # x, y, width, height in original image pixels
    score: float                    # detection confidence
    embedding: np.ndarray           # 128 float32 values, length normalised to 1

    @property
    def area(self) -> int:
        return self.box[2] * self.box[3]


def load_image(data: bytes) -> Image.Image:
    """Safely decode a selfie (format and size limits applied; see images.py)."""
    return images.decode(data, settings.max_selfie_pixels, target_side=MAX_DETECT_SIDE)


def models_available() -> bool:
    return DETECTOR_PATH.is_file() and RECOGNIZER_PATH.is_file()


# OpenCV models are not safe to share between threads, so each thread gets its own copy.
_local = threading.local()


def _models():
    if not hasattr(_local, "detector"):
        _local.detector = cv2.FaceDetectorYN.create(str(DETECTOR_PATH), "", (320, 320), DETECT_SCORE, 0.3, 5000)
        _local.recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER_PATH), "")
    return _local.detector, _local.recognizer


CONFIDENT_SCORE = 0.9                     # upright, clear faces score above this
STRAIGHTEN_ANGLES = (-90, -60, -30, 30, 60, 90)


def _straighten(bgr: np.ndarray, det: np.ndarray, detector) -> tuple[np.ndarray, np.ndarray]:
    """Re-detect a strongly tilted face (e.g. someone floating in space).

    YuNet is trained on roughly upright faces; on a face tilted ~60 degrees it can still
    fire, but with wrong eye/mouth points, which ruins the embedding. So we cut out the
    area around the face, turn it in 30-degree steps, and keep the version the detector
    is clearly more confident about. Returns (image, detection) to compute the embedding from.
    """
    x, y, w, h = det[:4]
    cx, cy, half = x + w / 2, y + h / 2, max(w, h)
    x0, y0 = int(max(0, cx - half)), int(max(0, cy - half))
    x1, y1 = int(min(bgr.shape[1], cx + half)), int(min(bgr.shape[0], cy + half))
    crop = bgr[y0:y1, x0:x1]
    size = (crop.shape[1], crop.shape[0])
    best_score, best = det[14] + 0.05, (bgr, det)  # must be clearly better than the original
    for angle in STRAIGHTEN_ANGLES:
        turned = cv2.warpAffine(crop, cv2.getRotationMatrix2D((size[0] / 2, size[1] / 2), angle, 1.0), size)
        detector.setInputSize(size)
        _, found = detector.detect(turned)
        for d in [] if found is None else found:
            near_middle = abs(d[0] + d[2] / 2 - size[0] / 2) < size[0] * 0.3 and abs(d[1] + d[3] / 2 - size[1] / 2) < size[1] * 0.3
            if near_middle and d[14] > best_score:
                best_score, best = d[14], (turned, d)
    return best


def find_faces(img: Image.Image) -> list[Face]:
    """Detect every face in the image and compute an embedding for each one."""
    detector, recognizer = _models()
    # YuNet misses a face that fills most of the picture (a close-up selfie), so if nothing
    # is found at the normal size we try again on smaller copies, where the face fits.
    for max_side in (MAX_DETECT_SIDE, *CLOSE_UP_SIDES):
        scale = min(1.0, max_side / max(img.size))
        work = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS) if scale < 1.0 else img
        bgr = cv2.cvtColor(np.asarray(work), cv2.COLOR_RGB2BGR)
        detector.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, detections = detector.detect(bgr)
        if detections is not None:
            break
    else:
        return []

    faces = []
    for det in detections:
        if min(det[2], det[3]) < MIN_FACE_PX:
            continue
        source, landmarks = bgr, det
        if det[14] < CONFIDENT_SCORE:
            source, landmarks = _straighten(bgr, det, detector)
        aligned = recognizer.alignCrop(source, landmarks)  # rotate/crop the face using the eye & mouth landmarks
        emb = recognizer.feature(aligned).flatten().astype(np.float32)
        emb /= np.linalg.norm(emb) + 1e-10
        x, y, w, h = (det[:4] / scale).round().astype(int).tolist()
        faces.append(Face((x, y, w, h), float(det[14]), emb))
    return faces
