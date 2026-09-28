"""Background processing of uploaded event photos. Runs as its own process:

    .venv/bin/python -m app.worker

The queue lives in PostgreSQL (photos.status = 'pending'). A photo is claimed with
SELECT ... FOR UPDATE SKIP LOCKED, so any number of worker processes and threads can run
without processing the same photo twice. Temporary failures are retried with a growing
delay; photos whose worker died are reclaimed after 10 minutes. Result files are written
under temporary names and moved into place only while the photo row is locked and still
exists, so a photo deleted during processing leaves nothing behind.

A second, low-priority job makes the styled versions (app.styles: B&W Editorial, Editorial
Film) of every finished photo whose photos.styles_version is older than STYLE_VERSION - new
uploads and, once, all existing photos. It runs only when no upload is waiting, never at the
same time as upload processing (HEAVY lock, so memory peaks do not add up), at a lower CPU
priority than the web server, in a short-lived child process that the kernel kills first if
memory runs out (so a spike can end only that job), and it is resumable and idempotent: the
version marker is set only after all four files are in place, so an interrupted run continues.
"""
import logging
import os
import subprocess
import sys
import signal
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import db, faces, images, maintenance, storage, styles
from .config import settings
from .observability import setup_logging

log = logging.getLogger("worker")

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 120, 600)
STALE_MINUTES = 10
HEARTBEAT_SECONDS = 10
MAINTENANCE_SECONDS = 15 * 60
STYLE_STALE_MINUTES = 20
STYLE_FREE_DISK_MB = 1024    # styled files never eat into the space kept free for uploads
STYLE_NICE = 10               # the styles job yields the CPU to the web server and to uploads
STYLE_TIMEOUT_S = 15 * 60
APP_ROOT = Path(__file__).resolve().parent.parent
HEAVY = threading.Lock()      # one image-processing job at a time in this process (memory)


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
    with HEAVY:
        return _render_locked(photo)


def _render_locked(photo: dict) -> tuple[dict, list, int]:
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


# --------------------------------------------------------------------------- styled versions

def claim_styles() -> dict | None:
    """The next finished photo without current styled versions - only while no upload is waiting."""
    with db.connect() as conn:
        return conn.execute(f"""
            UPDATE photos SET styles_locked_at = now(), styles_attempts = styles_attempts + 1
            WHERE id = (
                SELECT id FROM photos
                WHERE status = 'done' AND styles_version < %(v)s AND styles_attempts < {MAX_ATTEMPTS}
                  AND styles_next_at <= now()
                  AND (styles_locked_at IS NULL OR styles_locked_at < now() - interval '{STYLE_STALE_MINUTES} minutes')
                  AND NOT EXISTS (SELECT 1 FROM photos q WHERE q.status = 'processing'
                                  OR (q.status = 'pending' AND q.next_attempt_at <= now()))
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *
        """, {"v": styles.STYLE_VERSION}).fetchone()


def process_styles(photo: dict) -> bool:
    """Make both styled versions of one photo. Safe to repeat and to interrupt at any point."""
    event_id, photo_id = photo["event_id"], photo["id"]
    temps: dict = {}
    started = time.perf_counter()
    try:
        source = storage.original_path(event_id, photo["file_name"])
        if not source.exists():
            raise PermanentFailure("Загруженный файл не найден.")
        with HEAVY:
            try:
                rendered = styles.render(images.decode(source, settings.max_event_pixels, target_side=storage.DISPLAY_SIZE),
                                         source, photo_id)
            except images.ImageRejected as exc:
                raise PermanentFailure(str(exc)) from exc
            temps = styles.save(
                rendered,
                {s: storage.styled_path(event_id, s, photo_id, "full") for s in styles.STYLES},
                {s: storage.styled_path(event_id, s, photo_id, "preview") for s in styles.STYLES})
            del rendered
        with db.connect() as conn:
            row = conn.execute("SELECT id FROM photos WHERE id = %s AND status = 'done' FOR UPDATE", (photo_id,)).fetchone()
            if row is None:
                log.info("photo %s was deleted or reset during styling", photo_id)
                return False
            for tmp, dest in temps.items():
                storage.place(tmp, dest)
            conn.execute("""UPDATE photos SET styles_version = %s, styles_error = NULL, styles_attempts = 0,
                                   styles_locked_at = NULL, updated_at = clock_timestamp() WHERE id = %s""",
                         (styles.STYLE_VERSION, photo_id))
        log.info("photo %s styled: ms=%s", photo_id, round((time.perf_counter() - started) * 1000))
        return True
    except Exception as exc:
        permanent = isinstance(exc, PermanentFailure)
        log.warning("photo %s styling failed (attempt %s): %s", photo_id, photo["styles_attempts"],
                    str(exc) if permanent else type(exc).__name__)
        _styles_failed(photo, str(exc) if permanent else f"Не удалось сделать стили ({type(exc).__name__}).", permanent)
    finally:
        for tmp in temps:
            tmp.unlink(missing_ok=True)
    return False


