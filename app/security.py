"""Same-origin checks and rate limiting. The site has no sign-in (a public one-time site).

- Unsafe /admin requests (POST) must come from this site's own pages (Origin/Referer
  check), so another website cannot make a visitor's browser delete or change events.
- Rate limits live in PostgreSQL so every web process shares them.
"""
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, Response

from . import db
from .config import settings

log = logging.getLogger("security")


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
    """Middleware: refuses cross-site POSTs to /admin/* before the request body is touched."""
    path = request.url.path
    if path.startswith("/admin"):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            log.warning("admin request refused: cross-site origin")
            return JSONResponse({"error": "forbidden", "message": "Запрос отклонён (с другого сайта)."}, status_code=403)
        # No sign-in: the organiser area is open to everyone (public site by the owner's choice).
    return await call_next(request)
