"""Moving an event's photo files onto this server's disk (e.g. from the organiser's computer).

Switched off unless IMPORT_TOKEN_SHA256 is set (the SHA-256 of a long random token); set it
only for the duration of a transfer, then remove it again. The files are sent as plain tar
archives. Only regular files at events/<id>/<originals|display|previews|thumbs>/<name> are
accepted; anything else (links, other folders, "..") is refused. Existing files with the same
size are left alone, so an interrupted transfer can simply be sent again.
"""
import hashlib
import hmac
import logging
import os
import re
import tarfile
from pathlib import Path

from . import storage
from .config import settings

log = logging.getLogger("importer")

PATH = "/internal/import-files"
MAX_ARCHIVE_BYTES = 2 * 1024 ** 3
_MEMBER = re.compile(r"^events/(\d+)/(originals|display|previews|thumbs)/([A-Za-z0-9][A-Za-z0-9._-]{0,120})$")


def enabled() -> bool:
    return bool(settings.import_token_sha256)


def authorised(header: str) -> bool:
    if not enabled() or not header.startswith("Bearer "):
        return False
    digest = hashlib.sha256(header[len("Bearer "):].strip().encode()).hexdigest()
    return hmac.compare_digest(digest, settings.import_token_sha256)


def extract(archive: Path) -> dict:
    root = settings.data_dir.resolve()
    written = skipped = 0
    refused: list[str] = []
    with tarfile.open(archive, mode="r:") as tar:
        for member in tar:
            match = _MEMBER.match(member.name)
            if not member.isfile() or not match:
                if not member.isdir():
                    refused.append(member.name[:200])
                continue
            dest = (root / member.name).resolve()
            if root not in dest.parents:
                refused.append(member.name[:200])
                continue
            if dest.exists() and dest.stat().st_size == member.size:
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = storage.new_incoming_path()
            with tar.extractfile(member) as src, open(part, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            os.replace(part, dest)
            written += 1
    log.info("import: %d written, %d already present, %d refused", written, skipped, len(refused))
    return {"written": written, "skipped": skipped, "refused": refused[:20]}


def inventory(event_id: int) -> dict:
    """{relative path: size} of every file of one event, to check a transfer is complete."""
    folder = storage.event_dir(event_id)
    root = settings.data_dir.resolve()
    if not folder.exists():
        return {}
    return {str(p.resolve().relative_to(root)): p.stat().st_size for p in folder.rglob("*") if p.is_file()}
