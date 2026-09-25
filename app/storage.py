"""Where photo files live on disk.

data/events/<event_id>/originals/<random>.<ext>   the uploaded file (admin only)
data/events/<event_id>/display/<photo_id>.jpg     metadata-free copy for visitor downloads (max 4096 px)
data/events/<event_id>/previews/<photo_id>.jpg    shown when a photo is opened (2048 px)
data/events/<event_id>/thumbs/<photo_id>.jpg      galleries (480 px)
data/incoming/<random>.part                       uploads being received (deleted or moved at once)
"""
import os
import shutil
import uuid
from pathlib import Path

from .config import settings

DISPLAY_SIZE = 4096
PREVIEW_SIZE = 2048
THUMB_SIZE = 480


def events_root() -> Path:
    return settings.data_dir / "events"


def event_dir(event_id: int) -> Path:
    return events_root() / str(int(event_id))


def incoming_dir() -> Path:
    path = settings.data_dir / "incoming"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_incoming_path() -> Path:
    return incoming_dir() / f"{uuid.uuid4().hex}.part"


def original_path(event_id: int, file_name: str) -> Path:
    return event_dir(event_id) / "originals" / Path(file_name).name


def display_path(event_id: int, photo_id: int) -> Path:
    return event_dir(event_id) / "display" / f"{int(photo_id)}.jpg"


def preview_path(event_id: int, photo_id: int) -> Path:
    return event_dir(event_id) / "previews" / f"{int(photo_id)}.jpg"


def thumb_path(event_id: int, photo_id: int) -> Path:
    return event_dir(event_id) / "thumbs" / f"{int(photo_id)}.jpg"


def derived_paths(event_id: int, photo_id: int) -> list[Path]:
    return [display_path(event_id, photo_id), preview_path(event_id, photo_id), thumb_path(event_id, photo_id)]


def place(src: Path, dest: Path) -> None:
    """Move a finished file into place atomically (same file system)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)


def delete_photo_files(event_id: int, photo_id: int, file_name: str) -> None:
    for path in (original_path(event_id, file_name), *derived_paths(event_id, photo_id)):
        path.unlink(missing_ok=True)


def delete_event_files(event_id: int) -> None:
    """Remove the whole event folder. Errors are raised (and logged by the caller), not hidden;
    anything left behind is removed later by the orphan sweep."""
    folder = event_dir(event_id)
    if folder.exists():
        shutil.rmtree(folder)


def free_disk_mb() -> int:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(settings.data_dir).free // (1024 * 1024)
