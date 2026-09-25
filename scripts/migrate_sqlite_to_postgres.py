"""One-time copy of the old SQLite database (data/app.db) into PostgreSQL.

Keeps every event, photo and face with the same IDs, checks that everything arrived
unchanged, and only then moves the SQLite files to data/sqlite-backup/ (nothing is deleted).
Refuses to run if PostgreSQL already contains events.

Usage (with PostgreSQL running, the web app stopped):
    .venv/bin/python scripts/migrate_sqlite_to_postgres.py
"""
import hashlib
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from psycopg.rows import tuple_row  # noqa: E402

from app import db  # noqa: E402

SQLITE = ROOT / "data" / "app.db"
BACKUP = ROOT / "data" / "sqlite-backup"


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def fingerprint(rows) -> str:
    """Hash of all face data, to prove the copy is byte-for-byte identical."""
    h = hashlib.sha256()
    for r in rows:
        h.update(repr((r[0], r[1], r[2], r[3], r[4], r[5], r[6], round(r[7], 4))).encode())
        h.update(bytes(r[8]))
    return h.hexdigest()


def main() -> None:
    if not SQLITE.exists():
        sys.exit("No data/app.db found - nothing to migrate.")
    src = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    db.init()  # creates the PostgreSQL tables if needed

    with db.connect() as conn:
        if conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]:
            sys.exit("PostgreSQL already has events - refusing to migrate twice.")

        events = src.execute("SELECT id, name, token, is_open, created_at FROM events ORDER BY id").fetchall()
        photos = src.execute("SELECT id, event_id, original_name, file_name, status, error, face_count, created_at "
                             "FROM photos ORDER BY id").fetchall()
        faces_sql = "SELECT id, photo_id, event_id, x, y, w, h, score, embedding FROM faces ORDER BY id"
        faces = src.execute(faces_sql).fetchall()

        cur = conn.cursor()
        cur.executemany("INSERT INTO events (id, name, token, is_open, created_at) VALUES (%s, %s, %s, %s, %s)",
                        [(i, n, t, bool(o), ts(c)) for i, n, t, o, c in events])
        cur.executemany("INSERT INTO photos (id, event_id, original_name, file_name, status, error, face_count, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(*p[:7], ts(p[7])) for p in photos])
        cur.executemany("INSERT INTO faces (id, photo_id, event_id, x, y, w, h, score, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", faces)
        # New rows must get IDs after the copied ones.
        for table in ("events", "photos", "faces"):
            conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                         f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")

        # Verify before committing.
        counts = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in ("events", "photos", "faces")}
        expected = {"events": len(events), "photos": len(photos), "faces": len(faces)}
        copied = conn.cursor(row_factory=tuple_row).execute(faces_sql).fetchall()
        if counts != expected or fingerprint(copied) != fingerprint(faces):
            raise SystemExit(f"Verification FAILED, nothing saved: {counts} vs {expected}")
        print(f"Copied and verified: {counts} (face data identical)")

    src.close()
    BACKUP.mkdir(parents=True, exist_ok=True)
    for f in SQLITE.parent.glob("app.db*"):
        shutil.move(str(f), BACKUP / f.name)
    print(f"Old SQLite files moved to {BACKUP.relative_to(ROOT)}/ (kept as a backup)")


if __name__ == "__main__":
    main()
