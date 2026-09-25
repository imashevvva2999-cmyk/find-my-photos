"""Comparing a visitor's face with every face of an event.

The event's embeddings are held in memory as a NumPy matrix. Each search first reads the
event's index_version (one small query). When it changed:
- if faces were only ADDED (photos still being processed), just the new rows are fetched;
- if faces were removed or replaced, the whole event is reloaded.
The comparison is exact (matrix x vector), not an approximation.

Face data of deleted events is dropped at once (forget), and events not searched for
15 minutes are dropped automatically.
"""
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from . import db

CACHE_EVENTS = 16          # events kept in memory (about 0.5 KB per face)
IDLE_SECONDS = 15 * 60     # events not searched for this long are dropped from memory


@dataclass
class _Entry:
    version: int
    max_face_id: int
    ids: np.ndarray        # photo id for each row
    matrix: np.ndarray     # one embedding per row
    used: float


_cache: "OrderedDict[int, _Entry]" = OrderedDict()
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def forget(event_id: int) -> None:
    """Drop an event's face data from memory (called when the event is deleted)."""
    with _lock:
        _cache.pop(event_id, None)


def _to_matrix(rows) -> tuple[np.ndarray, np.ndarray, int]:
    ids = np.array([r["photo_id"] for r in rows], dtype=np.int64)
    matrix = (np.frombuffer(b"".join(bytes(r["embedding"]) for r in rows), dtype=np.float32).reshape(len(rows), -1)
              if rows else np.zeros((0, 128), dtype=np.float32))
    return ids, matrix, max((r["id"] for r in rows), default=0)


def _fetch(event_id: int, after_face_id: int) -> tuple[int, list, int]:
    """One consistent snapshot: index version, face rows with id > after_face_id, total count."""
    with db.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        version = conn.execute("SELECT index_version FROM events WHERE id = %s", (event_id,)).fetchone()
        rows = conn.execute("""
            SELECT f.id, f.photo_id, f.embedding FROM faces f JOIN photos p ON p.id = f.photo_id
            WHERE f.event_id = %s AND p.status = 'done' AND f.id > %s ORDER BY f.id
        """, (event_id, after_face_id)).fetchall()
        total = conn.execute("""
            SELECT COUNT(*) AS n FROM faces f JOIN photos p ON p.id = f.photo_id
            WHERE f.event_id = %s AND p.status = 'done'
        """, (event_id,)).fetchone()["n"]
    return (version["index_version"] if version else -1), rows, total


def _event_matrix(event_id: int, version: int) -> tuple[np.ndarray, np.ndarray]:
    now = time.monotonic()
    with _lock:
        for stale in [k for k, e in _cache.items() if now - e.used > IDLE_SECONDS]:
            del _cache[stale]
        entry = _cache.get(event_id)
        if entry and entry.version == version:
            entry.used = now
            _cache.move_to_end(event_id)
            return entry.ids, entry.matrix

    new_version, rows, total = _fetch(event_id, entry.max_face_id if entry else 0)
    new_ids, new_matrix, new_max = _to_matrix(rows)
    if entry and total == len(entry.ids) + len(rows):          # only additions: append
        ids = np.concatenate([entry.ids, new_ids])
        matrix = np.vstack([entry.matrix, new_matrix])
        max_id = max(entry.max_face_id, new_max)
    elif entry:                                                  # removals/replacements: reload
        new_version, rows, total = _fetch(event_id, 0)
        ids, matrix, max_id = _to_matrix(rows)
    else:
        ids, matrix, max_id = new_ids, new_matrix, new_max
    with _lock:
        _cache[event_id] = _Entry(new_version, max_id, ids, matrix, now)
        _cache.move_to_end(event_id)
        while len(_cache) > CACHE_EVENTS:
            _cache.popitem(last=False)
    return ids, matrix


def search(event_id: int, version: int, embedding: np.ndarray, threshold: float, limit: int) -> tuple[list[tuple[int, float]], int]:
    """Return ([(photo_id, best score)] best first, at most `limit`), number of faces compared."""
    ids, matrix = _event_matrix(event_id, version)
    if len(ids) == 0:
        return [], 0
    scores = matrix @ embedding.astype(np.float32)
    best: dict[int, float] = {}
    for idx in np.flatnonzero(scores >= threshold):
        photo_id, score = int(ids[idx]), float(scores[idx])
        if score > best.get(photo_id, -1.0):
            best[photo_id] = score
    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)[:limit]
    return ranked, len(ids)
