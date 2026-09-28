"""Logging, request logs without secrets, security headers and request size limits.

Logs never contain: image bytes, face embeddings, passwords, event link tokens or signed
photo keys. Paths are redacted before they are logged: /e/<token>/... becomes
/e/<redacted>/... and /photo/<key> becomes /photo/<redacted>. Query strings are not logged.
"""
import logging
import re
import sys
import time
import uuid

from starlette.exceptions import HTTPException

from .config import settings

access_log = logging.getLogger("access")

_E_TOKEN = re.compile(r"^/e/[^/]+")
_PHOTO_KEY = re.compile(r"/photo/[^/?]+")
_UPLOAD_PATH = re.compile(r"^/admin/api/events/\d+/photos$")
_SEARCH_PATH = re.compile(r"^/e/[^/]+/search$")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("uvicorn.access").disabled = True  # replaced by the redacting log below


def redact(path: str) -> str:
    return _PHOTO_KEY.sub("/photo/<redacted>", _E_TOKEN.sub("/e/<redacted>", path))


def body_limit_for(path: str) -> int:
    if _UPLOAD_PATH.match(path):
        return settings.max_files_per_request * settings.max_upload_mb * 1024 * 1024 + (1 << 20)
    if _SEARCH_PATH.match(path):
        return settings.max_selfie_mb * 1024 * 1024 + 1024
    return 64 * 1024


def _headers(secure: bool) -> list[tuple[bytes, bytes]]:
    csp = ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; object-src 'none'; "
           "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    headers = {
        "content-security-policy": csp,
        "x-robots-tag": "noindex, nofollow",
        # same-origin: the private link is never sent to other sites. (With "no-referrer",
        # browsers send "Origin: null" on form posts, which breaks the same-origin check.)
        "referrer-policy": "same-origin",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "permissions-policy": "camera=(self), microphone=(), geolocation=()",
        "cross-origin-opener-policy": "same-origin",
    }
    if secure:
        headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
    return [(k.encode(), v.encode()) for k, v in headers.items()]


class EdgeMiddleware:
    """Outermost layer: request id, redacted access log, security headers, body size limit."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]
        path = scope.get("path", "")
        status = {"code": 500}
        extra_headers = _headers(settings.cookie_secure) + [(b"x-request-id", request_id.encode())]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                is_html = any(k.lower() == b"content-type" and v.startswith(b"text/html") for k, v in headers)
                if is_html and b"cache-control" not in names:
                    # Pages are never stored by the browser: always the current version (and
                    # private pages do not linger on shared computers).
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers + extra_headers
            await send(message)

        limit = body_limit_for(path)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(send_wrapper, 413, "Загружаемый файл слишком большой.")
            self._log(scope, path, 413, started, request_id)
            return
        received = {"bytes": 0}

        async def receive_wrapper():
            message = await receive()
            if message["type"] == "http.request":
                received["bytes"] += len(message.get("body", b""))
                if received["bytes"] > limit:  # body larger than declared or no Content-Length
                    raise HTTPException(413, "Загружаемый файл слишком большой.")  # becomes a normal 413 response
            return message

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        finally:
            self._log(scope, path, status["code"], started, request_id)

    @staticmethod
    async def _reject(send, code: int, message: str):
        body = ('{"error": "too_large", "message": "%s"}' % message).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _log(scope, path, code, started, request_id):
        access_log.info("%s %s %s %dms rid=%s", scope.get("method"), redact(path), code,
                        round((time.perf_counter() - started) * 1000), request_id)
