"""Visitor gallery: every finished photo of one event, and nothing from other events."""
from conftest import NASA, create_event, process_all, upload

from app import db, main

PHOTOS = ["photo_30.jpg", "photo_16.jpg", "photo_40.jpg", "photo_01.jpg", "photo_12.jpg"]


def event_with(admin, names):
    event_id, token = create_event(admin)
    upload(admin, event_id, [NASA / n for n in names])
    process_all()
    return event_id, token


def photo_ids(event_id):
    with db.connect() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM photos WHERE event_id = %s ORDER BY id", (event_id,))]


def test_gallery_lists_all_photos_of_this_event_only(admin, client):
    a_id, a_token = event_with(admin, PHOTOS)
    b_id, _ = event_with(admin, ["photo_40.jpg"])
    r = client.get(f"/e/{a_token}/gallery")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["total"] == 5 and data["next"] is None and data["waiting"] == 0
    assert [p["id"] for p in data["photos"]] == photo_ids(a_id)
    assert not set(photo_ids(b_id)) & {p["id"] for p in data["photos"]}
    for photo in data["photos"]:  # every link opens, as a thumbnail, a preview and a clean download
        assert client.get(photo["thumb"]).status_code == 200
        assert client.get(photo["view"]).status_code == 200
        assert client.get(photo["download"]).headers["content-type"] == "image/jpeg"


def test_gallery_is_paged(admin, client):
    event_id, token = event_with(admin, PHOTOS)
    first = client.get(f"/e/{token}/gallery?limit=2").json()
    assert len(first["photos"]) == 2 and first["total"] == 5 and first["next"] == first["photos"][-1]["id"]
    seen = [p["id"] for p in first["photos"]]
    after = first["next"]
    while after is not None:
        page = client.get(f"/e/{token}/gallery?limit=2&after={after}").json()
        seen += [p["id"] for p in page["photos"]]
        after = page["next"]
    assert seen == photo_ids(event_id)


def test_gallery_shows_only_finished_photos(admin, client):
    event_id, token = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])          # not processed yet
    assert client.get(f"/e/{token}/gallery").json() == {"photos": [], "next": None, "total": 0, "waiting": 1}


def test_gallery_respects_access_controls(admin, client):
    event_id, token = event_with(admin, ["photo_30.jpg"])
    assert client.get("/e/not-a-real-token-at-all-000/gallery").status_code == 404
    link = client.get(f"/e/{token}/gallery").json()["photos"][0]["view"]
    admin.post(f"/admin/events/{event_id}/toggle")             # organiser turns the event off
    assert client.get(f"/e/{token}/gallery").status_code == 403
    assert client.get(link).status_code == 404
    admin.post(f"/admin/events/{event_id}/toggle")
    admin.post(f"/admin/events/{event_id}/new-link")           # the old event link stops working
    assert client.get(f"/e/{token}/gallery").status_code == 404
    with db.connect() as conn:                                  # expired event
        conn.execute("UPDATE events SET expires_at = now() - interval '1 minute' WHERE id = %s", (event_id,))
        new_token = conn.execute("SELECT token FROM events WHERE id = %s", (event_id,)).fetchone()["token"]
    assert client.get(f"/e/{new_token}/gallery").status_code == 404


def test_gallery_limit_is_bounded(admin, client):
    _, token = event_with(admin, PHOTOS)
    data = client.get(f"/e/{token}/gallery?limit=100000").json()
    assert data["total"] == 5 and len(data["photos"]) == 5
    assert main.GALLERY_PAGE <= 500


def test_the_same_photo_cannot_be_added_to_an_event_twice(admin, client):
    event_id, token = create_event(admin)
    data = (NASA / "photo_30.jpg").read_bytes()
    r = upload(admin, event_id, [("IMG_1.JPG", data, "image/jpeg"),
                                 ("IMG_1 2.JPG", data, "image/jpeg"),                     # Finder copy, other name
                                 ("other.jpg", (NASA / "photo_16.jpg").read_bytes(), "image/jpeg")])
    body = r.json()
    assert [a["name"] for a in body["accepted"]] == ["IMG_1.JPG", "other.jpg"]
    assert body["rejected"][0]["name"] == "IMG_1 2.JPG" and "Уже есть в этом мероприятии" in body["rejected"][0]["reason"]
    assert upload(admin, event_id, [NASA / "photo_30.jpg"]).json()["rejected"]  # also in a later upload
    other_event, _ = create_event(admin, "Another event")
    assert upload(admin, other_event, [NASA / "photo_30.jpg"]).json()["accepted"]  # other events are separate
    process_all()
    assert client.get(f"/e/{token}/gallery").json()["total"] == 2


def test_photo_links_are_stable_for_a_while_so_thumbnails_are_cached(admin, client):
    _, token = event_with(admin, ["photo_30.jpg"])
    first = client.get(f"/e/{token}/gallery").json()["photos"][0]["thumb"]
    again = client.get(f"/e/{token}/gallery").json()["photos"][0]["thumb"]
    assert first == again or main.LINK_BUCKET_S == 0   # same 10-minute period (almost always)
    r = client.get(first)
    assert r.status_code == 200 and "max-age" in r.headers["cache-control"]
