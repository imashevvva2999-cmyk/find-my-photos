"""Background processing of uploaded event photos. Runs as its own process:

    .venv/bin/python -m app.worker

The queue lives in PostgreSQL (photos.status = 'pending'). A photo is claimed with
SELECT ... FOR UPDATE SKIP LOCKED, so any number of worker processes and threads can run
without processing the same photo twice. Temporary failures are retried with a growing
delay; photos whose worker died are reclaimed after 10 minutes. Result files are written
under temporary names and moved into place only while the photo row is locked and still
exists, so a photo deleted during processing leaves nothing behind.
"""
import logging
import signal
import socket
import threading
import time
import uuid
from datetime import datetime, timezone

import numpy as np

from . import db, faces, images, maintenance, storage
from .config import settings
from .observability import setup_logging

log = logging.getLogger("worker")

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 120, 600)
STALE_MINUTES = 10
HEARTBEAT_SECONDS = 10
MAINTENANCE_SECONDS = 15 * 60


class PermanentFailure(Exception):
    """Retrying will not help (e.g. the file is not a readable image)."""


def claim_one() -> dict | None:
    with db.connect() as conn:
        return conn.execute("""
            UPDATE photos SET status = 'processing', locked_at = now(), attempts = attempts + 1, updated_at = clock_timestamp()
            WHERE id = (
                SELECT id FROM photos WHERE status = 'pending' AND next_attempt_at <= now()
                ORDER BY next_attempt_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *
        """).fetchone()


def _render(photo: dict) -> tuple[dict, list, int]:
    """Decode once; write the three copies under temporary names; find faces."""
    event_id, photo_id = photo["event_id"], photo["id"]
    started = time.perf_counter()
    source = storage.original_path(event_id, photo["file_name"])
    if not source.exists():
        raise PermanentFailure("Загруженный файл не найден.")
    try:
        img = images.decode(source, settings.max_event_pixels, target_side=storage.DISPLAY_SIZE)
    except images.ImageRejected as exc:
        raise PermanentFailure(str(exc)) from exc
    temps = {}
    for dest, size in ((storage.display_path(event_id, photo_id), storage.DISPLAY_SIZE),
                       (storage.preview_path(event_id, photo_id), storage.PREVIEW_SIZE),
                       (storage.thumb_path(event_id, photo_id), storage.THUMB_SIZE)):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.stem}.{uuid.uuid4().hex}.tmp")
        temps[tmp] = dest
        images.save_clean_jpeg(img, tmp, size)
    found = faces.find_faces(img)
    return temps, found, round((time.perf_counter() - started) * 1000)


def _fail(photo: dict, message: str, permanent: bool) -> None:
    with db.connect() as conn:
        if permanent or photo["attempts"] >= MAX_ATTEMPTS:
            conn.execute("""UPDATE photos SET status = 'error', error = %s, locked_at = NULL, updated_at = clock_timestamp()
                            WHERE id = %s AND status = 'processing'""", (message, photo["id"]))
        else:
            delay = BACKOFF_SECONDS[min(photo["attempts"], len(BACKOFF_SECONDS)) - 1]
            conn.execute("""UPDATE photos SET status = 'pending', error = %s, locked_at = NULL, updated_at = clock_timestamp(),
                                   next_attempt_at = now() + make_interval(secs => %s)
                            WHERE id = %s AND status = 'processing'""", (message, delay, photo["id"]))


