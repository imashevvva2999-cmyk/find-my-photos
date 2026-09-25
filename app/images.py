"""Safe image handling for uploads and selfies.

- Only JPEG, PNG, WEBP and HEIC/HEIF are opened; every other format is refused.
- The pixel count is read from the file header and checked BEFORE decoding, so a tiny
  file that claims a gigantic image (a "decompression bomb") is refused cheaply.
- JPEGs are decoded directly at a reduced size when a smaller image is enough.
- Copies shown or downloaded by visitors are re-encoded without any metadata (no GPS,
  camera serial numbers or timestamps).
"""
import io
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()
Image.MAX_IMAGE_PIXELS = 200_000_000   # absolute ceiling; the real, lower limits are checked first
warnings.simplefilter("error", Image.DecompressionBombWarning)

# MPO = JPEG files with extra images (Android Ultra HDR / gain maps, many Samsung/Pixel photos).
# Pillow opens them with its JPEG reader (so "MPO" is not an opener name) and reports "MPO".
FORMATS = ("JPEG", "PNG", "WEBP", "HEIF")
EXTENSIONS = {"JPEG": {".jpg", ".jpeg"}, "MPO": {".jpg", ".jpeg"}, "PNG": {".png"}, "WEBP": {".webp"},
              "HEIF": {".heic", ".heif"}}
UNREADABLE = "Файл повреждён или не является фотографией JPG, PNG, WEBP или HEIC."


class ImageRejected(ValueError):
    """The file is not an acceptable image. The message is safe to show to the user."""


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int
    height: int


def _open(source: bytes | Path) -> Image.Image:
    fp = io.BytesIO(source) if isinstance(source, (bytes, bytearray, memoryview)) else source
    try:
        return Image.open(fp, formats=FORMATS)
    except Exception as exc:  # unknown format, truncated header, bomb warning...
        raise ImageRejected(UNREADABLE) from exc


def _check(img: Image.Image, max_pixels: int, filename: str | None) -> None:
    width, height = img.size
    if width < 1 or height < 1:
        raise ImageRejected(UNREADABLE)
    if width * height > max_pixels:
        raise ImageRejected(f"Изображение слишком большое ({width}×{height} пикселей; "
                            f"предел — {max_pixels // 1_000_000} мегапикселей).")
    if filename:
        ext = Path(filename).suffix.lower()
        if ext and ext not in EXTENSIONS.get(img.format, set()):
            raise ImageRejected("Расширение файла не соответствует типу изображения внутри файла.")


def inspect(source: bytes | Path, max_pixels: int, filename: str | None = None) -> ImageInfo:
    """Check format, pixel count and file structure without decoding the pixels."""
    img = _open(source)
    try:
        _check(img, max_pixels, filename)
        info = ImageInfo(img.format, img.width, img.height)
        img.verify()  # structural check (e.g. PNG chunk checksums) without a full decode
    except ImageRejected:
        raise
    except Exception as exc:
        raise ImageRejected(UNREADABLE) from exc
    finally:
        img.close()
    return info


def decode(source: bytes | Path, max_pixels: int, target_side: int | None = None,
           filename: str | None = None) -> Image.Image:
    """Decode to an upright RGB image. With target_side, JPEGs are decoded at a reduced
    size that is still at least target_side pixels on each side (saves memory)."""
    img = _open(source)
    try:
        _check(img, max_pixels, filename)
        if target_side and img.format in ("JPEG", "MPO"):
            img.draft("RGB", (target_side, target_side))
        img.load()
        upright = ImageOps.exif_transpose(img)
        return upright.convert("RGB")
    except ImageRejected:
        raise
    except Exception as exc:
        raise ImageRejected(UNREADABLE) from exc
    finally:
        img.close()


def save_clean_jpeg(img: Image.Image, path: Path, max_side: int, quality: int = 88) -> None:
    """Save a resized JPEG copy that carries no metadata at all."""
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.LANCZOS)
    copy.info = {}
    copy.save(path, "JPEG", quality=quality, optimize=True)
