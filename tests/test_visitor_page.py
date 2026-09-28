"""The visitor page always offers both ways to give a photo, and browsers get the current version."""
import re

from conftest import create_event


def test_visitor_page_offers_camera_and_upload(admin, client):
    _, token = create_event(admin)
    r = client.get(f"/e/{token}")
    html = r.text
    assert 'id="tab-camera"' in html and "Сделать фото" in html
    assert 'id="tab-upload"' in html and "Загрузить фото" in html
    for element in ("camera-panel", "camera-start", "camera-snap", "camera-retake", "selfie-input", "consent", "search-btn"):
        assert f'id="{element}"' in html, element


def test_pages_are_not_stored_and_assets_are_versioned(admin, client):
    """A browser must never show an outdated page or script after the site is updated."""
    _, token = create_event(admin)
    r = client.get(f"/e/{token}")
    assert r.headers["cache-control"] == "no-store"
    scripts = re.findall(r'src="(/static/event\.js\?v=[0-9a-f]{10})"', r.text)
    styles = re.findall(r'href="(/static/style\.css\?v=[0-9a-f]{10})"', r.text)
    assert scripts and styles
    assert client.get(scripts[0]).status_code == 200 and client.get(styles[0]).status_code == 200


def test_admin_shows_the_public_visitor_address_when_configured(admin, monkeypatch):
    """On the internet the admin works on the backend's own address, but guests use the public
    (Vercel) domain: the visitor link shown to the organiser must use the public domain."""
    from dataclasses import replace

    from app import main
    event_id, token = create_event(admin)
    assert f'value="http://testserver/e/{token}"' in admin.get(f"/admin/events/{event_id}").text
    monkeypatch.setattr(main, "settings", replace(main.settings, public_base_url="https://photos.example.org"))
    assert f'value="https://photos.example.org/e/{token}"' in admin.get(f"/admin/events/{event_id}").text


def test_site_is_called_tekhnokadr_and_event_names_are_unchanged(admin, client):
    """The SITE is "Технокадр" (header and tab title); events keep their own names."""
    _, token = create_event(admin, "Все фото")
    visitor = client.get(f"/e/{token}").text
    assert "<title>Все фото · Технокадр</title>" in visitor
    assert '<span class="brand wordmark">Технокадр</span>' in visitor
    assert '<h1 id="event-title" class="headline">Все фото</h1>' in visitor
    events_page = admin.get("/admin").text
    assert '<span class="brand wordmark">Технокадр</span>' in events_page and "<strong>Все фото</strong>" in events_page
    assert "Технокадр" in client.get("/e/not-a-real-token-at-all-000").text
    for page in (visitor, events_page):
        assert "Мои фото" not in page and "Мои<em>" not in page
