"""Regression tests for the findings of the second code review (N2-N8, N13)."""
import io
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image
from conftest import ADMIN_PASSWORD, NASA, ORIGIN, SEARCH, create_event, files_in_data, process_all, upload

from app import faces, main, maintenance, matching, security, worker
from app.config import settings

SELFIE = (SEARCH / "search2_koch_2018_portrait.jpg").read_bytes()


def test_parallel_wrong_passwords_cannot_exceed_the_limit(app):
    """N4: the attempt is counted before the slow password check."""
    codes, lock = [], threading.Lock()

    def attempt():
        c = TestClient(app, headers=ORIGIN)
        r = c.post("/admin/login", data={"password": "wrong-guess"})
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(401) <= settings.login_failures_per_15_min      # at most 5 real password checks
    assert codes.count(429) >= 20 - settings.login_failures_per_15_min


def test_logout_revokes_a_copied_session_cookie(admin, app):
    """N8: a stolen cookie stops working after logout."""
    stolen = admin.cookies.get("admin_session")
    admin.post("/admin/logout")
    thief = TestClient(app, headers=ORIGIN, cookies={"admin_session": stolen})
    security._valid_after["checked"] = 0.0  # skip the 5-second cache
    assert thief.get("/admin", follow_redirects=False).status_code == 303


def test_session_has_an_absolute_lifetime(admin, monkeypatch):
    """N8: activity does not extend a session beyond 12 hours."""
    assert admin.get("/admin", follow_redirects=False).status_code == 200
    real = time.time
    monkeypatch.setattr(security.time, "time", lambda: real() + security.SESSION_MAX_AGE + 60)
    assert admin.get("/admin", follow_redirects=False).status_code == 303


def test_login_still_works_after_logout(admin, app):
    admin.post("/admin/logout")
    security._valid_after["checked"] = 0.0
    fresh = TestClient(app, headers=ORIGIN)
    assert fresh.post("/admin/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False).status_code == 303
    assert fresh.get("/admin", follow_redirects=False).status_code == 200


def test_ultra_hdr_style_mpo_jpegs_are_accepted(admin):
    """N5: JPEGs with extra images (MPO) are normal phone photos."""
    event_id, _ = create_event(admin)
    buf = io.BytesIO()
    base = Image.open(NASA / "photo_30.jpg").convert("RGB")
    base.save(buf, "MPO", save_all=True, append_images=[base.resize((base.width // 4, base.height // 4))])
    r = upload(admin, event_id, [("phone.jpg", buf.getvalue(), "image/jpeg")])
    assert [a["name"] for a in r.json()["accepted"]] == ["phone.jpg"], r.json()
    process_all()
    assert admin.get(f"/admin/api/events/{event_id}/status").json()["done"] == 1


def test_deleting_an_event_removes_its_faces_from_memory(admin, client):
    """N7."""
    event_id, token = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    client.post(f"/e/{token}/search", content=SELFIE)
    assert event_id in matching._cache
    admin.post(f"/admin/events/{event_id}/delete")
    assert event_id not in matching._cache


def test_new_photos_are_added_to_the_cache_without_a_full_reload(admin, client, monkeypatch):
    """N6: while an event is being indexed, only new faces are fetched."""
    event_id, token = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    first = client.post(f"/e/{token}/search", content=SELFIE).json()
    calls = []
    real_fetch = matching._fetch
    monkeypatch.setattr(matching, "_fetch", lambda eid, after: calls.append(after) or real_fetch(eid, after))
    upload(admin, event_id, [NASA / "photo_40.jpg"])
    process_all()
    second = client.post(f"/e/{token}/search", content=SELFIE).json()
    assert calls and all(after > 0 for after in calls)      # incremental fetch only
    assert len(second["matches"]) == len(first["matches"]) + 1


def test_receiving_a_photo_does_not_need_a_search_slot(admin, client):
    """N2: slots are taken only for the computation, not while a (slow) upload arrives."""
    event_id, token = create_event(admin)
    gate = main.search_gate
    held = [gate.try_enter() for _ in range(gate.capacity)]
    try:
        r = client.post(f"/e/{token}/search", content=b"")
        assert r.status_code == 422 and r.json()["error"] == "empty"   # answered without a slot
    finally:
        for ok in held:
            if ok:
                gate.leave()


def test_oversized_upload_without_content_length_gets_413(admin, monkeypatch):
    """N13: a chunked body over the limit is 'too large', not 'could not be read'."""
    event_id, _ = create_event(admin)
    monkeypatch.setattr(settings, "max_upload_mb", 1)           # request limit becomes 6 MB

    def body():
        yield b'--B\r\nContent-Disposition: form-data; name="files"; filename="x.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
        for _ in range(8):
            yield b"\0" * (1 << 20)
        yield b"\r\n--B--\r\n"

    r = admin.post(f"/admin/api/events/{event_id}/photos", content=body(),
                   headers={"content-type": "multipart/form-data; boundary=B"})
    assert r.status_code == 413
    assert files_in_data() == set()


def test_deleting_an_event_while_its_photo_is_processed(admin, monkeypatch):
    """N3: event deletion and photo processing do not deadlock or leave files behind."""
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_16.jpg"])
    real = faces.find_faces

    def delete_meanwhile(img):
        result = real(img)
        maintenance.delete_event(event_id)
        return result

    monkeypatch.setattr(worker.faces, "find_faces", delete_meanwhile)
    process_all()
    assert files_in_data() == set()
    assert admin.get(f"/admin/events/{event_id}").status_code == 404
