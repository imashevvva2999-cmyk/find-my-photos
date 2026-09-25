"""Safe image handling: allowed formats only, size checked before decoding, no metadata leaks."""
import io
import struct
import zlib

import pytest
from PIL import Image
from conftest import NASA

from app import images

LIMIT = 40_000_000


def encode(fmt: str, size=(64, 48), **kw) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 90)).save(buf, fmt, **kw)
    return buf.getvalue()


def png_header_only(width: int, height: int) -> bytes:
    """A tiny file whose header claims a huge image (decompression bomb)."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b"")


@pytest.mark.parametrize("fmt,ext", [("JPEG", "a.jpg"), ("PNG", "a.png"), ("WEBP", "a.webp")])
def test_accepts_allowed_formats(fmt, ext):
    info = images.inspect(encode(fmt), LIMIT, filename=ext)
    assert (info.width, info.height) == (64, 48)


def test_accepts_heic():
    pytest.importorskip("pillow_heif")
    try:
        data = encode("HEIF")
    except (KeyError, OSError):
        pytest.skip("HEIF encoder not available in this build")
    assert images.inspect(data, LIMIT, filename="a.heic").format == "HEIF"


@pytest.mark.parametrize("data,name", [
    (encode("GIF"), "a.gif"),
    (encode("BMP"), "a.bmp"),
    (encode("TIFF"), "a.tif"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "a.svg"),
    (b"just some text", "a.jpg"),
    (encode("JPEG")[:200], "a.jpg"),          # truncated
])
def test_rejects_other_formats_and_corrupt_files(data, name):
    with pytest.raises(images.ImageRejected):
        images.decode(data, LIMIT, 1024, filename=name)


def test_rejects_decompression_bomb_before_decoding():
    bomb = png_header_only(20_000, 10_000)     # 200 megapixels claimed by a ~100 byte file
    with pytest.raises(images.ImageRejected) as err:
        images.inspect(bomb, LIMIT, filename="bomb.png")
    assert "слишком большое" in str(err.value).lower()


def test_rejects_extension_that_does_not_match_content():
    with pytest.raises(images.ImageRejected):
        images.inspect(encode("PNG"), LIMIT, filename="photo.jpg")


def test_applies_exif_orientation():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotated 90 degrees
    data = encode("JPEG", size=(200, 100), exif=exif.tobytes())
    assert images.decode(data, LIMIT, 4096).size == (100, 200)


def test_clean_copy_has_no_metadata(tmp_path):
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"
    exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}  # GPS
    img = images.decode(encode("JPEG", size=(300, 200), exif=exif.tobytes()), LIMIT, 4096)
    out = tmp_path / "clean.jpg"
    images.save_clean_jpeg(img, out, max_side=4096)
    with Image.open(out) as saved:
        assert not saved.getexif()
        assert "exif" not in saved.info


def test_large_jpeg_is_decoded_at_reduced_size():
    img = images.decode((NASA / "photo_16.jpg").read_bytes(), LIMIT, 400)   # 1920x1600 source
    assert 400 <= max(img.size) < 1920
