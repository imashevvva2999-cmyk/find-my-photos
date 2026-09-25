"""Receiving event photos.

For every file: copy it to data/incoming/<random>.part with a size cap, check format,
pixel count and structure (images.inspect), then insert the database row and move the
file into place inside one transaction, so the worker never sees a row without its file.
Any failure removes the temporary file. Every file gets its own result.
A file identical (byte for byte) to one already in the event is refused, so an event never
holds the same image twice, whatever the file names.
"""
import hashlib
import logging
import uuid
from pathlib import Path

from . import db, images, storage
from .config import ALLOWED_EXTENSIONS, settings

log = logging.getLogger("uploads")
CHUNK = 1 << 20


class EventMissing(Exception):
    pass


def disk_has_room() -> bool:
    return storage.free_disk_mb() >= settings.min_free_disk_mb


def clean_name(filename: str | None) -> str:
    name = Path(filename or "photo").name.replace("\x00", "").strip() or "photo"
    return name[:200]


def _insert_photo(conn, event_id: int, name: str, file_name: str, content_sha256: str) -> int | None:
    """New photo row, or None if the same file is already in this event."""
    event = conn.execute("SELECT id FROM events WHERE id = %s FOR SHARE", (event_id,)).fetchone()
    if event is None:
        raise EventMissing()
    row = conn.execute(
        """INSERT INTO photos (event_id, original_name, file_name, content_sha256) VALUES (%s, %s, %s, %s)
           ON CONFLICT (event_id, content_sha256) WHERE content_sha256 IS NOT NULL DO NOTHING RETURNING id""",
        (event_id, name, file_name, content_sha256)).fetchone()
    return row["id"] if row else None


def save_upload(event_id: int, filename: str | None, stream) -> dict:
    """Store one uploaded file. Returns {"accepted": {...}} or {"rejected": {...}}.
    Raises EventMissing if the event no longer exists. Runs in a worker thread."""
    name = clean_name(filename)
    ext = Path(name).suffix.lower()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if ext not in ALLOWED_EXTENSIONS:
        return {"rejected": {"name": name, "reason": "Неподдерживаемый тип файла (нужен JPG, PNG, WEBP или HEIC)."}}

    part = storage.new_incoming_path()
    dest = None
    try:
        size = 0
        digest = hashlib.sha256()
        with open(part, "wb") as out:
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    return {"rejected": {"name": name, "reason": f"Больше {settings.max_upload_mb} МБ."}}
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            return {"rejected": {"name": name, "reason": "Файл пустой."}}
        images.inspect(part, settings.max_event_pixels, filename=name)

        file_name = uuid.uuid4().hex + ext
        dest = storage.original_path(event_id, file_name)
        with db.connect() as conn:
            photo_id = _insert_photo(conn, event_id, name, file_name, digest.hexdigest())
            if photo_id is None:
                dest = None
                return {"rejected": {"name": name, "reason": "Уже есть в этом мероприятии (такая же фотография была загружена раньше)."}}
            storage.place(part, dest)  # before commit: a committed row always has its file
        return {"accepted": {"id": photo_id, "name": name}}
    except images.ImageRejected as exc:
        return {"rejected": {"name": name, "reason": str(exc)}}
    except EventMissing:
        raise
    except Exception as exc:
        log.error("upload failed: event=%s error=%s", event_id, type(exc).__name__)
        if dest is not None:
            dest.unlink(missing_ok=True)
        return {"rejected": {"name": name, "reason": "Не удалось сохранить. Попробуйте ещё раз."}}
    finally:
        part.unlink(missing_ok=True)
