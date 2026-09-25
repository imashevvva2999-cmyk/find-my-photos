"""Web routes.

- /admin/...      organiser area (hashed password, signed session, same-origin POSTs)
- /e/<token>/...  one event's visitor page; <token> is a long random secret
- /healthz, /readyz  health checks (no private data)

Photo processing happens in a separate worker process (app/worker.py).
"""
import asyncio
import hashlib
import logging
import secrets
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import cv2
import psycopg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, TimestampSigner, URLSafeTimedSerializer
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import db, faces, images, importer, maintenance, matching, migrate, passkeys, security, storage, uploads, worker
from .config import ALLOWED_EXTENSIONS, settings
from .observability import EdgeMiddleware, setup_logging

log = logging.getLogger("app")
APP_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


def asset(name: str) -> str:
    """URL of a static file with a content version (?v=...), so browsers never keep using an
    outdated script or stylesheet after the site is updated."""
    path = APP_DIR / "static" / name
    version = hashlib.sha256(path.read_bytes()).hexdigest()[:10] if path.exists() else "0"
    return f"/static/{name}?v={version}"


templates.env.globals["asset"] = asset

# Result links: signed [event id, photo id] with a timestamp; valid RESULT_LINK_MINUTES.
link_signer = URLSafeTimedSerializer(settings.derived_key("result-links"), salt="result-link")
LINK_BUCKET_S = 600


class _BucketSigner(TimestampSigner):
    """Stamps links with the start of the current 10-minute period, so the same photo gets the
    same URL for 10 minutes and the browser can reuse thumbnails it already has. Links are still
    checked against the real clock (link_signer), so they never last longer than promised."""

    def get_timestamp(self) -> int:
        return int(time.time()) // LINK_BUCKET_S * LINK_BUCKET_S


link_maker = URLSafeTimedSerializer(settings.derived_key("result-links"), salt="result-link", signer=_BucketSigner)
search_pool = ThreadPoolExecutor(max_workers=settings.search_concurrency, thread_name_prefix="search")
STATUS_PAGE = 500


