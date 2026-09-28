"""The two styled versions of every event photo: B&W Editorial and Editorial Film.

Both start from the same identity-safe "professional" retouch of the photo (app.retouch) and then
change only tone, colour and grain - nothing is generated, no face or body is reshaped. Both carry
the MDconf2026 watermark. The original / display copies are never touched; styled files live in

    data/events/<event_id>/processed/<style>/full/<photo_id>.jpg      download (max 4096 px)
    data/events/<event_id>/processed/<style>/preview/<photo_id>.jpg   shown in the viewer (2048 px)

The settings below are exactly the ones approved in the local comparison gallery. STYLE_VERSION is
stored per photo (photos.styles_version); bump it only when the output is meant to change, and the
worker then renders every photo again.

Editorial Film's colour table (luts/editorial_film.cube) was generated offline with Spectral Film
LUT (MIT, https://github.com/JanLohse/spectral_film_lut) from its Kodak Portra 160 / Endura Premier
data; stock names are only the model's parameters. LUT interpolation and grain are Film Lab's (MIT,
app/filmlab.py).
"""
import threading
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import filmlab, retouch

STYLE_VERSION = 1
STYLES = ("bw_editorial", "editorial_film")
TITLES = {"original": "Оригинал", "bw_editorial": "B&W Editorial", "editorial_film": "Editorial Film"}
FULL_SIZE = 4096
PREVIEW_SIZE = 2048
CUBE_PATH = Path(__file__).parent / "luts" / "editorial_film.cube"


def render(img: Image.Image, source: Path | None, photo_id: int) -> dict[str, Image.Image]:
    """Both styled versions (watermarked, at most FULL_SIZE px) of one decoded photo.
    Large buffers are released as soon as they are used (the worker shares 2 GB with the web server);
    pass the decoded image without keeping another reference to it, so it can be freed early."""
    rgb, faces = retouch.prepare(img)
    del img
    pro = np.asarray(retouch.retouch(rgb, faces, retouch.STYLES["professional"], retouch.iso_of(source),
                                     watermark=False).image)
    del rgb
    out = {"bw_editorial": retouch.add_watermark(Image.fromarray(bw_editorial(pro, seed=photo_id * 17)))}
    base = pro.astype(np.float32)
    del pro
    base /= 255
    film = editorial_film(base, photo_id, faces)
    del base
    film *= 255
    film += 0.5
    out["editorial_film"] = retouch.add_watermark(Image.fromarray(film.astype(np.uint8)))
    return out


def save(images: dict[str, Image.Image], full_paths: dict[str, Path], preview_paths: dict[str, Path]) -> dict[Path, Path]:
    """Write every file under a temporary name next to its destination; returns {temp: destination}."""
    temps: dict[Path, Path] = {}
    try:
        for style, im in images.items():
            for dest, side, quality in ((full_paths[style], FULL_SIZE, 90), (preview_paths[style], PREVIEW_SIZE, 88)):
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(f"{dest.stem}.{uuid.uuid4().hex}.tmp")
                temps[tmp] = dest
                copy = im.copy()
                copy.thumbnail((side, side), Image.LANCZOS)
                copy.info = {}
                copy.save(tmp, "JPEG", quality=quality, optimize=True)
    except BaseException:
        for tmp in temps:
            tmp.unlink(missing_ok=True)
        raise
    return temps


# --------------------------------------------------------------------------- B&W Editorial (approved)

def _mono(f, weights):
    return np.clip(f @ np.array(weights, np.float32), 0, 1)


def _lum(f):
    return f @ np.array([0.2126, 0.7152, 0.0722], np.float32)


def _grain(f, amount, scale, seed):
    """Film-like luminance grain, strongest in the mid-tones, sized to the photo."""
    h, w = f.shape[:2]
    noise = np.random.default_rng(seed).standard_normal((h, w)).astype(np.float32)
    noise = cv2.GaussianBlur(noise, (0, 0), 0.55 * scale + 0.25)
    noise /= noise.std() + 1e-6
    l = _lum(f)
    weight = 0.35 + 2.6 * l * (1 - l)
    return np.clip(f + (amount * noise * weight)[..., None], 0, 1)


def bw_editorial(pro: np.ndarray, seed: int) -> np.ndarray:
    f = pro.astype(np.float32) / 255
    H, W = f.shape[:2]
    scale = max(H, W) / 4096
    g = _mono(f, (0.40, 0.48, 0.12))                     # red-rich: clean, luminous skin
    g8 = (g * 255).astype(np.uint8)
    local = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8)).apply(g8).astype(np.float32) / 255
    g = g * 0.72 + local * 0.28                           # detailed faces and fabrics
    g = np.interp(g, [0, .06, .25, .5, .75, .9, 1], [0, .018, .2, .5, .8, .915, .965]).astype(np.float32)
    g = np.clip(g + 0.25 * (g - cv2.GaussianBlur(g, (0, 0), 1.0 * scale + 0.3)), 0, 1)
    out = _grain(np.repeat(g[..., None], 3, axis=2), 0.007, scale, seed)
    return (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)


