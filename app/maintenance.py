"""Retention and clean-up, run by the worker every 15 minutes (and by tests directly).

Rules:
- An event (photos, face data, files) is deleted when it expires (events.expires_at,
  default EVENT_RETENTION_DAYS after creation; the admin can change it) or is deleted.
- Files without a database row (e.g. left by a crash) are removed after 1 hour.
- Rate-limit counters are kept 1 day, search statistics 30 days.
Visitor selfies are never stored, so there is nothing to clean up for them.
"""
import logging
import shutil
import time
from pathlib import Path

import psycopg

from . import db, matching, storage

log = logging.getLogger("maintenance")

ORPHAN_MIN_AGE_S = 3600
RATE_LIMIT_KEEP = "1 day"
SEARCH_LOG_KEEP = "30 days"


def purge_expired_events() -> int:
    with db.connect() as conn:
        expired = [r["id"] for r in conn.execute("SELECT id FROM events WHERE expires_at < now()").fetchall()]
    deleted = 0
    for event_id in expired:
        try:
            delete_event(event_id)
            deleted += 1
        except Exception as exc:  # one failure must not stop the rest; retried next run
            log.error("could not delete expired event %s: %s", event_id, type(exc).__name__)
    if deleted:
        log.info("deleted %s expired events", deleted)
    return deleted


def delete_event(event_id: int, attempts: int = 3) -> None:
    """Delete an event's rows (photos and faces cascade) and then its files.

    Rows are locked in the same order the worker uses (photos first, then the event), so a
    deletion and the processing of one of its photos cannot deadlock. A lock conflict that
    still happens is retried. The event's face data is also dropped from this process's
    search cache."""
    for attempt in range(1, attempts + 1):
        try:
            with db.connect() as conn:
                conn.execute("SELECT id FROM photos WHERE event_id = %s ORDER BY id FOR UPDATE", (event_id,))
                conn.execute("DELETE FROM events WHERE id = %s", (event_id,))
            break
        except psycopg.OperationalError as exc:
            if not db.is_retryable(exc) or attempt == attempts:
                raise
            time.sleep(0.2 * attempt)
    matching.forget(event_id)
    try:
        storage.delete_event_files(event_id)
    except OSError as exc:
        log.error("could not delete files of event %s: %s (the orphan sweep will retry)", event_id, type(exc).__name__)


def _old(path: Path, now: float) -> bool:
    try:
        return now - path.stat().st_mtime > ORPHAN_MIN_AGE_S
    except FileNotFoundError:
        return False


def sweep_orphans() -> int:
    now = time.time()
    removed = 0
    incoming = storage.settings.data_dir / "incoming"
    if incoming.exists():
        for part in incoming.iterdir():
            if part.is_file() and _old(part, now):
                part.unlink(missing_ok=True)
                removed += 1

    root = storage.events_root()
    if not root.exists():
        return removed
    with db.connect() as conn:
        events = {r["id"] for r in conn.execute("SELECT id FROM events").fetchall()}
        photos = conn.execute("SELECT id, event_id, file_name FROM photos").fetchall()
    photo_ids = {(p["event_id"], p["id"]) for p in photos}
    originals = {(p["event_id"], p["file_name"]) for p in photos}

    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        event_id = int(folder.name) if folder.name.isdigit() else None
        files = [f for f in folder.rglob("*") if f.is_file()]
        if event_id not in events:
            if all(_old(f, now) for f in files):
                shutil.rmtree(folder, ignore_errors=True)
                removed += len(files)
            continue
        for f in files:
            if not _old(f, now):
                continue
            kind = f.parent.name
            if f.name.endswith(".tmp"):
                orphan = True
            elif kind == "originals":
                orphan = (event_id, f.name) not in originals
            else:
                orphan = not f.stem.isdigit() or (event_id, int(f.stem)) not in photo_ids
            if orphan:
                f.unlink(missing_ok=True)
                removed += 1
    if removed:
        log.info("removed %s orphaned files", removed)
    return removed


def prune() -> tuple[int, int]:
    with db.connect() as conn:
        rl = conn.execute(f"DELETE FROM rate_limits WHERE window_start < now() - interval '{RATE_LIMIT_KEEP}'").rowcount
        sl = conn.execute(f"DELETE FROM search_log WHERE at < now() - interval '{SEARCH_LOG_KEEP}'").rowcount
    return rl, sl


def run_once() -> dict:
    expired = purge_expired_events()
    orphans = sweep_orphans()
    rate_limits, search_logs = prune()
    return {"expired_events": expired, "orphan_files": orphans, "rate_limits": rate_limits, "search_logs": search_logs}
