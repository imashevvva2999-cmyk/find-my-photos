# Find My Photos — event face search

An organiser uploads event photos. A guest opens the event's private link, **takes a selfie
or uploads a photo**, clicks **Find my photos**, and sees the event photos they **may**
appear in (including group photos), with the time the search took.

Runs on this Mac. Nothing is published to the internet.

## Run it

```bash
cd ~/Desktop/"day 1"
./run.sh
```

`run.sh` installs the exact library versions (only when `requirements.txt` changes), verifies the
face models by checksum, prepares secrets in `.env`, starts the project's own PostgreSQL server,
applies database migrations, and starts **two programs**: the web server and the background
worker. Stop both with **Ctrl+C**. The database keeps running (`./scripts/postgres.sh stop`).

- Admin area: <http://127.0.0.1:8000/admin>
- Health check: <http://127.0.0.1:8000/readyz>

Requirements: macOS with `python3`, `curl` and **Postgres.app** (<https://postgresapp.com>).

### Admin password

Only a scrypt **hash** is stored (`ADMIN_PASSWORD_HASH` in `.env`). To set a new password:

```bash
.venv/bin/python scripts/set_admin_password.py
```

Changing the password, or clicking **Log out**, ends every admin session on every device
(a copied session cookie stops working too). Sessions also end 12 hours after login. After
5 wrong attempts from one address, that address is blocked for 15 minutes; a flood of failures
from many addresses only slows every login down (so the real admin is not locked out).

## Use it

1. **Organiser:** log in, create an event, drag photos into **Upload photos** (JPG, PNG, WEBP,
   HEIC, up to 30 MB each). Processing runs in the background; the **Processing** card shows
   progress, failed photos (with **Retry failed photos**), and three timers: upload time,
   upload + processing until searchable, and face-processing time on the server.
2. **Share** the private link (**Copy**). **Create new link** revokes the old one at once.
3. **Guest:** open the link, choose **Take a photo** (camera) or **Upload a photo**, tick the
   consent box, click **Find my photos**. The timer runs until the results are on screen and
   shows how the time was spent. Results are *possible matches*; **Download** gives a copy
   without metadata (no GPS or camera data).

## How it works

| Part | File | Role |
|---|---|---|
| Web server | `app/main.py` | Routes; blocking work runs in thread pools; searches are admitted by a bounded gate |
| Worker process | `app/worker.py` | Claims photos from a PostgreSQL queue (`FOR UPDATE SKIP LOCKED`), retries with backoff, reclaims stuck photos, heartbeat, runs maintenance every 15 min |
| Face engine | `app/faces.py` | OpenCV YuNet (detection) + SFace (128-number embeddings) |
| Safe images | `app/images.py` | JPEG/PNG/WEBP/HEIC only, pixel limit checked before decoding, metadata-free copies |
| Uploads | `app/uploads.py` | Per-file validation, size cap while copying, DB row + file committed together |
| Matching | `app/matching.py` | Per-event embedding matrix cached in memory, reloaded when `index_version` changes |
| Security | `app/security.py` | Admin session with password version, same-origin POST check, DB-backed rate limits |
| Retention | `app/maintenance.py` | Deletes expired events, orphaned files, old counters and statistics |
| Logging, headers | `app/observability.py` | Redacted request log, CSP/HSTS headers, request size limits |
| Database | `app/db.py`, `app/migrate.py`, `app/migrations/` | Checked connection pool; versioned migrations |

### Face matching

Search compares the guest's face with every face of the event (cosine similarity) and shows
photos above `MATCH_THRESHOLD` (default **0.363**, the value the SFace authors publish for
one-to-one verification), best first, at most `MAX_RESULTS` (60). This threshold has **not
been calibrated** for searching a whole event, and no labelled test set of real event photos
exists yet, so the site makes **no accuracy or identity claims**: every result is labelled
"Possible match" ("closer" at ≥ 0.5, otherwise "weaker — please check").

## Privacy and retention rules

| Data | Kept | Deleted |
|---|---|---|
| Event photos (original, preview, thumbnail, download copy) | until the event expires | automatically at expiry (default **90 days** after creation; the admin can set 1–365 days), or when the admin deletes the photo/event |
| Face data (embeddings) | with its photo | together with the photo/event |
| Guest selfie (camera or upload) | **only in memory** during one search | immediately; never written to disk, database or logs; also removed from the page after the search |
| Result links | 120 minutes (`RESULT_LINK_MINUTES`) | expire; revoked at once by a new event link, turning search off, or deleting the event |
| Search statistics | 30 days | outcome, number of results, duration only — no IP, no image |
| Rate-limit counters | 1 day | hashed IP, no raw address |
| Orphaned files (e.g. after a crash) | — | removed after 1 hour |

Logs never contain images, face data, passwords, event tokens or signed links (paths are
logged as `/e/<redacted>/…`). Note: deleted rows can remain in PostgreSQL's write-ahead log
and in backups until those are rotated.

## Settings (environment variables / `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | set by setup | App login to PostgreSQL |
| `SECRET_KEY` | generated | Root secret (session and link keys are derived from it) |
| `ADMIN_PASSWORD_HASH` | generated | scrypt hash |
| `COOKIE_SECURE` | `false` | Set `true` behind HTTPS (Secure cookie + HSTS) |
| `EVENT_RETENTION_DAYS` | `90` | Default event lifetime |
| `RESULT_LINK_MINUTES` | `120` | Result link lifetime |
| `MATCH_THRESHOLD` / `MAX_RESULTS` | `0.363` / `60` | Matching cut-off and result cap |
| `MAX_UPLOAD_MB` / `MAX_SELFIE_MB` | `30` / `15` | File size limits |
| `SEARCH_CONCURRENCY` / `SEARCH_QUEUE` | `2` / `64` | Parallel searches / waiting searches before "busy" |
| `SEARCHES_PER_10_MIN` | `30` | Per guest address and event |
| `WORKER_THREADS` | `2` | Photos processed in parallel |
| `MIN_FREE_DISK_MB` | `1024` | Uploads pause below this |

## Test it

```bash
.venv/bin/pip install -r requirements-dev.txt        # once
.venv/bin/python -m playwright install chromium webkit   # once, for browser tests
.venv/bin/python -m pytest                            # unit, integration and browser tests
.venv/bin/python -m pytest -m e2e tests/e2e -v        # browser tests only (fake camera)
.venv/bin/python tests/load/make_dataset.py 500 2000  # synthetic load-test photos (once)
.venv/bin/python tests/load/run_load.py               # load test on an isolated copy
```

All tests use separate databases (`findmyphotos_test`, `findmyphotos_e2e`, `findmyphotos_load`)
and temporary folders; the real site, its data and `photos/` are never touched. Test photos are
NASA public-domain images (`test_data/`) and MIT-licensed repository samples (`sample_photos/`).

## Before a real deployment (not done)

- A domain with **HTTPS** behind a reverse proxy (phones only allow the camera on https pages);
  set `COOKIE_SECURE=true` and start uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>`.
- A privacy notice and consent text reviewed for your jurisdiction (face data is biometric data,
  e.g. GDPR Art. 9), and a named contact for deletion requests.
- Backups of the database and `data/events/` that follow the retention rules.
- Monitoring of `/readyz`, disk space and the worker heartbeat.
- A labelled set of real event photos to calibrate `MATCH_THRESHOLD`.
- Decisions: individual admin accounts, default retention period, whether uploaded (non-camera)
  selfies should be allowed for every event.
