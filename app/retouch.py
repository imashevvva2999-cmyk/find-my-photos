"""Identity-safe portrait / event-photo retouching: the common base of the styled versions (app.styles).

Nothing is generated: a small segmentation model only tells WHERE the skin, hair, clothes
and background are (MediaPipe "selfie multiclass" model, run with LiteRT on the CPU), and
the retouching itself is classic, local photo editing inside those masks - the same moves a
retoucher makes by hand. Face geometry is never touched (no warping, no reshaping, no new
details), and every face is checked afterwards with the face-recognition model: the
processed face must still give (almost) the same face "fingerprint" as the original.

Steps (strength depends on the style, see STYLES):
1. whole photo: mood-preserving white balance, face-aware exposure (app.enhance), then the
   style's tone curve (event grade / soft highlights);
2. each clearly visible face: skin mask (eyes, brows and mouth cut out by the face
   landmarks) -> frequency-separation skin smoothing that keeps the fine skin texture,
   gentler blotches and shine, fill light for faces in shadow, skin-tone cast correction,
   a touch of eye detail;
3. whole photo: sharpening of hair, clothes and edges but not skin; mild noise reduction for
   dark / high-ISO photos;
4. the MDconf2026 watermark (Inter Bold, bottom-left).
"""
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import enhance
from . import faces as face_models
from .config import MODELS_DIR

WORK_SIDE = 4096                        # retouching is done at this size
SEG_PATH = MODELS_DIR / "selfie_multiclass_256x256.tflite"
SEG_SIZE = 256
BG, HAIR, BODY_SKIN, FACE_SKIN, CLOTHES, OTHER = range(6)
MIN_RETOUCH_FACE = 48                   # faces smaller than this (work pixels) are not retouched
IDENTITY_MIN = 0.93                     # a clearly visible face must stay at least this similar
GUARDED_FACE = 120                      # ...if it is at least this big (tiny faces are measured unreliably)
WATERMARK = "MDconf2026"
WATERMARK_FONT = Path(__file__).parent / "static" / "fonts" / "inter-bold-latin.woff2"
WATERMARK_SCALE = 3                     # 3x the size approved in the test gallery (owner's request)


@dataclass(frozen=True)
class Style:
    title: str
    skin: float           # skin treatment (share of the smoothed skin that is blended in)
    texture: float        # how much fine skin texture is put back (1 = all of it)
    shine: float          # how much of the skin highlight shine is removed
    face_light: float     # largest fill-light lift for faces in shadow (0..1 of full scale)
    face_clarity: float   # local contrast that gives the face shape
    eyes: float           # eye detail
    sharpen: float        # sharpening of everything that is not skin
    grade: str            # "natural", "event" or "soft" tone curve
    denoise: float        # luminance noise reduction for dark / high-ISO photos
    skin_tone: float      # share of an unnatural skin cast (green/blue/yellow) that is corrected


STYLES = {   # the approved B&W Editorial and Editorial Film both start from this retouch
    "professional": Style("Professional", skin=0.40, texture=0.8, shine=0.30, face_light=0.06, face_clarity=0.35,
                          eyes=0.45, sharpen=0.65, grade="event", denoise=1.0, skin_tone=0.4),
}


# --------------------------------------------------------------------------- models

_local = threading.local()


def _segmenter():
    if not hasattr(_local, "seg"):
        from ai_edge_litert.interpreter import Interpreter
        it = Interpreter(model_path=str(SEG_PATH), num_threads=1)
        it.allocate_tensors()
        _local.seg = (it, it.get_input_details()[0]["index"], it.get_output_details()[0]["index"])
    return _local.seg


