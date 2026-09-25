"""Retention and deletion rules."""
import os
import time
from datetime import datetime, timedelta, timezone

from conftest import DATA_DIR, NASA, SEARCH, create_event, files_in_data, process_all, upload

from app import db, maintenance
from app.config import settings


def test_new_events_get_the_default_expiry(admin):
    event_id, _ = create_event(admin)
    with db.connect() as conn:
        days = conn.execute("SELECT expires_at - created_at AS d FROM events WHERE id = %s", (event_id,)).fetchone()["d"]
    assert days == timedelta(days=settings.event_retention_days)


def test_admin_can_change_expiry_within_limits(admin):
    event_id, _ = create_event(admin)
    assert admin.post(f"/admin/events/{event_id}/expiry", data={"days": "10"}, follow_redirects=False).status_code == 303
    with db.connect() as conn:
        left = conn.execute("SELECT expires_at - now() AS d FROM events WHERE id = %s", (event_id,)).fetchone()["d"]
    assert timedelta(days=9) < left <= timedelta(days=10)
    assert admin.post(f"/admin/events/{event_id}/expiry", data={"days": "400"}).status_code == 400


def test_expired_events_are_deleted_with_files_and_face_data(admin, client):
    event_id, token = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    link = client.post(f"/e/{token}/search", content=(SEARCH / "search1_exact_copy.jpg").read_bytes()).json()["matches"][0]["view"]
    with db.connect() as conn:
        conn.execute("UPDATE events SET expires_at = now() - interval '1 minute' WHERE id = %s", (event_id,))
    report = maintenance.run_once()
    assert report["expired_events"] == 1
    assert files_in_data() == set()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM faces").fetchone()["n"] == 0
    assert client.get(link).status_code == 404
    assert client.get(f"/e/{token}").status_code == 404


def test_orphan_files_and_folders_are_swept(admin):
    event_id, _ = create_event(admin)
    orphan = DATA_DIR / "events" / str(event_id) / "thumbs" / "999.jpg"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")
    ghost = DATA_DIR / "events" / "12345" / "previews" / "1.jpg"
    ghost.parent.mkdir(parents=True)
    ghost.write_bytes(b"x")
    stale_part = DATA_DIR / "incoming" / "abc.part"
    stale_part.parent.mkdir(parents=True)
    stale_part.write_bytes(b"x")
    old = time.time() - 2 * 3600
    for p in (orphan, ghost, stale_part):
        os.utime(p, (old, old))
    report = maintenance.run_once()
    assert report["orphan_files"] >= 3
    assert not orphan.exists() and not ghost.parent.parent.exists() and not stale_part.exists()


def test_fresh_files_are_not_swept_while_uploads_are_in_flight():
    part = DATA_DIR / "incoming" / "busy.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x")
    maintenance.run_once()
    assert part.exists()


def test_old_rate_limits_and_search_logs_are_pruned(admin):
    event_id, _ = create_event(admin)
    long_ago = datetime.now(timezone.utc) - timedelta(days=45)
    with db.connect() as conn:
        conn.execute("INSERT INTO rate_limits VALUES ('login:x', %s, 3)", (long_ago,))
        conn.execute("INSERT INTO search_log (event_id, at, outcome, results, duration_ms) VALUES (%s, %s, 'ok', 1, 5)",
                     (event_id, long_ago))
    report = maintenance.run_once()
    assert report["rate_limits"] == 1 and report["search_logs"] == 1
