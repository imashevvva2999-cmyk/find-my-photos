# Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> Executed inline (superpowers:executing-plans) in the session that wrote it. No git repository exists and the owner has not asked for commits, so "commit" steps are replaced by "run the test suite"; the pre-change snapshot lives outside the project for diffing.

**Goal:** Turn the "Find My Photos" MVP into production-quality code without changing the user flow (admin uploads → visitor takes/uploads a selfie → "Find my photos" → possible matches + search time).

**Architecture:** FastAPI web process + a separate worker process that claims photo jobs from PostgreSQL (`FOR UPDATE SKIP LOCKED`). Versioned SQL migrations, a validated settings object, safe image decoding shared by uploads and selfies, an in-process per-event embedding cache for search, a maintenance loop (retention purge, orphan sweep, pruning) in the worker, structured logs with secret redaction, and health endpoints.

**Tech Stack:** Python 3.12, FastAPI/Starlette, psycopg 3 + psycopg_pool, PostgreSQL 17 (Postgres.app binaries, project-local cluster), OpenCV YuNet + SFace, Pillow + pillow-heif, NumPy; pytest; Playwright (Chromium, fake camera) for E2E; psutil for load-test resource sampling.

**Spec:** The owner's request (production requirements list) + the baseline review (31 findings) summarised in "Findings addressed" below.

## Global Constraints

- Do not deploy, and never upload the owner's photos (`photos/`) to any external service. Load tests use synthetic data derived from permitted NASA photos in `test_data/` only, against an isolated database and data directory.
- Credentials only in environment variables / `.env` (mode 600). No secrets in code or logs.
- Never log image bytes, embeddings, event tokens, signed photo keys or passwords.
- Keep the user flow and URLs: `/admin`, `/admin/events/{id}`, `/e/{token}`, `POST /e/{token}/search`, `/e/{token}/photo/{key}`.
- Results are always labelled "possible match"; no accuracy claims without a labelled test set.
- Visitor selfies are never written to disk or the database.
- Existing data (events 1, 3, 5 and their files) must survive the migration.

---

## Findings addressed (baseline review, verified against code)

| # | Sev | Finding | Task |
|---|---|---|---|
| 1 | High | Unauthenticated upload body is fully parsed/spooled before the auth dependency runs (verified in fastapi/routing.py:430 vs 481) | 4, 5 |
| 2 | High | `load_image` decodes any Pillow format with no pixel limit (decompression bomb) | 3 |
| 3 | High | Sync DB/disk/numpy work inside `async def` search/upload blocks the event loop | 5, 7 |
| 4 | High | No retention/deletion rules for photos, embeddings, events | 9 |
| 5 | High | Any photo of anyone can be used to find them (1:N with no result cap) | 7 (cap, logs), decision Q |
| 6 | High | No brute-force protection on admin login; plaintext password | 4 |
| 7 | Med | Session cookie not Secure, not revocable | 4 |
| 8 | Med | In-process queue breaks with >1 process; duplicate processing | 6 |
| 9 | Med | Transient failures leave photos stuck; no retry | 6 |
| 10 | Med | Delete-during-processing leaves orphan files | 6, 9 |
| 11 | Med | Upload writes file before DB row; batch-level failure reporting | 5 |
| 12 | Med | Every search loads all embeddings | 7 |
| 13 | Med | Status poll returns all photos, polls forever | 8 |
| 14 | Med | In-memory, unbounded, per-process visitor rate limiter; unbounded search queue | 4, 7 |
| 15 | Med | Visitors download originals with EXIF/GPS | 6, 7 |
| 16 | Med | Access logs contain event tokens and signed keys; no logging config | 10 |
| 17 | Med | No health checks / worker liveness | 10 |
| 18 | Med | No migrations; DDL at startup | 2 |
| 19 | Med | Pool without connection check; no PoolTimeout handling; never closed | 2 |
| 20 | Med | `.env` setup not idempotent; superuser secret loaded by app | 1 |
| 21 | Med | Unpinned deps installed each start; model downloads unverified | 1 |
| 22 | Med | Camera can stay on after switching tabs during permission prompt | 8 |
| 23 | Med | Phones cannot use the camera (HTTP, 127.0.0.1); misleading message | 8, decision Q |
| 24 | Med | 1:1 threshold used for 1:N search, not calibrated | 7 (config + cap), docs |
| 25 | Med | Large photos fully decoded several times in worker | 3, 6 |
| 26 | Low | Timer breakdown mislabels upload time; stops before thumbnails | 8 |
| 27 | Low | Missing file → 500 instead of 404 | 7 |
| 28 | Low | CSRF relies only on SameSite; CSP gaps | 4, 10 |
| 29 | Low | Photo delete failures silent | 8 |
| 30 | Low | Only live tests using production secrets | 11 |
| 31 | Low | Source tabs not keyboard accessible | 8 |