def segment(rgb: np.ndarray) -> np.ndarray:
    """Class probabilities (h, w, 6) for an RGB uint8 crop: background, hair, body skin, face skin, clothes, other."""
    it, inp, out = _segmenter()
    small = cv2.resize(rgb, (SEG_SIZE, SEG_SIZE), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    it.set_tensor(inp, small[None])
    it.invoke()
    probs = it.get_tensor(out)[0]
    h, w = rgb.shape[:2]
    return np.dstack([cv2.resize(np.ascontiguousarray(probs[..., c]), (w, h), interpolation=cv2.INTER_LINEAR)
                      for c in range(probs.shape[-1])])


@dataclass
class FaceInfo:
    det: np.ndarray                 # YuNet row in work-image pixels: box, 5 landmarks, score
    box: tuple = field(init=False)

    def __post_init__(self):
        self.box = tuple(int(round(v)) for v in self.det[:4])

    @property
    def size(self) -> int:
        return max(self.box[2], self.box[3])

    def point(self, i: int) -> np.ndarray:  # 0 right eye, 1 left eye, 2 nose, 3 right mouth, 4 left mouth
        return self.det[4 + 2 * i: 6 + 2 * i]


def detect(rgb: np.ndarray) -> list[FaceInfo]:
    """Faces with their 5 landmarks, in work-image pixels (same detector as the face search)."""
    detector, _ = face_models._models()
    # like the face search: a face that fills the picture is found on a smaller copy
    for max_side in (face_models.MAX_DETECT_SIDE, *face_models.CLOSE_UP_SIDES):
        scale = min(1.0, max_side / max(rgb.shape[:2]))
        small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else rgb
        bgr = cv2.cvtColor(small, cv2.COLOR_RGB2BGR)
        detector.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, dets = detector.detect(bgr)
        if dets is not None:
            break
    out = []
    for d in [] if dets is None else dets:
        d = d.copy()
        d[:14] /= scale
        if min(d[2], d[3]) >= face_models.MIN_FACE_PX / scale:
            out.append(FaceInfo(d))
    return out


def identity_similarity(before: np.ndarray, after: np.ndarray, faces: list[FaceInfo]) -> list[float]:
    """Cosine similarity of each face's recognition fingerprint before and after (1.0 = identical)."""
    _, recognizer = face_models._models()
    b, a = cv2.cvtColor(before, cv2.COLOR_RGB2BGR), cv2.cvtColor(after, cv2.COLOR_RGB2BGR)
    sims = []
    for f in faces:
        e1 = recognizer.feature(recognizer.alignCrop(b, f.det)).flatten()
        e2 = recognizer.feature(recognizer.alignCrop(a, f.det)).flatten()
        sims.append(float(e1 @ e2 / (np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-10)))
    return sims


# --------------------------------------------------------------------------- building blocks

def _guided(I: np.ndarray, r: int, eps: float) -> np.ndarray:
    """Edge-preserving smoothing (guided filter, guide = input): evens out tone, keeps edges."""
    k = (2 * r + 1, 2 * r + 1)
    mean = cv2.boxFilter(I, -1, k)
    var = cv2.boxFilter(I * I, -1, k) - mean * mean
    a = var / (var + eps)
    b = mean - a * mean
    return cv2.boxFilter(a, -1, k) * I + cv2.boxFilter(b, -1, k)


def _ellipse(shape, center, axes, angle=0.0) -> np.ndarray:
    m = np.zeros(shape, np.uint8)
    cv2.ellipse(m, (int(center[0]), int(center[1])), (max(1, int(axes[0])), max(1, int(axes[1]))), angle, 0, 360, 255, -1)
    return m.astype(np.float32) / 255


def _curve(style: Style) -> np.ndarray:
    """Tone curve on lightness (0..255) for the style."""
    x = np.arange(256, dtype=np.float32) / 255
    if style.grade == "event":      # more mid-tone contrast, rolled-off highlights, a hint of shadow lift
        y = x + 0.06 * (x - 0.5) * (1 - np.abs(2 * x - 1)) * 2
        y = np.where(y > 0.78, 0.78 + (y - 0.78) * 0.8, y)
        y = y + 0.012 * (1 - x) ** 3
    elif style.grade == "soft":     # lower contrast, soft highlights, lifted shadows
        y = x - 0.04 * (x - 0.5) * (1 - np.abs(2 * x - 1)) * 2
        y = np.where(y > 0.72, 0.72 + (y - 0.72) * 0.72, y)
        y = y + 0.025 * (1 - x) ** 2
    else:
        y = x
    return np.clip(y * 255, 0, 255).astype(np.float32)


def iso_of(path: Path | None) -> int:
    try:
        return int(Image.open(path).getexif().get_ifd(0x8769).get(0x8827) or 0) if path else 0
    except Exception:
        return 0


# --------------------------------------------------------------------------- the pipeline

@dataclass
class Result:
    image: Image.Image
    faces: int
    retouched_faces: int
    identity: list[float]
    ms: int


def prepare(img: Image.Image) -> tuple[np.ndarray, list[FaceInfo]]:
    """Work-size RGB array and the faces in it (shared by all styles of one photo)."""
    work = img.convert("RGB")
    if max(work.size) > WORK_SIDE:
        work = work.copy()
        work.thumbnail((WORK_SIDE, WORK_SIDE), Image.LANCZOS)
    rgb = np.ascontiguousarray(np.asarray(work))
    return rgb, detect(rgb)


def retouch(rgb: np.ndarray, faces: list[FaceInfo], style: Style, iso: int = 0, watermark: bool = True) -> Result:
    """Retouch with an identity guard: a clearly visible face whose recognition fingerprint moved
    too much is retouched again more gently, and not at all if that is still too much."""
    started = time.perf_counter()
    damp: dict[int, float] = {}
    for _ in range(3):
        result = _retouch(rgb, faces, style, iso, damp, watermark)
        guarded = [f for f in faces if f.size >= MIN_RETOUCH_FACE]
        weak = [id(f) for f, sim in zip(guarded, result.identity) if f.size >= GUARDED_FACE and sim < IDENTITY_MIN]
        if not weak:
            break
        for key in weak:
            damp[key] = 0.0 if damp.get(key, 1.0) < 1.0 else 0.4
    result.ms = round((time.perf_counter() - started) * 1000)
    return result


def _retouch(rgb: np.ndarray, faces: list[FaceInfo], style: Style, iso: int, damp: dict[int, float],
             watermark: bool = True) -> Result:
    started = time.perf_counter()
    H, W = rgb.shape[:2]

    # 1. whole-photo colour and tone (mood-preserving), then the style's curve
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab = enhance._white_balance(lab)
    f = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB).astype(np.float32) / 255
    f = enhance._tone(f, [fc.box for fc in faces])
    base = (f * 255 + 0.5).astype(np.uint8)
    del f                                                            # (memory: not needed any more)
    dark_photo = float(np.median(cv2.cvtColor(base, cv2.COLOR_RGB2GRAY)[::8, ::8])) < 75
    lab = cv2.cvtColor(base, cv2.COLOR_RGB2LAB).astype(np.float32)
    L, A, B = lab[..., 0], lab[..., 1] - 128, lab[..., 2] - 128
    L[:] = cv2.LUT(np.clip(L, 0, 255).astype(np.uint8), _curve(style))
    vib = {"natural": 0.06, "event": 0.10, "soft": 0.05}[style.grade]
    for y in range(0, H, 256):   # per pixel; done in row bands only to keep memory low
        a, b, l = A[y:y + 256], B[y:y + 256], L[y:y + 256]
        chroma = np.hypot(a, b)
        boost = vib * np.clip(1 - chroma / 50, 0, 1) * (1 - enhance._smoothstep(l / 255, 0.75, 0.95))
        skin_like = (a > 5) & (b > 5) & (b < 2.6 * a)
        boost = np.where(skin_like, boost * 0.35, boost)
        a *= 1 + boost
        b *= 1 + boost

    # 2. faces: skin retouching, fill light, skin tone, eyes
    skin_full = np.zeros((H, W), np.float32)
    retouched = 0
    for fc in sorted(faces, key=lambda x: x.size):
        if fc.size < MIN_RETOUCH_FACE:
            continue
        x, y, w, h = fc.box
        side = int(fc.size * 2.6)
        cx, cy = x + w / 2, y + h * 0.62
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        if x1 - x0 < 16 or y1 - y0 < 16:
            continue
        crop_rgb = np.ascontiguousarray(base[y0:y1, x0:x1])
        probs = segment(crop_rgb)
        skin = np.clip(probs[..., FACE_SKIN] + 0.6 * probs[..., BODY_SKIN], 0, 1)
        shape = skin.shape
        # the person around the face (skin + hair): light is added only there, never to the background
        person = cv2.GaussianBlur(np.clip(probs[..., FACE_SKIN] + probs[..., BODY_SKIN] + probs[..., HAIR], 0, 1),
                                  (0, 0), max(1.5, fc.size * 0.03))
        # only the skin that belongs to this face: a generous ellipse around it (not neighbours)
        own = _ellipse(shape, (cx - x0, y + h * 0.55 - y0), (w * 0.95, h * 1.25))
        skin *= cv2.GaussianBlur(own, (0, 0), fc.size * 0.08)
        # cut out eyes, brows and mouth using the landmarks
        re, le, _, rm, lm = (fc.point(i) - (x0, y0) for i in range(5))
        iod = float(np.linalg.norm(le - re)) or w * 0.4
        keep_out = np.zeros(shape, np.float32)
        for eye in (re, le):
            keep_out = np.maximum(keep_out, _ellipse(shape, eye, (iod * 0.26, iod * 0.16)))
            keep_out = np.maximum(keep_out, _ellipse(shape, eye - (0, iod * 0.30), (iod * 0.34, iod * 0.12)))
        mw = float(np.linalg.norm(lm - rm)) or iod * 0.8
        keep_out = np.maximum(keep_out, _ellipse(shape, (rm + lm) / 2, (mw * 0.62, mw * 0.36)))
        skin *= 1 - cv2.GaussianBlur(keep_out, (0, 0), max(1.0, iod * 0.05))
        skin = cv2.GaussianBlur(skin, (0, 0), max(1.0, fc.size * 0.025))   # soft edge: no cut-out look
        soft_skin = cv2.GaussianBlur(skin, (0, 0), max(2.0, fc.size * 0.08))  # for colour changes
        # small faces get less retouching (invisible there, and their features are only a few pixels);
        # profile / unreliable faces (eyes almost on top of each other) get almost none, because the
        # eye and mouth cut-outs cannot be placed reliably; the identity guard can lower it further
        k = float(enhance._smoothstep(np.float32(fc.size), 80, 220))
        k *= float(enhance._smoothstep(np.float32(iod / max(w, 1)), 0.22, 0.36))
        k *= damp.get(id(fc), 1.0)

        Lc, Ac, Bc = L[y0:y1, x0:x1], A[y0:y1, x0:x1], B[y0:y1, x0:x1]
        r = max(2, int(fc.size * 0.045))
        smooth = _guided(Lc, r, 60.0)
        fine = Lc - cv2.GaussianBlur(Lc, (0, 0), max(0.7, fc.size * 0.006))
        target = smooth + style.texture * fine
        m = skin * style.skin * k
        Lc += (target - Lc) * m
        Ac += (_guided(Ac, r, 20.0) - Ac) * m * 0.8          # evens out blotchy redness
        Bc += (_guided(Bc, r, 20.0) - Bc) * m * 0.8

        skin_px = skin > 0.5
        if skin_px.sum() > 50:
            level = float(np.median(Lc[skin_px]))
            # shine: skin highlights well above the face's own level are toned down
            thr = level + 32
            shine = np.clip((Lc - thr) / 30, 0, 1) * skin
            Lc -= style.shine * k * shine * (Lc - thr)
            # fill light for faces in shadow (a soft light over the face and hair around it)
            if level < 125:
                lift = min(style.face_light * 255, (125 - level) * 0.45)
                light = cv2.GaussianBlur(_ellipse(shape, (cx - x0, y + h * 0.5 - y0), (w * 0.9, h * 1.1)), (0, 0), fc.size * 0.15)
                Lc += lift * light * person * (1 - np.clip(Lc / 255, 0, 1)) ** 0.5
            if style.face_clarity:
                light = cv2.GaussianBlur(_ellipse(shape, (cx - x0, y + h * 0.5 - y0), (w * 0.8, h * 0.9)), (0, 0), fc.size * 0.12)
                Lc += style.face_clarity * k * 0.35 * (Lc - cv2.GaussianBlur(Lc, (0, 0), fc.size * 0.15)) * light * skin
            # skin tone: an unnatural (green / blue / too yellow) cast on the skin is partly corrected
            sa, sb = float(np.median(Ac[skin_px])), float(np.median(Bc[skin_px]))
            hue, chroma_s = np.degrees(np.arctan2(sb, sa)), float(np.hypot(sa, sb))
            want = np.clip(hue, 38, 68)
            if abs(want - hue) > 2 or chroma_s < 8:
                cs = max(chroma_s, 10.0)
                ta, tb = cs * np.cos(np.radians(want)), cs * np.sin(np.radians(want))
                Ac += (ta - sa) * style.skin_tone * k * soft_skin
                Bc += (tb - sb) * style.skin_tone * k * soft_skin
        # eyes: a touch of detail, nothing moved or enlarged
        if style.eyes:
            for eye in (re, le):
                em = cv2.GaussianBlur(_ellipse(shape, eye, (iod * 0.24, iod * 0.14)), (0, 0), max(1.0, iod * 0.04))
                Lc += style.eyes * k * (Lc - cv2.GaussianBlur(Lc, (0, 0), max(0.6, iod * 0.012))) * em
        skin_full[y0:y1, x0:x1] = np.maximum(skin_full[y0:y1, x0:x1], skin)
        retouched += 1

    # 3. whole photo: noise (dark / high ISO), then sharpening of everything but skin
    if dark_photo:                                                   # colour noise only
        A[:] = cv2.GaussianBlur(A, (0, 0), 1.2)
        B[:] = cv2.GaussianBlur(B, (0, 0), 1.2)
    if style.denoise and (iso >= 1600 or dark_photo):
        h_nl = style.denoise * (2.0 + (1.0 if iso >= 3200 else 0) + (1.0 if iso >= 6400 else 0))
        L8 = np.clip(L, 0, 255).astype(np.uint8)
        den = cv2.fastNlMeansDenoising(L8, None, h_nl, 5, 13).astype(np.float32)
        L += (den - L) * 0.5                                         # mild: keeps grain, no blotches
    detail = L - cv2.GaussianBlur(L, (0, 0), 1.0 * max(H, W) / 4096 + 0.2)
    no_sharpen = np.maximum(skin_full, cv2.GaussianBlur(skin_full, (0, 0), 6))   # skin and its outline
    L += style.sharpen * detail * (np.abs(detail) > 2.5) * (1 - 0.9 * np.clip(no_sharpen * 1.5, 0, 1))

    out = np.dstack([np.clip(L, 0, 255), np.clip(A + 128, 0, 255), np.clip(B + 128, 0, 255)]).astype(np.uint8)
    out_rgb = cv2.cvtColor(out, cv2.COLOR_LAB2RGB)
    ident = identity_similarity(rgb, out_rgb, [fc for fc in faces if fc.size >= MIN_RETOUCH_FACE])
    image = Image.fromarray(out_rgb)
    return Result(add_watermark(image) if watermark else image, len(faces), retouched, ident,
                  round((time.perf_counter() - started) * 1000))


def add_watermark(img: Image.Image, text: str = WATERMARK) -> Image.Image:
    """White Inter Bold text with a soft shadow, bottom-left, scaled to the photo: WATERMARK_SCALE times the
    size of the approved test (then 2.1 % of the short side); the margin grows from 3 % to 4 % so the
    larger text keeps clear of the edges. Shadow and opacity are unchanged (they scale with the text)."""
    base = img.convert("RGBA")
    short = min(base.size)
    size = WATERMARK_SCALE * max(16, round(short * 0.021))
    pad = round(short * 0.04)
    font = ImageFont.truetype(str(WATERMARK_FONT), size)
    x0, y0, x1, y1 = ImageDraw.Draw(base).textbbox((0, 0), text, font=font)
    x, y = pad - x0, base.height - pad - y1
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((x, y + max(1, size // 20)), text, font=font, fill=(0, 0, 0, 185))
    layer = layer.filter(ImageFilter.GaussianBlur(max(1.5, size * 0.12)))
    ImageDraw.Draw(layer).text((x, y), text, font=font, fill=(255, 255, 255, 225))
    return Image.alpha_composite(base, layer).convert("RGB")
