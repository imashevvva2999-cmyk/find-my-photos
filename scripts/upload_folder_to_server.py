"""Upload every photo in a local folder to an event on the online server, through the normal
organiser API (same checks as the admin page: format, size, duplicates). Then wait until the
server has found the faces in all of them.

    .venv/bin/python scripts/upload_folder_to_server.py --server https://example.onrender.com \
        --password-file data/.deploy-admin-password --event-name "Технокадр" "photo 2"

Re-running is safe: photos already in the event are refused as duplicates, so an interrupted
upload can simply be started again (use --event-id to continue the same event).
The password is read from a file and never printed.
"""
import argparse
import re
import sys
import time
from pathlib import Path

import httpx

BATCH = 5  # MAX_FILES_PER_REQUEST on the server
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", required=True)
    parser.add_argument("--password-file", required=True, type=Path)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--event-name")
    target.add_argument("--event-id", type=int)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()

    server = args.server.rstrip("/")
    files = sorted(p for p in args.folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS)
    if not files:
        raise SystemExit(f"No photos in {args.folder}")
    client = httpx.Client(base_url=server, headers={"Origin": server}, timeout=httpx.Timeout(120, connect=20))

    r = client.post("/admin/login", data={"password": args.password_file.read_text().strip()})
    if r.status_code != 303:
        raise SystemExit(f"Login failed (HTTP {r.status_code}). Check ADMIN_PASSWORD_HASH on the server.")

    if args.event_id:
        event_id = args.event_id
    else:
        r = client.post("/admin/events", data={"name": args.event_name})
        match = re.search(r"/admin/events/(\d+)$", r.headers.get("location", ""))
        if r.status_code != 303 or not match:
            raise SystemExit(f"Could not create the event (HTTP {r.status_code}).")
        event_id = int(match.group(1))
    print(f"Event {event_id}: uploading {len(files)} photos in batches of {BATCH}…", flush=True)

    accepted, duplicates, rejected = 0, 0, []
    started = time.time()
    for i in range(0, len(files), BATCH):
        batch = files[i:i + BATCH]
        for attempt in range(5):
            handles = [open(p, "rb") for p in batch]
            try:
                r = client.post(f"/admin/api/events/{event_id}/photos",
                                files=[("files", (p.name, h, "image/jpeg")) for p, h in zip(batch, handles)])
                if r.status_code < 500:
                    break
            except httpx.HTTPError as exc:
                r = None
                print(f"  network problem ({type(exc).__name__}), retrying…", flush=True)
            finally:
                for h in handles:
                    h.close()
            time.sleep(3 * (attempt + 1))
        if r is None or r.status_code != 200:
            raise SystemExit(f"Upload stopped at photo {i + 1} (HTTP {getattr(r, 'status_code', '-')}). "
                             f"Run again with --event-id {event_id} to continue.")
        body = r.json()
        accepted += len(body["accepted"])
        for item in body["rejected"]:
            if "Уже есть" in item["reason"]:
                duplicates += 1
            else:
                rejected.append(item)
        done = min(i + BATCH, len(files))
        if done % 50 < BATCH or done == len(files):
            rate = done / max(1e-6, time.time() - started)
            print(f"  {done}/{len(files)} sent · {accepted} new · {duplicates} already there · "
                  f"{len(rejected)} refused · {rate:.1f} photos/s", flush=True)

    for item in rejected:
        print(f"  refused: {item['name']}: {item['reason']}")
    print("Waiting for the server to find the faces…", flush=True)
    last = None
    while True:
        s = client.get(f"/admin/api/events/{event_id}/status").json()
        line = f"  processed {s['done'] + s['error']}/{s['total']} · faces {s['faces']} · errors {s['error']}"
        if line != last:
            print(line, flush=True)
            last = line
        if s["total"] and s["done"] + s["error"] >= s["total"]:
            break
        time.sleep(15)
    page = client.get(f"/admin/events/{event_id}").text
    link = re.search(r'id="visitor-link"[^>]*value="([^"]+)"', page) or re.search(r'value="([^"]+/e/[^"]+)"', page)
    print(f"Done: {s['done']} photos ready, {s['faces']} faces.")
    if link:
        print(f"Visitor link: {link.group(1)}")


if __name__ == "__main__":
    sys.exit(main())
