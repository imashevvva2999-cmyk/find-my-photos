"""Background worker: durable queue, retries, deletion races, metadata-free copies."""
import threading

from PIL import Image
from conftest import DATA_DIR, NASA, create_event, files_in_data, process_all, upload

from app import db, faces, storage, worker


def photo_rows(event_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM photos WHERE event_id = %s ORDER BY id", (event_id,)).fetchall()


def test_photos_are_processed_with_previews_and_clean_download_copy(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg", NASA / "photo_16.jpg"])
    assert process_all() == 2
    rows = photo_rows(event_id)
    assert [r["status"] for r in rows] == ["done", "done"]
    assert rows[1]["face_count"] == 6 and rows[0]["has_display"]
    for r in rows:
        for path in (storage.display_path(event_id, r["id"]), storage.preview_path(event_id, r["id"]),
                     storage.thumb_path(event_id, r["id"])):
            assert path.exists()
        with Image.open(storage.display_path(event_id, r["id"])) as img:
            assert not img.getexif()


def test_two_claimers_never_get_the_same_photo(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / f"photo_{i:02d}.jpg" for i in range(1, 6)])
    upload(admin, event_id, [NASA / f"photo_{i:02d}.jpg" for i in range(6, 11)])
    claimed, lock = [], threading.Lock()

    def claim_all():
        while (photo := worker.claim_one()) is not None:
            with lock:
                claimed.append(photo["id"])

    threads = [threading.Thread(target=claim_all) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == 10 and len(set(claimed)) == 10


def test_transient_failure_is_retried_then_succeeds(admin, monkeypatch):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    real = faces.find_faces
    calls = {"n": 0}

    def flaky(img):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("temporary disk problem")
        return real(img)

    monkeypatch.setattr(worker.faces, "find_faces", flaky)
    process_all()
    row = photo_rows(event_id)[0]
    assert row["status"] == "pending" and row["attempts"] == 1 and row["error"]
    with db.connect() as conn:  # skip the backoff wait
        conn.execute("UPDATE photos SET next_attempt_at = now()")
    process_all()
    assert photo_rows(event_id)[0]["status"] == "done"


def test_gives_up_after_max_attempts(admin, monkeypatch):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    monkeypatch.setattr(worker.faces, "find_faces", lambda img: (_ for _ in ()).throw(OSError("still broken")))
    for _ in range(worker.MAX_ATTEMPTS):
        with db.connect() as conn:
            conn.execute("UPDATE photos SET next_attempt_at = now()")
        process_all()
    row = photo_rows(event_id)[0]
    assert row["status"] == "error" and row["attempts"] == worker.MAX_ATTEMPTS


def test_unreadable_original_fails_immediately(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    row = photo_rows(event_id)[0]
    storage.original_path(event_id, row["file_name"]).write_bytes(b"corrupted after upload")
    process_all()
    row = photo_rows(event_id)[0]
    assert row["status"] == "error" and row["attempts"] == 1


def test_stale_processing_rows_are_reclaimed(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    with db.connect() as conn:
        conn.execute("UPDATE photos SET status = 'processing', locked_at = now() - interval '30 minutes'")
    assert worker.reap_stale() == 1
    assert photo_rows(event_id)[0]["status"] == "pending"


def test_photo_deleted_while_processing_leaves_no_files(admin, monkeypatch):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    photo_id = photo_rows(event_id)[0]["id"]
    real = faces.find_faces

    def delete_midway(img):
        result = real(img)
        assert admin.post(f"/admin/api/photos/{photo_id}/delete").status_code == 200  # admin deletes it now
        return result

    monkeypatch.setattr(worker.faces, "find_faces", delete_midway)
    process_all()
    assert files_in_data() == set()


def test_admin_can_retry_failed_photos(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    with db.connect() as conn:
        conn.execute("UPDATE photos SET status = 'error', error = 'x', attempts = 3")
    r = admin.post(f"/admin/api/events/{event_id}/retry")
    assert r.json() == {"requeued": 1}
    process_all()
    assert photo_rows(event_id)[0]["status"] == "done"


def test_index_version_changes_when_faces_change(admin):
    event_id, _ = create_event(admin)
    with db.connect() as conn:
        before = conn.execute("SELECT index_version FROM events WHERE id = %s", (event_id,)).fetchone()["index_version"]
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    with db.connect() as conn:
        after = conn.execute("SELECT index_version FROM events WHERE id = %s", (event_id,)).fetchone()["index_version"]
    assert after > before


def test_heartbeat_is_recorded():
    worker.beat("test-worker", processed=3)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM worker_heartbeats WHERE name = 'test-worker'").fetchone()
    assert row["processed"] == 3


def test_no_temp_files_left_after_processing(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_16.jpg"])
    process_all()
    assert not [p for p in DATA_DIR.rglob("*.part")] and not [p for p in DATA_DIR.rglob("*.tmp")]