---

## File structure

| File | Responsibility |
|---|---|
| `app/config.py` | `Settings` dataclass loaded from env with validation; derived secrets |
| `app/db.py` | Connection pool (checked connections), `connect()`, `PoolTimeout` → 503 |
| `app/migrate.py` + `app/migrations/*.sql` | Versioned migrations with advisory lock; `python -m app.migrate` |
| `app/security.py` | Password hashing (scrypt), session version, Origin check, DB-backed rate limiter |
| `app/images.py` | Safe decode: format allow-list, pixel limits, draft decoding, EXIF-free re-encode |
| `app/faces.py` | Detection + embeddings (unchanged algorithm; uses `images`) |
| `app/uploads.py` | Stream an upload part to disk with a size cap, validate, commit atomically |
| `app/matching.py` | Per-event embedding cache + top-N search |
| `app/worker.py` | Separate process: claim jobs (`SKIP LOCKED`), retries/backoff, heartbeat, maintenance loop |
| `app/maintenance.py` | Retention purge, orphan sweep, pruning of rate-limit and search-log rows |
| `app/observability.py` | Logging setup, redacting access log middleware, security headers, body-size limits |
| `app/main.py` | Routes only |
| `scripts/postgres.sh` | Idempotent setup; superuser secret kept out of `.env` |
| `scripts/set_admin_password.py` | Store a scrypt hash of a new admin password in `.env` |
| `tests/` | pytest suite (isolated DB + temp data dir), `tests/e2e/` Playwright, `tests/load/` load tests |