class SearchGate:
    """Admission control: at most `capacity` searches running or waiting; others get 503."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._inside = 0
        self._lock = threading.Lock()

    def try_enter(self) -> bool:
        with self._lock:
            if self._inside >= self.capacity:
                return False
            self._inside += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self._inside = max(0, self._inside - 1)


search_gate = SearchGate(settings.search_concurrency + settings.search_queue)
STARTUP: dict = {"migrations_ok": None}  # set once at startup; used by /readyz


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings.log_level)
    try:
        todo = await run_in_threadpool(migrate.pending, settings.database_url)
        STARTUP["migrations_ok"] = not todo
    except Exception as exc:  # database down at start: serve anyway, /readyz reports it
        log.error("could not check migrations: %s", type(exc).__name__)
        todo = []
    if todo:
        raise SystemExit(f"Database migrations pending: {', '.join(todo)}. Run: .venv/bin/python -m app.migrate")
    if not faces.models_available():
        log.error("face models missing in %s", faces.DETECTOR_PATH.parent)
    db.init()
    log.info("web app started")
    yield
    db.close()
    search_pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(security.admin_guard)  # innermost: runs after the session is loaded
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.derived_key("session"),
    session_cookie="admin_session",
    max_age=security.SESSION_MAX_AGE,
    same_site="strict",
    https_only=settings.cookie_secure,
)
app.add_middleware(EdgeMiddleware)  # outermost: headers, size limits, redacted access log
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def json_error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _wants_json(request: Request) -> bool:
    path = request.url.path
    return path.startswith("/admin/api/") or path.endswith(("/search", "/gallery")) or path == "/readyz"


@app.exception_handler(db.DatabaseUnavailable)
async def database_unavailable(request: Request, _exc: db.DatabaseUnavailable):
    message = "Сервис временно недоступен. Попробуйте ещё раз через минуту."
    if _wants_json(request):
        return json_error("unavailable", message, 503)
    return templates.TemplateResponse(request, "error.html", {"title": "Временно недоступно", "message": message},
                                      status_code=503)


# --------------------------------------------------------------------------- public

@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(request, "home.html")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


# --------------------------------------------------------------------------- photo-file import
# Off (404) unless IMPORT_TOKEN_SHA256 is set; see app/importer.py.

@app.post(importer.PATH)
async def import_files(request: Request):
    if not importer.authorised(request.headers.get("authorization", "")):
        raise HTTPException(404)
    archive = storage.new_incoming_path()
    try:
        with open(archive, "wb") as out:
            async for chunk in request.stream():
                out.write(chunk)
        result = await run_in_threadpool(importer.extract, archive)
    except tarfile.TarError:
        return JSONResponse({"error": "bad_archive"}, status_code=400)
    finally:
        archive.unlink(missing_ok=True)
    return result


@app.get(importer.PATH)
def import_inventory(request: Request, event: int):
    if not importer.authorised(request.headers.get("authorization", "")):
        raise HTTPException(404)
    return importer.inventory(event)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    body: dict = {"database": "ok", "migrations": "unknown", "models": "ok" if faces.models_available() else "missing"}
    try:
        with db.connect() as conn:
            q = conn.execute("""
                SELECT COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                       COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                       COUNT(*) FILTER (WHERE status = 'error') AS error FROM photos""").fetchone()
            beat = conn.execute("SELECT EXTRACT(EPOCH FROM now() - max(beat_at))::float AS age FROM worker_heartbeats").fetchone()
        body["queue"] = dict(q)
        age = beat["age"]
        body["worker"] = {"alive": age is not None and age < 60, "last_heartbeat_seconds": None if age is None else round(age)}
        if STARTUP["migrations_ok"] is None:  # the database was down at startup: check once now
            STARTUP["migrations_ok"] = not migrate.pending(settings.database_url)
        body["migrations"] = "ok" if STARTUP["migrations_ok"] else "pending"
    except (db.DatabaseUnavailable, psycopg.Error, OSError) as exc:
        body["database"] = "unavailable"
        log.warning("readiness check: database unavailable (%s)", type(exc).__name__)
    ready = body["database"] == "ok" and body["migrations"] == "ok" and body["models"] == "ok"
    return JSONResponse(body, status_code=200 if ready else 503)


# --------------------------------------------------------------------------- admin login

@app.get("/admin/login")
def login_page(request: Request):
    return RedirectResponse("/admin", status_code=303)  # no sign-in: the organiser area is open


@app.post("/admin/login")
def login(request: Request, password: str = Form("")):
    result = security.attempt_login(security.client_ip(request), password)
    if result == "blocked":
        return templates.TemplateResponse(request, "login.html", {
            "error": "Слишком много неудачных попыток. Подождите 15 минут и попробуйте снова."}, status_code=429)
    if result == "ok":
        security.start_session(request.session)
        log.info("admin logged in")
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "Неверный пароль."}, status_code=401)


def _passkey_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": "passkey", "message": message}, status_code=status)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "Неверный запрос.")
    return body


@app.post("/admin/passkey/login/options")
def passkey_login_options(request: Request):
    return PlainTextResponse(passkeys.authentication_options(request), media_type="application/json")


@app.post("/admin/passkey/login/verify")
async def passkey_login_verify(request: Request):
    """Sign in with a passkey. Failed attempts count towards the same limit as wrong passwords."""
    body = await _json_body(request)
    ip_key = "login:" + security._hash_key(security.client_ip(request))
    if not await run_in_threadpool(security.RateLimiter.hit, ip_key, settings.login_failures_per_15_min, security.LOGIN_WINDOW):
        return _passkey_error("Слишком много неудачных попыток. Подождите 15 минут и попробуйте снова.", 429)
    try:
        await run_in_threadpool(passkeys.authenticate, request, body)
    except passkeys.PasskeyError as exc:
        return _passkey_error(str(exc), 401)
    await run_in_threadpool(security.RateLimiter.undo, ip_key, security.LOGIN_WINDOW)
    security.start_session(request.session)
    log.info("admin logged in with a passkey")
    return {"ok": True, "next": "/admin"}


@app.post("/admin/logout")
def logout(request: Request):
    """Logs out every admin session (the one shared admin account), including copied cookies."""
    security.revoke_all_sessions()
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


# --------------------------------------------------------------------------- admin area
# Every /admin route below is protected by security.admin_guard (middleware).

def load_event(conn, event_id: int):
    event = conn.execute("SELECT *, expires_at < now() AS expired FROM events WHERE id = %s", (event_id,)).fetchone()
    if event is None:
        raise HTTPException(404, "Мероприятие не найдено")
    return event


@app.get("/admin")
def admin_home(request: Request):
    with db.connect() as conn:
        events = conn.execute("""
            SELECT e.*, e.expires_at < now() AS expired,
                   COUNT(p.id) AS photo_count, COALESCE(SUM(p.face_count), 0) AS face_count
            FROM events e LEFT JOIN photos p ON p.event_id = e.id
            GROUP BY e.id ORDER BY e.id DESC""").fetchall()
    return templates.TemplateResponse(request, "admin_home.html",
                                      {"events": events, "passkeys": passkeys.list_passkeys()})


@app.post("/admin/api/passkeys/options")
def passkey_register_options(request: Request):
    return PlainTextResponse(passkeys.registration_options(request), media_type="application/json")


@app.post("/admin/api/passkeys")
async def passkey_register(request: Request):
    body = await _json_body(request)
    try:
        await run_in_threadpool(passkeys.register, request, body.get("credential"), str(body.get("name", "")))
    except passkeys.PasskeyError as exc:
        return _passkey_error(str(exc))
    return {"ok": True}


@app.post("/admin/passkeys/{passkey_id}/delete")
def passkey_delete(passkey_id: int):
    passkeys.delete(passkey_id)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/events")
def create_event(name: str = Form("")):
    name = name.strip()[:120]
    if not name:
        return RedirectResponse("/admin", status_code=303)
    with db.connect() as conn:
        event_id = conn.execute("""
            INSERT INTO events (name, token, expires_at) VALUES (%s, %s, now() + make_interval(days => %s))
            RETURNING id""", (name, secrets.token_urlsafe(16), settings.event_retention_days)).fetchone()["id"]
    log.info("event %s created", event_id)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.get("/admin/events/{event_id}")
def admin_event(request: Request, event_id: int):
    with db.connect() as conn:
        event = load_event(conn, event_id)
    return templates.TemplateResponse(request, "admin_event.html", {
        "event": event,
        "visitor_link": f"{settings.public_base_url or str(request.base_url).rstrip('/')}/e/{event['token']}",
        "max_mb": settings.max_upload_mb,
        "max_files": settings.max_files_per_request,
        "extensions": ", ".join(sorted(e.lstrip(".").upper() for e in ALLOWED_EXTENSIONS)),
        "max_days": settings.max_retention_days,
    })


@app.post("/admin/events/{event_id}/toggle")
def toggle_event(event_id: int):
    with db.connect() as conn:
        load_event(conn, event_id)
        conn.execute("UPDATE events SET is_open = NOT is_open WHERE id = %s", (event_id,))
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.post("/admin/events/{event_id}/new-link")
def new_link(event_id: int):
    """Replace the private link; the old link and all result links stop working at once."""
    with db.connect() as conn:
        load_event(conn, event_id)
        conn.execute("UPDATE events SET token = %s WHERE id = %s", (secrets.token_urlsafe(16), event_id))
    log.info("event %s link rotated", event_id)
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.post("/admin/events/{event_id}/expiry")
def set_expiry(event_id: int, days: int = Form(...)):
    if not 1 <= days <= settings.max_retention_days:
        raise HTTPException(400, f"Укажите число дней от 1 до {settings.max_retention_days}.")
    with db.connect() as conn:
        load_event(conn, event_id)
        conn.execute("UPDATE events SET expires_at = now() + make_interval(days => %s) WHERE id = %s", (days, event_id))
    return RedirectResponse(f"/admin/events/{event_id}", status_code=303)


@app.post("/admin/events/{event_id}/delete")
def delete_event(event_id: int):
    with db.connect() as conn:
        load_event(conn, event_id)
    maintenance.delete_event(event_id)
    log.info("event %s deleted by admin", event_id)
    return RedirectResponse("/admin", status_code=303)


def _event_exists(event_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM events WHERE id = %s", (event_id,)).fetchone() is not None


@app.post("/admin/api/events/{event_id}/photos")
async def upload_photos(request: Request, event_id: int):
    if not await run_in_threadpool(_event_exists, event_id):
        return json_error("not_found", "Этого мероприятия больше нет.", 404)
    if not await run_in_threadpool(uploads.disk_has_room):
        return json_error("disk_full", "Диск сервера почти заполнен, поэтому загрузка приостановлена. "
                                       "Освободите место и попробуйте снова.", 507)
    try:
        form = await request.form(max_files=settings.max_files_per_request, max_fields=20)
    except (StarletteHTTPException, BaseExceptionGroup) as raised:
        exc = _http_error_in(raised)  # may arrive wrapped in an exception group by the middleware
        if exc is None:
            raise
        if exc.status_code == 413:  # body larger than allowed (raised while it was being read)
            return json_error("too_large", "Загружаемый файл слишком большой.", 413)
        if "too many files" in str(exc.detail).lower():
            return json_error("too_many_files", f"Отправляйте не больше {settings.max_files_per_request} файлов за раз.", 413)
        return json_error("bad_upload", "Не удалось прочитать загрузку. Попробуйте ещё раз.", 400)
    accepted, rejected = [], []
    try:
        for item in form.getlist("files"):
            if not hasattr(item, "file"):
                continue
            try:
                result = await run_in_threadpool(uploads.save_upload, event_id, item.filename, item.file)
            except uploads.EventMissing:
                return json_error("not_found", "Этого мероприятия больше нет.", 404)
            (accepted if "accepted" in result else rejected).append(next(iter(result.values())))
    finally:
        await form.close()
    log.info("upload to event %s: accepted=%s rejected=%s", event_id, len(accepted), len(rejected))
    return {"accepted": accepted, "rejected": rejected}


def _http_error_in(exc: BaseException) -> StarletteHTTPException | None:
    """Find an HTTP error that may be wrapped in (nested) exception groups."""
    if isinstance(exc, StarletteHTTPException):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            if (found := _http_error_in(inner)) is not None:
                return found
    return None


def _parse_cursor(since: str | None):
    if not since or "|" not in since:
        return None, 0
    stamp, _, photo_id = since.partition("|")
    try:
        return datetime.fromisoformat(stamp), int(photo_id)
    except ValueError:
        return None, 0


@app.get("/admin/api/events/{event_id}/status")
def event_status(event_id: int, since: str | None = None):
    """Counts for the whole event, plus the photos that changed since `since` (a cursor)."""
    stamp, last_id = _parse_cursor(since)
    with db.connect() as conn:
        load_event(conn, event_id)
        groups = conn.execute("""
            SELECT status, COUNT(*) AS n, COALESCE(SUM(face_count), 0) AS faces,
                   COALESCE(SUM(processing_ms), 0) AS ms, COUNT(processing_ms) AS timed
            FROM photos WHERE event_id = %s GROUP BY status""", (event_id,)).fetchall()
        rows = conn.execute("""
            SELECT id, original_name, status, error, face_count, processing_ms, updated_at FROM photos
            WHERE event_id = %s AND (%s::timestamptz IS NULL OR (updated_at, id) > (%s::timestamptz, %s))
            ORDER BY updated_at, id LIMIT %s""", (event_id, stamp, stamp, last_id, STATUS_PAGE)).fetchall()
    counts = {s: 0 for s in ("pending", "processing", "done", "error")}
    for g in groups:
        counts[g["status"]] = g["n"]
    cursor = f"{rows[-1]['updated_at'].isoformat()}|{rows[-1]['id']}" if rows else since
    return {
        "total": sum(counts.values()),
        **counts,
        "faces": sum(g["faces"] for g in groups),
        "processing_seconds": round(sum(g["ms"] for g in groups) / 1000, 2),
        "timed_photos": sum(g["timed"] for g in groups),
        "photos": [{k: v for k, v in r.items() if k != "updated_at"} for r in rows],
        "cursor": cursor,
        "has_more": len(rows) == STATUS_PAGE,
    }


@app.post("/admin/api/events/{event_id}/retry")
def retry_failed(event_id: int):
    with db.connect() as conn:
        load_event(conn, event_id)
    return {"requeued": worker.retry_failed(event_id)}


@app.post("/admin/api/photos/{photo_id}/delete")
def delete_photo(photo_id: int):
    with db.connect() as conn:
        photo = conn.execute("DELETE FROM photos WHERE id = %s RETURNING event_id, file_name", (photo_id,)).fetchone()
        if photo is None:
            raise HTTPException(404, "Фото не найдено")
        conn.execute("UPDATE events SET index_version = index_version + 1 WHERE id = %s", (photo["event_id"],))
    storage.delete_photo_files(photo["event_id"], photo_id, photo["file_name"])
    return {"ok": True}


@app.get("/admin/photos/{photo_id}/{kind}")
def admin_photo(photo_id: int, kind: str):
    if kind not in ("thumb", "preview", "display", "original"):
        raise HTTPException(404)
    with db.connect() as conn:
        photo = conn.execute("SELECT * FROM photos WHERE id = %s", (photo_id,)).fetchone()
    if photo is None:
        raise HTTPException(404)
    event_id = photo["event_id"]
    if kind == "original":
        path = storage.original_path(event_id, photo["file_name"])
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, filename=photo["original_name"])
    path = {"thumb": storage.thumb_path, "preview": storage.preview_path, "display": storage.display_path}[kind](event_id, photo_id)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


# --------------------------------------------------------------------------- visitor area

def event_by_token(token: str):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM events WHERE token = %s AND expires_at > now()", (token,)).fetchone()


@app.get("/e/{token}")
def visitor_page(request: Request, token: str):
    event = event_by_token(token)
    if event is None:
        return templates.TemplateResponse(request, "not_found.html", status_code=404)
    return templates.TemplateResponse(request, "event.html", {
        "event": event, "max_mb": settings.max_selfie_mb, "link_minutes": settings.result_link_minutes,
    })


def _log_search(event_id: int, outcome: str, results: int, duration_ms: int) -> None:
    try:
        with db.connect() as conn:
            conn.execute("INSERT INTO search_log (event_id, outcome, results, duration_ms) VALUES (%s, %s, %s, %s)",
                         (event_id, outcome, results, duration_ms))
    except db.DatabaseUnavailable:
        pass  # statistics are best effort


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _run_search(event: dict, data: bytes) -> tuple[str, dict, int]:
    """Runs in the search thread pool. `data` (the selfie) exists only in memory here."""
    step = time.perf_counter()
    try:
        found = faces.find_faces(faces.load_image(data))
    except (images.ImageRejected, cv2.error) as exc:
        if isinstance(exc, cv2.error):
            log.warning("face detection failed on a selfie: %s", type(exc).__name__)
        return "not_image", {"error": "not_image", "message": "Не удалось прочитать это фото. "
                             "Используйте фотографию в формате JPG, PNG, WEBP или HEIC."}, 422
    face_ms = _ms(step)
    if not found:
        return "no_face", {"error": "no_face", "message": "На вашем фото не найдено лицо. Используйте чёткое фото при хорошем "
                           "освещении, где лицо видно полностью и смотрит в камеру."}, 422
    found.sort(key=lambda f: f.area, reverse=True)
    you = found[0]
    others = [f for f in found[1:] if f.area >= you.area * 0.25]  # tiny background faces are ignored
    if others:
        return "multiple_faces", {"error": "multiple_faces", "message": f"На вашем фото найдено несколько лиц ({len(others) + 1}). "
                                  "Используйте фото, на котором только вы."}, 422
    if min(you.box[2], you.box[3]) < 60:
        return "face_too_small", {"error": "face_too_small", "message": "Лицо на этом фото слишком маленькое. "
                                  "Используйте снимок крупнее."}, 422

    step = time.perf_counter()
    with db.connect() as conn:
        state = conn.execute("""
            SELECT e.index_version,
                   COUNT(p.id) FILTER (WHERE p.status = 'done') AS done,
                   COUNT(p.id) FILTER (WHERE p.status IN ('pending', 'processing')) AS waiting
            FROM events e LEFT JOIN photos p ON p.event_id = e.id WHERE e.id = %s GROUP BY e.id""",
                             (event["id"],)).fetchone()
    if state is None:  # the event was deleted a moment ago
        return "not_found", {"error": "not_found", "message": "Ссылка на мероприятие недействительна."}, 404
    ranked, compared = matching.search(event["id"], state["index_version"], you.embedding,
                                       settings.match_threshold, settings.max_results)
    matches = []
    for photo_id, score in ranked:
        matches.append({**_photo_links(event, photo_id),
                        "strength": "higher" if score >= settings.strong_match else "lower"})
    return "ok", {"matches": matches, "searched_photos": state["done"], "waiting_photos": state["waiting"],
                  "compared_faces": compared, "timing": {"face_ms": face_ms, "match_ms": _ms(step)}}, 200


SELFIE_UPLOAD_TIMEOUT_S = 30


@app.post("/e/{token}/search")
async def search(request: Request, token: str):
    started = time.perf_counter()
    event = await run_in_threadpool(event_by_token, token)
    if event is None:
        return json_error("not_found", "Ссылка на мероприятие недействительна.", 404)
    if not event["is_open"]:
        return json_error("closed", "Поиск фотографий для этого мероприятия сейчас выключен.", 403)
    if not await run_in_threadpool(security.search_allowed, security.client_ip(request), event["id"]):
        await run_in_threadpool(_log_search, event["id"], "rate_limited", 0, _ms(started))
        return json_error("too_many", "Слишком много поисков. Подождите несколько минут и попробуйте снова.", 429)

    # PRIVACY: the selfie is held only in memory for this request. It is never written to
    # disk, the database or the logs, and the buffer is cleared as soon as it is used.
    # The photo is received BEFORE a search slot is taken, with a time limit, so slow
    # uploads cannot occupy the slots other visitors need.
    max_bytes = settings.max_selfie_mb * 1024 * 1024
    selfie = bytearray()
    try:
        async with asyncio.timeout(SELFIE_UPLOAD_TIMEOUT_S):
            async for chunk in request.stream():
                selfie.extend(chunk)
                if len(selfie) > max_bytes:
                    selfie.clear()
                    return json_error("too_large", f"Ваше фото больше {settings.max_selfie_mb} МБ.", 413)
    except TimeoutError:
        selfie.clear()
        return json_error("upload_timeout", "Фото отправлялось слишком долго. Проверьте подключение к интернету "
                                            "и попробуйте снова.", 408)
    except (StarletteHTTPException, BaseExceptionGroup) as raised:  # size limit hit while receiving
        selfie.clear()
        exc = _http_error_in(raised)
        if exc is None or exc.status_code != 413:
            raise
        return json_error("too_large", f"Ваше фото больше {settings.max_selfie_mb} МБ.", 413)
    if not selfie:
        return json_error("empty", "Сначала выберите или сделайте фото своего лица.", 422)
    receive_ms = _ms(started)

    if not search_gate.try_enter():
        selfie.clear()
        return json_error("busy", "Сейчас ищут многие. Попробуйте ещё раз через несколько секунд.", 503)
    try:
        outcome, payload, status = await asyncio.get_running_loop().run_in_executor(
            search_pool, _run_search, dict(event), bytes(selfie))
    finally:
        search_gate.leave()
        selfie.clear()
    server_ms = _ms(started)
    if status == 200:
        payload["timing"] = {"receive_ms": receive_ms, **payload["timing"], "server_ms": server_ms}
    await run_in_threadpool(_log_search, event["id"], outcome, len(payload.get("matches", [])), server_ms)
    return JSONResponse(payload, status_code=status)


GALLERY_PAGE = 500  # the page asks for all photos at once, in pages of this size


def _photo_links(event: dict, photo_id: int) -> dict:
    base = f"/e/{event['token']}/photo/{link_maker.dumps([event['id'], photo_id])}"
    return {"thumb": f"{base}?size=thumb", "view": base, "download": f"{base}?download=1"}


@app.get("/e/{token}/gallery")
def gallery(token: str, after: int = 0, limit: int = GALLERY_PAGE):
    """All finished photos of this event, a page at a time (in upload order).
    Anyone with the event link may browse them; the links are signed and expire like search results.
    The first page also says how many photos are still being processed (not shown yet)."""
    event = event_by_token(token)
    if event is None:
        return json_error("not_found", "Ссылка на мероприятие недействительна.", 404)
    if not event["is_open"]:
        return json_error("closed", "Это мероприятие сейчас выключено.", 403)
    limit = max(1, min(limit, GALLERY_PAGE))
    with db.connect() as conn:
        rows = conn.execute("""SELECT id FROM photos WHERE event_id = %s AND status = 'done' AND id > %s
                               ORDER BY id LIMIT %s""", (event["id"], after, limit + 1)).fetchall()
        counts = conn.execute("""
            SELECT COUNT(*) FILTER (WHERE status = 'done') AS done,
                   COUNT(*) FILTER (WHERE status IN ('pending', 'processing')) AS waiting
            FROM photos WHERE event_id = %s""", (event["id"],)).fetchone() if after == 0 else None
    page = rows[:limit]
    return JSONResponse({"photos": [{"id": r["id"], **_photo_links(event, r["id"])} for r in page],
                         "next": page[-1]["id"] if len(rows) > limit else None,
                         "total": counts["done"] if counts else None,
                         "waiting": counts["waiting"] if counts else None},
                        headers={"Cache-Control": "no-store"})


@app.get("/e/{token}/photo/{key}")
def visitor_photo(token: str, key: str, size: str = "preview", download: bool = False):
    """One matched photo. The signed key proves it came from this visitor's search."""
    try:
        event_id, photo_id = link_signer.loads(key, max_age=settings.result_link_minutes * 60)
    except (BadSignature, ValueError, TypeError):
        raise HTTPException(404, "Срок действия ссылки истёк. Выполните поиск снова.")
    event = event_by_token(token)
    if event is None or event["id"] != event_id or not event["is_open"]:
        raise HTTPException(404)
    with db.connect() as conn:
        photo = conn.execute("SELECT id FROM photos WHERE id = %s AND event_id = %s AND status = 'done'",
                             (photo_id, event_id)).fetchone()
    if photo is None:
        raise HTTPException(404)
    if download:
        path = storage.display_path(event_id, photo_id)  # metadata-free copy, never the original
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg", filename=f"photo-{photo_id}.jpg")
    path = storage.thumb_path(event_id, photo_id) if size == "thumb" else storage.preview_path(event_id, photo_id)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
