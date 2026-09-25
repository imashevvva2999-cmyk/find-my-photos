"""Add every photo in a folder to an event, with the same checks as the admin upload page
(format, size, pixel count, and no image twice in one event). The worker then finds the faces.

    .venv/bin/python scripts/import_folder.py --new-event "Event name" "path/to/folder"
    .venv/bin/python scripts/import_folder.py --event-id 7 "path/to/folder"

Prints the new event's visitor link path. Files stay where they are; copies are stored in data/.
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, uploads  # noqa: E402
from app.config import ALLOWED_EXTENSIONS, settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--new-event", metavar="NAME")
    target.add_argument("--event-id", type=int)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()

    files = sorted(p for p in args.folder.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS)
    if not files:
        raise SystemExit(f"No photos found in {args.folder}")
    db.init()
    try:
        with db.connect() as conn:
            if args.new_event:
                row = conn.execute("""
                    INSERT INTO events (name, token, expires_at) VALUES (%s, %s, now() + make_interval(days => %s))
                    RETURNING id, token""", (args.new_event.strip()[:120], secrets.token_urlsafe(16),
                                             settings.event_retention_days)).fetchone()
            else:
                row = conn.execute("SELECT id, token FROM events WHERE id = %s", (args.event_id,)).fetchone()
                if row is None:
                    raise SystemExit(f"No event with id {args.event_id}")
        accepted, rejected = 0, []
        for i, path in enumerate(files, 1):
            if not uploads.disk_has_room():
                raise SystemExit("Stopped: the disk is almost full.")
            with open(path, "rb") as stream:
                result = uploads.save_upload(row["id"], path.name, stream)
            if "accepted" in result:
                accepted += 1
            else:
                rejected.append(result["rejected"])
            if i % 100 == 0 or i == len(files):
                print(f"  {i}/{len(files)} files read, {accepted} added", flush=True)
        print(f"Event {row['id']}: {accepted} photos added, {len(rejected)} not added.")
        for item in rejected:
            print(f"  not added: {item['name']}: {item['reason']}")
        print(f"Visitor link: /e/{row['token']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
