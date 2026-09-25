"""Admin authentication, brute-force limits, CSRF origin checks and body-before-auth."""
import asyncio

from conftest import ADMIN_PASSWORD, ORIGIN, create_event

from app import db, main, security
from app.passwords import hash_password


def test_admin_area_requires_login(client):
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/admin/login"
    assert client.get("/admin/api/events/1/status").status_code == 401
    assert client.post("/admin/events", data={"name": "x"}, follow_redirects=False).status_code in (303, 401)
    assert client.get("/admin/photos/1/original", follow_redirects=False).status_code in (303, 401)


def test_unauthenticated_upload_is_rejected_before_the_body_is_read():
    """Anyone could otherwise make the server spool unlimited data to disk."""
    chunks_read = 0

    async def receive():
        nonlocal chunks_read
        chunks_read += 1
        return {"type": "http.request", "body": b"\0" * (1 << 20), "more_body": chunks_read < 500}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "method": "POST", "path": "/admin/api/events/1/photos", "raw_path": b"/admin/api/events/1/photos",
        "query_string": b"", "root_path": "", "scheme": "http", "server": ("testserver", 80), "client": ("1.2.3.4", 5),
        "headers": [(b"host", b"testserver"), (b"content-type", b"multipart/form-data; boundary=B"),
                    (b"content-length", str(500 << 20).encode()), (b"origin", b"http://testserver")],
        "http_version": "1.1", "asgi": {"version": "3.0"}, "state": {},
    }
    asyncio.run(main.app(scope, receive, send))
    assert sent[0]["status"] in (401, 413)
    assert chunks_read <= 1, f"server read {chunks_read} MB before rejecting"


def test_wrong_passwords_lock_out_the_ip(client):
    for _ in range(5):
        assert client.post("/admin/login", data={"password": "wrong"}).status_code == 401
    r = client.post("/admin/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False)
    assert r.status_code == 429          # even the right password waits until the lockout ends
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rate_limits").fetchone()["n"] >= 1  # shared, not in memory


def test_session_cookie_flags(client):
    r = client.post("/admin/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_changing_the_password_logs_out_existing_sessions(admin, monkeypatch):
    assert admin.get("/admin", follow_redirects=False).status_code == 200
    monkeypatch.setattr(security.settings, "admin_password_hash", hash_password("a-new-password-5678"))
    assert admin.get("/admin", follow_redirects=False).status_code == 303


def test_logout_invalidates_session(admin):
    admin.post("/admin/logout")
    assert admin.get("/admin", follow_redirects=False).status_code == 303


def test_admin_posts_from_another_site_are_refused(admin):
    event_id, _ = create_event(admin)
    evil = {"Origin": "https://evil.example"}
    assert admin.post(f"/admin/events/{event_id}/delete", headers=evil, follow_redirects=False).status_code == 403
    assert admin.post("/admin/events", data={"name": "x"}, headers={"Origin": ""}, follow_redirects=False).status_code == 403
    assert admin.get(f"/admin/events/{event_id}").status_code == 200   # still exists


def test_security_headers(client):
    h = client.get("/").headers
    assert "base-uri 'none'" in h["content-security-policy"] and "form-action 'self'" in h["content-security-policy"]
    # same-origin: links are never sent to other sites, and browsers still send a real Origin
    # on same-site form posts (with no-referrer they send "Origin: null" and admin POSTs break).
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "same-origin"
    assert h["permissions-policy"].startswith("camera=(self)")
    assert "noindex" in h["x-robots-tag"]


def test_origin_header_default_is_accepted(admin):
    assert admin.post("/admin/events", data={"name": "ok"}, headers=ORIGIN, follow_redirects=False).status_code == 303
