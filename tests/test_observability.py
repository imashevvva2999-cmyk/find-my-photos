"""Health checks, error pages and log redaction."""
import logging

from conftest import NASA, create_event, process_all, upload

from app import db, main, worker


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_dependencies_without_private_data(client):
    worker.beat("w1", processed=0)
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] == "ok" and body["migrations"] == "ok" and body["models"] == "ok"
    assert set(body["queue"]) == {"pending", "processing", "error"}
    assert body["worker"]["alive"] is True
    assert "token" not in r.text and "name" not in body.get("events", {})


def test_readyz_fails_when_database_is_down(client, monkeypatch):
    def down():
        raise db.DatabaseUnavailable("down")

    monkeypatch.setattr(main.db, "connect", down)
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["database"] == "unavailable"


def test_pages_show_a_friendly_error_when_the_database_is_down(client, monkeypatch):
    def down():
        raise db.DatabaseUnavailable("down")

    monkeypatch.setattr(main.db, "connect", down)
    r = client.get("/e/some-token-value-123456")
    assert r.status_code == 503 and "попробуйте ещё раз" in r.text.lower()
    r = client.post("/e/some-token-value-123456/search", content=b"x")
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


def test_access_log_redacts_private_links(admin, client, caplog):
    event_id, token = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    caplog.set_level(logging.INFO, logger="access")
    client.get(f"/e/{token}")
    client.get(f"/e/{token}/photo/SECRETKEY123.abc?download=1")
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "access")
    assert token not in text and "SECRETKEY123" not in text
    assert "/e/<redacted>" in text and "/photo/<redacted>" in text


def test_logs_never_contain_image_bytes_or_embeddings(admin, client, caplog):
    caplog.set_level(logging.DEBUG)
    event_id, token = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    client.post(f"/e/{token}/search", content=(NASA / "photo_30.jpg").read_bytes())
    # "httpx" is the test's own HTTP client (not part of the server), which logs full URLs.
    text = "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")
    assert token not in text
    assert "\\x" not in text and "JFIF" not in text and "embedding=" not in text