def process(photo: dict) -> bool:
    temps: dict = {}
    try:
        temps, found, processing_ms = _render(photo)
        with db.connect() as conn:
            row = conn.execute("SELECT id FROM photos WHERE id = %s AND status = 'processing' FOR UPDATE",
                               (photo["id"],)).fetchone()
            if row is None:
                log.info("photo %s was deleted during processing", photo["id"])
                return False
            for tmp, dest in temps.items():
                storage.place(tmp, dest)
            conn.execute("DELETE FROM faces WHERE photo_id = %s", (photo["id"],))
            conn.cursor().executemany(
                "INSERT INTO faces (photo_id, event_id, x, y, w, h, score, embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(photo["id"], photo["event_id"], *f.box, f.score, f.embedding.astype(np.float32).tobytes()) for f in found])
            conn.execute("""
                UPDATE photos SET status = 'done', error = NULL, face_count = %s, processing_ms = %s,
                       processed_at = now(), has_display = TRUE, locked_at = NULL, updated_at = clock_timestamp()
                WHERE id = %s""", (len(found), processing_ms, photo["id"]))
            conn.execute("UPDATE events SET index_version = index_version + 1 WHERE id = %s", (photo["event_id"],))
        log.info("photo %s processed: faces=%s ms=%s", photo["id"], len(found), processing_ms)
        return True
    except PermanentFailure as exc:
        log.warning("photo %s failed permanently: %s", photo["id"], exc)
        _fail(photo, str(exc), permanent=True)
    except Exception as exc:
        log.warning("photo %s failed (attempt %s): %s", photo["id"], photo["attempts"], type(exc).__name__)
        try:
            _fail(photo, "Обработка не удалась; она будет автоматически повторена.", permanent=False)
        except db.DatabaseUnavailable:
            pass  # the stale-row reaper will pick the photo up again
    finally:
        for tmp in temps:
            tmp.unlink(missing_ok=True)
    return False


def reap_stale() -> int:
    """Photos stuck in 'processing' (worker crashed or was killed) go back to the queue."""
    with db.connect() as conn:
        cur = conn.execute(f"""
            UPDATE photos SET status = CASE WHEN attempts >= {MAX_ATTEMPTS} THEN 'error' ELSE 'pending' END,
                   error = CASE WHEN attempts >= {MAX_ATTEMPTS} THEN 'Processing did not finish.' ELSE error END,
                   locked_at = NULL, next_attempt_at = now(), updated_at = clock_timestamp()
            WHERE status = 'processing' AND locked_at < now() - interval '{STALE_MINUTES} minutes'""")
        return cur.rowcount


def retry_failed(event_id: int) -> int:
    with db.connect() as conn:
        cur = conn.execute("""UPDATE photos SET status = 'pending', attempts = 0, error = NULL,
                                     next_attempt_at = now(), updated_at = clock_timestamp()
                              WHERE event_id = %s AND status = 'error'""", (event_id,))
        return cur.rowcount


def run_pending() -> int:
    """Process until nothing is ready (used by tests and tools). Returns photos claimed."""
    claimed = 0
    while (photo := claim_one()) is not None:
        claimed += 1
        process(photo)
    return claimed


def beat(name: str, processed: int) -> None:
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute("""
            INSERT INTO worker_heartbeats (name, pid, started_at, beat_at, processed) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET pid = EXCLUDED.pid, beat_at = EXCLUDED.beat_at, processed = EXCLUDED.processed
        """, (name, _pid(), now, now, processed))


def _pid() -> int:
    import os
    return os.getpid()


def main() -> None:
    setup_logging(settings.log_level)
    db.init(max_size=settings.worker_threads + 3)
    stop = threading.Event()
    counter = {"processed": 0}
    counter_lock = threading.Lock()
    name = f"{socket.gethostname()}:{_pid()}"

    def handle_signal(signum, _frame):
        log.info("stopping after the current photos (signal %s)", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    def loop():
        while not stop.is_set():
            try:
                photo = claim_one()
            except db.DatabaseUnavailable:
                stop.wait(5)
                continue
            if photo is None:
                stop.wait(0.5)
                continue
            process(photo)
            with counter_lock:
                counter["processed"] += 1

    threads = [threading.Thread(target=loop, name=f"photo-{i}") for i in range(settings.worker_threads)]
    for t in threads:
        t.start()
    log.info("worker started: threads=%s", settings.worker_threads)
    last_maintenance = 0.0
    while not stop.is_set():
        try:
            beat(name, counter["processed"])
            reaped = reap_stale()
            if reaped:
                log.warning("reclaimed %s stuck photos", reaped)
            if time.monotonic() - last_maintenance > MAINTENANCE_SECONDS:
                maintenance.run_once()
                last_maintenance = time.monotonic()
        except db.DatabaseUnavailable:
            log.warning("database unavailable; retrying")
        except Exception:
            log.exception("maintenance step failed")
        stop.wait(HEARTBEAT_SECONDS)
    for t in threads:
        t.join()
    db.close()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
