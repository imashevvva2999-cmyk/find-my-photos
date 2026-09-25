"""Build synthetic event collections for load tests from permitted test photos only.

Source photos: test_data/collection_40 (NASA, public domain) and sample_photos (MIT-licensed
repository test images). Each output photo is a random variant of a source photo (crop,
mirror, brightness/contrast, resize), so every file is different and needs real processing.
The owner's own photos (photos/) are never used.

    .venv/bin/python tests/load/make_dataset.py 500 2000
Creates test_data/load/set_500/ and test_data/load/set_2000/.
"""
import random
import sys
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

ROOT = Path(__file__).resolve().parents[2]
SOURCES = sorted((ROOT / "test_data" / "collection_40").glob("*.jpg")) + sorted((ROOT / "sample_photos").glob("*.jpg"))
OUT = ROOT / "test_data" / "load"


def variant(src: Path, rng: random.Random) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = img.size
    keep = rng.uniform(0.8, 1.0)
    cw, ch = int(w * keep), int(h * keep)
    x, y = rng.randint(0, w - cw), rng.randint(0, h - ch)
    img = img.crop((x, y, x + cw, y + ch))
    if rng.random() < 0.5:
        img = ImageOps.mirror(img)
    img = ImageEnhance.Brightness(img).enhance(rng.uniform(0.85, 1.15))
    img = ImageEnhance.Contrast(img).enhance(rng.uniform(0.9, 1.1))
    side = rng.randint(1600, 2400)  # typical camera export sizes
    img.thumbnail((side, side)) if max(img.size) > side else None
    if max(img.size) < side:
        scale = side / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def build(count: int) -> Path:
    folder = OUT / f"set_{count}"
    folder.mkdir(parents=True, exist_ok=True)
    rng = random.Random(count)
    existing = len(list(folder.glob("*.jpg")))
    for i in range(existing, count):
        src = SOURCES[i % len(SOURCES)]
        variant(src, rng).save(folder / f"load_{i:05d}.jpg", "JPEG", quality=85)
    return folder


if __name__ == "__main__":
    for n in map(int, sys.argv[1:] or ["500", "2000"]):
        folder = build(n)
        size = sum(p.stat().st_size for p in folder.glob("*.jpg")) / 1e6
        print(f"{folder.relative_to(ROOT)}: {n} photos, {size:.0f} MB")