# --------------------------------------------------------------------------- Editorial Film (approved)

_table = None
_table_lock = threading.Lock()


def load_cube(path: Path = CUBE_PATH) -> np.ndarray:
    """Plain .cube reader (red varies fastest in the file) -> table indexed [r, g, b]."""
    size, rows = None, []
    for line in path.read_text().splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        if parts[0] == "LUT_3D_SIZE":
            size = int(parts[1])
        elif parts[0][0].isdigit() or parts[0][0] in "-.":
            rows.append([float(v) for v in parts[:3]])
    data = np.asarray(rows, np.float32).reshape(size, size, size, 3)     # [b][g][r]
    return np.ascontiguousarray(data.transpose(2, 1, 0, 3))


def build_table(cube: np.ndarray) -> np.ndarray:
    """Full 256^3 uint8 lookup (48 MB) made with Film Lab's tetrahedral apply_lut, one slab at a time."""
    v = np.arange(256, dtype=np.float32) / 255
    t = np.empty((256, 256, 256, 3), np.uint8)
    for r in range(256):
        slab = np.stack(np.meshgrid(np.full(1, v[r]), v, v, indexing="ij"), -1)[0]
        t[r] = (filmlab.apply_lut(slab, cube, 1.0) * 255 + 0.5).clip(0, 255).astype(np.uint8)
    return t


def table() -> np.ndarray:
    """The lookup, built once and cached on the data disk (memory-mapped: shared, read on demand)."""
    global _table
    with _table_lock:
        if _table is None:
            from .config import settings
            cache = settings.data_dir / "cache" / f"editorial_film.v{STYLE_VERSION}.256.npy"
            if not cache.exists():
                cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_name(f"{cache.name}.{uuid.uuid4().hex}.tmp")
                with open(tmp, "wb") as fh:
                    np.save(fh, build_table(load_cube()))
                tmp.replace(cache)
            _table = np.load(cache, mmap_mode="r").reshape(-1, 3)
        return _table


def apply_table(rgb8, strength=1.0):
    idx = (rgb8[..., 0].astype(np.uint32) << 16) | (rgb8[..., 1].astype(np.uint32) << 8) | rgb8[..., 2]
    out = np.take(table(), idx.ravel(), axis=0).reshape(rgb8.shape)
    if strength >= 1:
        return out.astype(np.float32) / 255
    return (rgb8.astype(np.float32) * (1 - strength) + out.astype(np.float32) * strength) / 255


def to8(x):
    return (np.clip(x, 0, 1) * 255 + 0.5).astype(np.uint8)


def face_calm(shape, faces, keep=0.35):
    """Per-pixel grain weight: 1 everywhere, `keep` on faces (feathered ellipses), so fine film grain
    never sits on eyes and skin texture strongly enough to change how a face reads."""
    m = np.zeros(shape[:2], np.float32)
    for f in faces or []:
        x, y, w, h = f.box
        cv2.ellipse(m, (int(x + w / 2), int(y + h / 2)), (int(w * 0.75), int(h * 0.9)), 0, 0, 360, 1.0, -1)
    if m.any():
        m = cv2.GaussianBlur(m, (0, 0), max(4.0, max(shape[:2]) / 400))
    return (1 - (1 - keep) * np.clip(m, 0, 1))[..., None]


BAND, OVERLAP = 512, 64                                        # rows per band


def banded(img, faces, step):
    """Run step(chunk, calm_chunk, k_short, k_long, band_no) over 512-row bands with overlap, so peak memory is
    a few bands, not several full frames. k_short / k_long rescale Film Lab's frame-relative sizes (grain: short
    edge) so each band renders exactly as the full frame would."""
    h, w = img.shape[:2]
    calm = face_calm(img.shape, faces)
    out = np.empty_like(img, dtype=np.float32)
    for n, y0 in enumerate(range(0, h, BAND)):
        a, b = max(0, y0 - OVERLAP), min(h, y0 + BAND + OVERLAP)
        ch = img[a:b]
        res = step(ch, calm[a:b], min(h, w) / min(ch.shape[:2]), max(h, w) / max(ch.shape[:2]), n)
        out[y0:min(h, y0 + BAND)] = res[y0 - a:y0 - a + min(BAND, h - y0)]
    return out


def grain(img, intensity, size, seed, calm, k_short):
    return img + (filmlab.add_grain(img, intensity, size * k_short, seed) - img) * calm


def editorial_film(base, seed, faces=None):
    return banded(base, faces, lambda c, calm, ks, kl, n:
                  grain(apply_table(to8(c)), 0.009, 0.0004, seed * 1000 + n, calm, ks))