def _styles_failed(photo: dict, message: str, permanent: bool) -> None:
    try:
        with db.connect() as conn:
            attempts = MAX_ATTEMPTS if permanent else photo["styles_attempts"]
            delay = BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1]
            conn.execute("""UPDATE photos SET styles_error = %s, styles_attempts = %s, styles_locked_at = NULL,
                                   styles_next_at = now() + make_interval(secs => %s) WHERE id = %s""",
                         (message, attempts, delay, photo["id"]))
    except db.DatabaseUnavailable:
        pass  # the lock goes stale and the photo is picked up again


def child_setup() -> None:
    """Called first thing in the styling child process (app.stylejob): lowest CPU priority we use, and first in line for the kernel's
    out-of-memory killer, so a memory spike can end only this one job - never the web server or
    the upload worker."""
    try:
        os.nice(STYLE_NICE)
    except OSError:
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("1000")
    except OSError:
        pass  # not Linux


def style_in_child(photo: dict) -> bool:
    """Style one photo in a separate short-lived process (python -m app.stylejob <id>). Its memory
    is returned to the system as soon as it ends; if it is killed or hangs, only this attempt fails."""
    with HEAVY:
        try:
            done = subprocess.run([sys.executable, "-m", "app.stylejob", str(photo["id"])], cwd=APP_ROOT,
                                  timeout=STYLE_TIMEOUT_S, check=False)
            code = done.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    if code == 0:
        return True
    if code != 1:   # 1 = the child already recorded its own failure
        log.warning("photo %s styling process ended abnormally (%s)", photo["id"], code)
        _styles_failed(photo, "Обработка стилей прервалась (не хватило памяти или времени); будет повторена.", False)
    return False


def retry_styles(event_id: int | None = None) -> int:
    """Give photos whose styling failed MAX_ATTEMPTS times a fresh set of attempts."""
    with db.connect() as conn:
        cur = conn.execute(f"""UPDATE photos SET styles_attempts = 0, styles_next_at = now(), styles_locked_at = NULL
                               WHERE styles_version < %s AND styles_attempts >= {MAX_ATTEMPTS}
                                 AND (%s::int IS NULL OR event_id = %s)""",
                           (styles.STYLE_VERSION, event_id, event_id))
        return cur.rowcount


def run_pending_styles() -> int:
    """Style until nothing is left (used by tests and tools). Returns photos claimed."""
    claimed = 0
    while (photo := claim_styles()) is not None:
        claimed += 1
        process_styles(photo)
    return claimed


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
        count = cur.rowcount
    retry_styles(event_id)   # the organiser's "retry" also retries failed styled versions
    return count


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

    def styles_loop():
        while not stop.is_set():
            if not styles.retouch.SEG_PATH.exists():
                log.error("styles paused: segmentation model %s is missing", styles.retouch.SEG_PATH.name)
                stop.wait(600)
                continue
            if storage.free_disk_mb() < settings.min_free_disk_mb + STYLE_FREE_DISK_MB:
                log.warning("styles paused: not enough free disk space")
                stop.wait(300)
                continue
            try:
                photo = claim_styles()
            except db.DatabaseUnavailable:
                stop.wait(5)
                continue
            if photo is None:
                stop.wait(5)
                continue
            style_in_child(photo)

    threads = [threading.Thread(target=loop, name=f"photo-{i}") for i in range(settings.worker_threads)]
    if settings.style_threads:
        threads.append(threading.Thread(target=styles_loop, name="styles"))
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