## Settings (env vars; defaults are for local use)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | required | App login to PostgreSQL |
| `SECRET_KEY` | required (≥32 chars) | Root secret; session and link keys are derived from it with HMAC |
| `ADMIN_PASSWORD_HASH` | required | `scrypt$n$r$p$salt$hash` |
| `DATA_DIR` | `./data` | Photo storage |
| `COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `EVENT_RETENTION_DAYS` | `90` | Default event lifetime; photos + faces deleted after expiry |
| `RESULT_LINK_MINUTES` | `120` | Lifetime of signed result links |
| `MATCH_THRESHOLD` | `0.363` | Cosine similarity cut-off (SFace published value) |
| `MAX_RESULTS` | `60` | Maximum possible matches returned per search |
| `MAX_UPLOAD_MB` / `MAX_SELFIE_MB` | `30` / `15` | Per-file limits |
| `MAX_EVENT_PIXELS` / `MAX_SELFIE_PIXELS` | `80_000_000` / `40_000_000` | Decoded size limits |
| `SEARCH_CONCURRENCY` / `SEARCH_QUEUE` | `2` / `16` | Parallel searches / waiting searches before 503 |
| `SEARCHES_PER_10_MIN` | `30` | Per client IP and event |
| `WORKER_THREADS` | `2` | Photos processed in parallel by the worker |
| `MIN_FREE_DISK_MB` | `1024` | Uploads refused below this |

---

### Task 1: Pinned dependencies, verified models, idempotent environment setup

**Files:** Modify `requirements.txt`, `run.sh`, `scripts/postgres.sh`; Create `requirements-dev.txt`, `scripts/set_admin_password.py`

- [ ] Pin every runtime dependency to the exact version currently installed and tested (`pip freeze` subset). Add `requirements-dev.txt` (pytest, playwright, psutil).
- [ ] `run.sh`: install only when the requirements hash changes (stamp file in `.venv`); verify model SHA-256 (`8f2383e4…fa4` YuNet, `0ba9fbfa…e79` SFace) and refuse on mismatch; run migrations; start web and worker; stop both on Ctrl+C.
- [ ] `postgres.sh setup`: keep the superuser password in `data/.pg-superuser` (600), not `.env`; update `.env` keys in place (no duplicates); if the cluster exists but `DATABASE_URL` is missing, reset the app role password with the stored superuser secret and write it back.
- [ ] `set_admin_password.py`: prompt (or `--generate`), write `ADMIN_PASSWORD_HASH`, remove plaintext `ADMIN_PASSWORD`. `run.sh` converts an existing plaintext password once and tells the owner.
- [ ] Verify: run `./run.sh` twice; `.env` has exactly one of each key; app starts.

### Task 2: Settings, connection pool, migrations

**Files:** Rewrite `app/config.py`, `app/db.py`; Create `app/migrate.py`, `app/migrations/001_initial.sql`, `app/migrations/002_production.sql`; Test `tests/test_migrations.py`

**Produces:** `config.settings` (frozen dataclass), `db.init()`, `db.close()`, `db.connect()`, `migrate.upgrade(url)`, `migrate.pending(url) -> list[str]`

- [ ] Test first: upgrading an empty test database creates all tables; running twice is a no-op; running two upgrades concurrently succeeds (advisory lock); app refuses to start when migrations are pending.
- [ ] `001_initial.sql` = current schema with `IF NOT EXISTS` (adopts existing databases). `002_production.sql` adds: `events.expires_at` (backfilled `created_at + 90 days`), `events.index_version`; `photos.attempts`, `photos.locked_at`, `photos.next_attempt_at`, `photos.updated_at`, index on `(status, next_attempt_at)`; tables `rate_limits(key, window_start, count)`, `worker_heartbeats(name, pid, beat_at, processed)`, `search_log(event_id, at, results, duration_ms, outcome)` (no IP, no image data).
- [ ] Pool: `check=ConnectionPool.check_connection`, `max_size` configurable, `timeout=10`; exception handler maps `PoolTimeout`/`OperationalError` to 503 with a friendly message; closed in lifespan exit.

### Task 3: Safe image decoding

**Files:** Create `app/images.py`; Modify `app/faces.py`; Test `tests/test_images.py`

**Produces:** `images.inspect(path_or_bytes, max_pixels) -> ImageInfo(format, width, height)` (raises `ImageRejected(reason)`), `images.decode(src, max_pixels, target_side) -> PIL.Image (RGB, oriented)`, `images.save_clean_jpeg(img, path, max_side, quality)`

- [ ] Tests first: accepts JPEG/PNG/WEBP/HEIC; rejects GIF/BMP/TIFF/SVG/text, truncated JPEG, an 18 000×10 000 PNG (bomb) *before* decoding, extension/format mismatch; EXIF orientation applied; saved JPEG has no EXIF/GPS.
- [ ] `Image.open(..., formats=["JPEG","PNG","WEBP","HEIF"])`; check `width*height` before `load()`; `warnings` → error for `DecompressionBombWarning`; JPEG `draft("RGB", (target, target))` to cut memory.

### Task 4: Admin authentication, CSRF and rate limiting

**Files:** Create `app/security.py`; Modify `app/main.py`; Test `tests/test_security.py`

**Produces:** `security.verify_password(pw) -> bool`, `security.require_admin(request)`, `security.check_origin` middleware, `security.RateLimiter.hit(key, limit, window_s) -> bool` (PostgreSQL-backed, shared across processes)

- [ ] Tests first: wrong password → 401 and counted; 6th failure from one IP in 15 min → 429 even with the right password; global limit; session invalid after password change (session carries a password-version); cookie `Secure` when `COOKIE_SECURE=true`; POST to `/admin/*` with foreign `Origin` → 403; **unauthenticated upload is rejected before the body is read** (test streams a body and asserts < 1 MB was consumed).
- [ ] Auth for `/admin/api/*` and admin form posts enforced in middleware ahead of body parsing; login uses constant-time scrypt verify.

### Task 5: Upload pipeline

**Files:** Create `app/uploads.py`; Modify `app/main.py` (upload route), `app/storage.py`; Test `tests/test_uploads.py`

**Produces:** `uploads.save_upload(event_id, UploadFile) -> Accepted|Rejected`

- [ ] Tests first: per-file results (good + bad in one batch); file > limit rejected while streaming (never fully buffered); corrupt image rejected; wrong extension rejected; DB insert failure leaves no file; low disk → 507; event deleted meanwhile → rejected, no orphan; request body cap → 413.
- [ ] Route takes `Request`, parses `request.form(max_files=5, max_fields=10, max_part_size=1MB)` after auth; each part streamed in chunks to `data/incoming/<uuid>.part`, validated with `images.inspect`, then DB insert and `os.replace` into place; all blocking work in the thread pool.

### Task 6: Durable background worker

**Files:** Rewrite `app/worker.py`; Modify `app/storage.py`; Test `tests/test_worker.py`

**Produces:** `worker.claim_one() -> Photo|None`, `worker.process(photo)`, `python -m app.worker`

- [ ] Tests first: two claimers never get the same photo; transient error → back to `pending` with backoff and `attempts+1`; after 3 attempts → `error`; permanent decode error → `error` immediately; stale `processing` (locked > 10 min) reclaimed; photo deleted while processing → no files left; admin "retry failed" resets errors; `index_version` bumps when faces change.
- [ ] Writes `display.jpg` (≤ 4096 px, EXIF-free, for visitor downloads), preview (2048) and thumb (480) from one draft decode; temp names renamed only after the row is locked and still exists.
- [ ] Heartbeat row every 10 s; graceful shutdown on SIGTERM/SIGINT (finish current photo).

### Task 7: Search: correctness, isolation, performance

**Files:** Create `app/matching.py`; Modify `app/main.py`; Test `tests/test_search.py`, `tests/test_access.py`

**Produces:** `matching.search(event_id, embedding, threshold, limit) -> list[(photo_id, score)]`

- [ ] Tests first: selfie never written (data dir + temp dir unchanged, no DB row contains it); no face / several faces / too small messages; results capped at `MAX_RESULTS`; cache invalidated when a photo is added/deleted (`index_version`); event A link cannot open event B photo; tampered/expired key → 404; rotated token revokes links; downloads are EXIF-free `display.jpg`; missing file → 404; search 503 when queue is full; searches allowed while indexing is in progress (`waiting_photos` reported).
- [ ] All DB, decode and numpy work off the event loop in a bounded executor; `search_log` row per search (counts + duration only); per-(IP,event) limit via `RateLimiter`.

### Task 8: Front-end robustness (camera, upload, timers, admin)

**Files:** Modify `app/static/event.js`, `app/static/admin.js`, `app/templates/event.html`, `app/templates/admin_event.html`, `app/static/style.css`

- [ ] Camera: drop a stream that arrives after the visitor left the camera tab or hid the page; `isSecureContext` check with a specific message; platform-neutral permission help; capture ≤ 1600 px; clear the selfie/uploaded file from the page after a search; keyboard-accessible tabs (arrow keys, roving tabindex).
- [ ] Timer: breakdown shows upload / face detection / matching / network+display; timer stops when the gallery is rendered and its first thumbnail has loaded (or 3 s cap).
- [ ] Admin: incremental status polling (`since` cursor, counts via SQL), pause when tab hidden; per-file upload errors incl. 413/507; "Retry failed photos" button; delete error handling; expiry date shown and editable.

### Task 9: Retention and deletion

**Files:** Create `app/maintenance.py`; Modify `app/main.py`, templates; Test `tests/test_retention.py`

- [ ] Tests first: expired event → rows, faces and files deleted and links 404; orphan files (no row, older than 1 h) and dirs of deleted events removed; `rate_limits` older than 1 day and `search_log` older than 30 days pruned; admin can change expiry (max 365 days).
- [ ] Rules (documented in README + shown to admin/visitor): event photos and face data kept until the event expires (default 90 days) or is deleted; visitor selfie kept only in memory for the duration of one search; result links valid 120 minutes and revoked immediately when the link is rotated, search closed or the event deleted; search log keeps counts only, 30 days.

### Task 10: Observability and hardening headers

**Files:** Create `app/observability.py`; Modify `app/main.py`; Test `tests/test_observability.py`

- [ ] Tests first: log lines for `/e/<token>/…` and `/photo/<key>` contain `<redacted>`, never the token or key; no log line contains base64/embedding data; `/healthz` 200; `/readyz` 200 with DB up + migrations current + models loaded, 503 otherwise; worker heartbeat age and queue counts reported.
- [ ] Uvicorn access log disabled; JSON-ish structured request log with request id and duration; CSP adds `base-uri 'none'; form-action 'self'`; HSTS when `COOKIE_SECURE=true`; request body size limits per route.

### Task 11: Test suite, E2E and load tests

**Files:** Create `tests/conftest.py`, `tests/e2e/test_browser.py`, `tests/load/make_dataset.py`, `tests/load/run_load.py`; Modify `tests/test_full_flow.py`, `tests/evaluate_40.py`

- [ ] pytest suite runs against an isolated `findmyphotos_test` database and a temporary data dir (never the real one).
- [ ] E2E (Playwright, Chromium): upload flow; camera flow with a fake camera fed from a permitted NASA face video (`--use-file-for-fake-video-capture`); mobile emulation (Pixel 7 viewport + touch) for both flows; permission-denied path.
- [ ] Load: isolated server on port 8100 with its own database/data dir; synthetic collections of 500 and 2 000 photos derived from permitted NASA photos; measure upload+indexing throughput, search p50/p95/p99 at 1/10/25/50 concurrent visitors for 60 s each, error rate, CPU and memory of web, worker and PostgreSQL; searches during indexing.

### Task 12: Second review and fixes

- [ ] Dispatch a fresh reviewer on the changed code; fix every Critical/High; rerun the affected tests and the E2E test.

## Decisions for the owner (defaults implemented, confirm later)

1. Retention default: 90 days after event creation (configurable per event).
2. Public deployment: HTTPS domain + reverse proxy are required for phone cameras; not done here.
3. Admin accounts: one shared admin password (hashed) vs. individual accounts.
4. Uploaded selfies vs camera-only: both kept (owner requirement); misuse mitigated by caps, rate limits and logs.
