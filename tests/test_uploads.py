"""Upload pipeline: per-file validation, size limits while streaming, no orphaned files."""
import io

from PIL import Image
from conftest import NASA, create_event, files_in_data, upload
from test_images import png_header_only

from app import uploads


def jpeg_bytes(size=(80, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def gif_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "GIF")
    return buf.getvalue()


def test_mixed_batch_gets_a_result_per_file(admin):
    event_id, _ = create_event(admin)
    r = upload(admin, event_id, [
        NASA / "photo_30.jpg",
        ("broken.jpg", b"not a photo", "image/jpeg"),
        ("notes.txt", b"hello", "text/plain"),
        ("anim.gif", gif_bytes(), "image/gif"),
        ("bomb.png", png_header_only(20_000, 10_000), "image/png"),
    ])
    assert r.status_code == 200
    body = r.json()
    assert [a["name"] for a in body["accepted"]] == ["photo_30.jpg"]
    assert {x["name"] for x in body["rejected"]} == {"broken.jpg", "notes.txt", "anim.gif", "bomb.png"}
    assert all(x["reason"] for x in body["rejected"])
    stored = files_in_data()
    assert len(stored) == 1 and stored.pop().startswith(f"events/{event_id}/originals/")   # no temp leftovers


def test_file_over_the_size_limit_is_rejected_without_leftovers(admin):
    event_id, _ = create_event(admin)
    too_big = jpeg_bytes() + b"\0" * (31 * 1024 * 1024)
    r = upload(admin, event_id, [("huge.jpg", too_big, "image/jpeg")])
    assert r.status_code == 200
    assert r.json()["rejected"][0]["reason"].startswith("Больше")
    assert files_in_data() == set()


def test_extension_must_match_content(admin):
    event_id, _ = create_event(admin)
    buf = io.BytesIO()
    Image.new("RGB", (50, 50)).save(buf, "PNG")
    r = upload(admin, event_id, [("photo.jpg", buf.getvalue(), "image/jpeg")])
    assert r.json()["rejected"][0]["name"] == "photo.jpg"


def test_too_many_files_in_one_request(admin):
    event_id, _ = create_event(admin)
    r = upload(admin, event_id, [(f"{i}.jpg", jpeg_bytes(), "image/jpeg") for i in range(6)])
    assert r.status_code == 413
    assert files_in_data() == set()


def test_request_body_limit_is_enforced_from_content_length(admin):
    event_id, _ = create_event(admin)
    r = admin.post(f"/admin/api/events/{event_id}/photos", content=b"x",
                   headers={"content-type": "multipart/form-data; boundary=B", "content-length": str(10**12)})
    assert r.status_code == 413


def test_upload_to_missing_event(admin):
    assert upload(admin, 999, [NASA / "photo_30.jpg"]).status_code == 404
    assert files_in_data() == set()


def test_low_disk_space_is_reported(admin, monkeypatch):
    event_id, _ = create_event(admin)
    monkeypatch.setattr(uploads, "disk_has_room", lambda: False)
    r = upload(admin, event_id, [NASA / "photo_30.jpg"])
    assert r.status_code == 507
    assert "диск" in r.json()["message"].lower()
    assert files_in_data() == set()


def test_database_failure_leaves_no_file(admin, monkeypatch):
    event_id, _ = create_event(admin)

    def boom(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(uploads, "_insert_photo", boom)
    r = upload(admin, event_id, [NASA / "photo_30.jpg"])
    assert r.json()["rejected"][0]["reason"] == "Не удалось сохранить. Попробуйте ещё раз."
    assert files_in_data() == set()
