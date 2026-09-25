"""Admin authentication, CSRF origin checks and rate limiting.

- The admin password is stored as a scrypt hash (ADMIN_PASSWORD_HASH), never in plain text.
- A session is a signed cookie holding a "password version": changing the password logs
  out every existing session. Logging out clears the cookie.
- Admin requests are authenticated in middleware, BEFORE any request body is read, so an
  anonymous client cannot make the server receive a large upload.
- Unsafe admin requests (POST) must come from this site (Origin/Referer check).
- Rate limits live in PostgreSQL so every web process shares them.
"""
import hashlib
import logging
from datetime import datetime, timezone
import threading
import time
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import db
from .config import settings
from .passwords import password_version, verify_password

log = logging.getLogger("security")

SESSION_MAX_AGE = 12 * 3600
# The sign-in pages themselves (password and passkey) are the only admin paths open without a session.
PUBLIC_ADMIN_PATHS = {"/admin/login", "/admin/passkey/login/options", "/admin/passkey/login/verify"}


def check_password(password: str) -> bool:
    return verify_password(password, settings.admin_password_hash)


def current_version() -> str:
    return password_version(settings.admin_password_hash)


def start_session(session: dict) -> None:
    session.clear()
    session["admin"] = current_version()
    session["since"] = time.time()


_valid_after = {"value": 0.0, "checked": 0.0}
_valid_after_lock = threading.Lock()


def sessions_valid_after() -> float:
    """Sessions started before this moment are rejected (set by logout). Cached for 5 s."""
    with _valid_after_lock:
        if time.monotonic() - _valid_after["checked"] < 5:
            return _valid_after["value"]
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM admin_state WHERE key = 'sessions_valid_after'").fetchone()
    value = row["value"].timestamp() if row else 0.0
    with _valid_after_lock:
        _valid_after.update(value=value, checked=time.monotonic())
    return value


def revoke_all_sessions() -> None:
    """Log out every admin session on every device (a copied cookie stops working too)."""
    with db.connect() as conn:
        conn.execute("""INSERT INTO admin_state (key, value) VALUES ('sessions_valid_after', clock_timestamp())
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
    with _valid_after_lock:
        _valid_after["checked"] = 0.0


def is_admin(session: dict) -> bool:
    """Right password version, absolute lifetime of 12 h (not extended by activity),
    and started after the last logout."""
    since = session.get("since")
    if session.get("admin") != current_version() or not isinstance(since, (int, float)):
        return False
    if time.time() - since > SESSION_MAX_AGE:
        return False
    return since > sessions_valid_after()


def client_ip(request) -> str:
    """The client address as seen by uvicorn. Behind a reverse proxy, start uvicorn with
    --proxy-headers --forwarded-allow-ips=<proxy ip> so this is the real visitor address."""
    return request.client.host if request.client else "unknown"


def _hash_key(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:32]  # no raw IPs stored


class RateLimiter:
    """Fixed-window counters in PostgreSQL, shared by all processes."""

    @staticmethod
    def hit(key: str, limit: int, window_s: int, cost: int = 1) -> bool:
        """Count one event; return True if still within the limit."""
        now = datetime.now(timezone.utc).timestamp()
        window_start = datetime.fromtimestamp(now - now % window_s, timezone.utc)
        with db.connect() as conn:
            count = conn.execute("""
                INSERT INTO rate_limits (key, window_start, count) VALUES (%s, %s, %s)
                ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limits.count + EXCLUDED.count
                RETURNING count
            """, (key, window_start, cost)).fetchone()["count"]
        return count <= limit

    @staticmethod
    def peek(key: str, window_s: int) -> int:
        now = datetime.now(timezone.utc).timestamp()
        window_start = datetime.fromtimestamp(now - now % window_s, timezone.utc)
        with db.connect() as conn:
            row = conn.execute("SELECT count FROM rate_limits WHERE key = %s AND window_start = %s",
                               (key, window_start)).fetchone()
        return row["count"] if row else 0


    @staticmethod
    def undo(key: str, window_s: int) -> None:
        now = datetime.now(timezone.utc).timestamp()
        window_start = datetime.fromtimestamp(now - now % window_s, timezone.utc)
        with db.connect() as conn:
            conn.execute("UPDATE rate_limits SET count = GREATEST(count - 1, 0) WHERE key = %s AND window_start = %s",
                         (key, window_start))


LOGIN_WINDOW = 15 * 60
_password_checks = threading.BoundedSemaphore(4)  # each scrypt check uses 32 MB of memory


def attempt_login(ip: str, password: str) -> str:
    """Returns "ok", "wrong" or "blocked".

    The attempt is counted BEFORE the (slow) password check, so parallel guesses cannot all
    pass the limit; a successful login takes its count back. Too many failures from one
    address block that address for 15 minutes. A flood from many addresses only slows every
    login down (instead of locking the real admin out)."""
    per_ip_key = "login:" + _hash_key(ip)
    within_ip_limit = RateLimiter.hit(per_ip_key, settings.login_failures_per_15_min, LOGIN_WINDOW)
    if not within_ip_limit:
        log.warning("admin login blocked for 15 minutes after repeated failures")
        return "blocked"
    if not RateLimiter.hit("login:all", settings.login_failures_per_15_min * 10, LOGIN_WINDOW):
        time.sleep(2)
    with _password_checks:
        ok = check_password(password)
    if ok:
        RateLimiter.undo(per_ip_key, LOGIN_WINDOW)
        RateLimiter.undo("login:all", LOGIN_WINDOW)
        return "ok"
    log.warning("admin login failed")
    return "wrong"


def search_allowed(ip: str, event_id: int) -> bool:
    return RateLimiter.hit(f"search:{event_id}:" + _hash_key(ip), settings.searches_per_10_min, 600)


def _same_origin(request) -> bool:
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    origin = request.headers.get("origin")
    if origin:
        return origin == expected
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}" == expected
    return False  # browsers always send one of them on POST; tools must send Origin


async def admin_guard(request, call_next) -> Response:
    """Middleware: protects /admin/* before the request body is touched."""
    path = request.url.path
    if path.startswith("/admin"):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            log.warning("admin request refused: cross-site origin")
            return JSONResponse({"error": "forbidden", "message": "Запрос отклонён (с другого сайта)."}, status_code=403)
        if path not in PUBLIC_ADMIN_PATHS and not await run_in_threadpool(is_admin, dict(request.session)):
            if path.startswith("/admin/api/") or request.method != "GET":
                return JSONResponse({"error": "login_required", "message": "Пожалуйста, войдите снова."}, status_code=401)
            return RedirectResponse("/admin/login", status_code=303)
    return await call_next(request)
