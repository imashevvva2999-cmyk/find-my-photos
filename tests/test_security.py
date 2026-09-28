"""The site has no sign-in (a public one-time site): everyone can view and manage events.
What remains: cross-site POSTs are refused, size limits, security headers."""
import asyncio

from conftest import ORIGIN, create_event

from app import main


def test_everything_works_without_signing_in(client):
    """A first-time visitor (no cookies, no password) can manage events."""
    assert client.get("/admin", follow_redirects=False).status_code == 200
    assert client.get("/admin/login", follow_redirects=False).headers["location"] == "/admin"  # old address
    event_id, token = create_event(client, "Открытое мероприятие")
    assert client.get(f"/admin/events/{event_id}").status_code == 200
    assert client.get(f"/admin/api/events/{event_id}/status").status_code == 200
    assert client.get(f"/e/{token}").status_code == 200
    assert not client.cookies                               # no session cookie is ever set
    assert client.post(f"/admin/events/{event_id}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/admin/events/{event_id}").status_code == 404


def test_sign_in_routes_are_gone(client):
    assert client.post("/admin/login", data={"password": "x"}).status_code == 405
    assert client.post("/admin/logout").status_code == 404
    for path in ("/admin/passkey/login/options", "/admin/api/passkeys/options", "/internal/import-files"):
        assert client.post(path).status_code == 404, path


def test_oversized_upload_is_rejected_before_the_body_is_read():
    """Anyone can upload (no sign-in), so a declared oversized body must be refused unread."""
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
    assert sent[0]["status"] == 413
    assert chunks_read <= 1, f"server read {chunks_read} MB before rejecting"


def test_admin_posts_from_another_site_are_refused(client):
    event_id, _ = create_event(client)
    evil = {"Origin": "https://evil.example"}
    assert client.post(f"/admin/events/{event_id}/delete", headers=evil, follow_redirects=False).status_code == 403
    assert client.post("/admin/events", data={"name": "x"}, headers={"Origin": ""}, follow_redirects=False).status_code == 403
    assert client.get(f"/admin/events/{event_id}").status_code == 200   # still exists


def test_security_headers(client):
    h = client.get("/").headers
    assert "base-uri 'none'" in h["content-security-policy"] and "form-action 'self'" in h["content-security-policy"]
    # same-origin: links are never sent to other sites, and browsers still send a real Origin
    # on same-site form posts (with no-referrer they send "Origin: null" and admin POSTs break).
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "same-origin"
    assert h["permissions-policy"].startswith("camera=(self)")
    assert "noindex" in h["x-robots-tag"]


def test_origin_header_default_is_accepted(client):
    assert client.post("/admin/events", data={"name": "ok"}, headers=ORIGIN, follow_redirects=False).status_code == 303
