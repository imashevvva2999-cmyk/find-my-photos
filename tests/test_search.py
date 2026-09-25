"""Visitor search and photo access: isolation between events, signed links, selfie handling."""
import os
import tempfile
import time

from PIL import Image
from conftest import NASA, SAMPLES, SEARCH, create_event, files_in_data, process_all, upload
from itsdangerous import URLSafeTimedSerializer

from app import db, main, matching, security
from app.config import settings

KOCH_SELFIE = (SEARCH / "search2_koch_2018_portrait.jpg").read_bytes()
KOCH_PHOTOS = ["photo_30.jpg", "photo_16.jpg", "photo_40.jpg"]   # all contain Christina Koch
OTHERS = ["photo_01.jpg", "photo_12.jpg"]                        # do not


def indexed_event(admin, names=KOCH_PHOTOS + OTHERS):
    event_id, token = create_event(admin)
    upload(admin, event_id, [NASA / n for n in names])
    process_all()
    return event_id, token


def search(client, token, data=KOCH_SELFIE):
    return client.post(f"/e/{token}/search", content=data, headers={"content-type": "image/jpeg"})


def names_for(results, event_id):
    signer = URLSafeTimedSerializer(settings.derived_key("result-links"), salt="result-link")
    ids = [signer.loads(m["view"].rsplit("/", 1)[1])[1] for m in results["matches"]]
    with db.connect() as conn:
        rows = conn.execute("SELECT id, original_name FROM photos WHERE id = ANY(%s)", (ids,)).fetchall()
    by_id = {r["id"]: r["original_name"] for r in rows}
    return [by_id[i] for i in ids]


def test_finds_the_person_including_group_photos(admin, client):
    event_id, token = indexed_event(admin)
    r = search(client, token)
    assert r.status_code == 200
    data = r.json()
    assert set(names_for(data, event_id)) == set(KOCH_PHOTOS)
    assert all(m["strength"] in ("higher", "lower") for m in data["matches"])
    assert set(data["timing"]) == {"receive_ms", "face_ms", "match_ms", "server_ms"}


def test_selfie_is_not_stored_anywhere(admin, client):
    event_id, token = indexed_event(admin)
    tmp_before = set(os.listdir(tempfile.gettempdir()))
    data_before = files_in_data()
    with db.connect() as conn:
        counts_before = conn.execute("SELECT (SELECT COUNT(*) FROM photos) p, (SELECT COUNT(*) FROM faces) f").fetchone()
    assert search(client, token).status_code == 200
    assert files_in_data() == data_before
    assert set(os.listdir(tempfile.gettempdir())) - tmp_before == set()
    with db.connect() as conn:
        assert conn.execute("SELECT (SELECT COUNT(*) FROM photos) p, (SELECT COUNT(*) FROM faces) f").fetchone() == counts_before
        log = conn.execute("SELECT * FROM search_log").fetchall()
    assert len(log) == 1 and set(log[0]) == {"id", "event_id", "at", "outcome", "results", "duration_ms"}


def test_clear_messages_for_unusable_selfies(admin, client):
    _, token = indexed_event(admin, ["photo_30.jpg"])
    cases = {
        "multiple_faces": (SAMPLES / "two_people.jpg").read_bytes(),
        "not_image": b"hello",
        "empty": b"",
    }
    blank = tempfile.SpooledTemporaryFile()
    Image.new("RGB", (800, 600), (120, 180, 230)).save(blank, "JPEG")
    blank.seek(0)
    cases["no_face"] = blank.read()
    for code, data in cases.items():
        r = search(client, token, data)
        assert r.status_code in (400, 422) and r.json()["error"] == code, (code, r.text)
    assert search(client, token, b"\0" * (settings.max_selfie_mb * 1024 * 1024 + 10)).status_code == 413


def test_results_are_capped(admin, client, monkeypatch):
    _, token = indexed_event(admin)
    monkeypatch.setattr(settings, "max_results", 1)
    assert len(search(client, token).json()["matches"]) == 1


def test_search_cache_follows_deletions(admin, client):
    event_id, token = indexed_event(admin)
    first = search(client, token).json()
    with db.connect() as conn:
        photo_id = conn.execute("SELECT id FROM photos WHERE original_name = 'photo_40.jpg'").fetchone()["id"]
    admin.post(f"/admin/api/photos/{photo_id}/delete")
    second = search(client, token).json()
    assert len(second["matches"]) == len(first["matches"]) - 1


def test_search_works_while_photos_are_still_being_indexed(admin, client):
    event_id, token = indexed_event(admin, ["photo_30.jpg"])
    upload(admin, event_id, [NASA / "photo_40.jpg"])        # not processed yet
    data = search(client, token).json()
    assert data["waiting_photos"] == 1 and len(data["matches"]) == 1


def test_busy_server_answers_503_instead_of_queueing_forever(admin, client, monkeypatch):
    _, token = indexed_event(admin, ["photo_30.jpg"])
    gate = main.search_gate
    held = [gate.try_enter() for _ in range(gate.capacity)]
    try:
        r = search(client, token)
        assert r.status_code == 503 and r.json()["error"] == "busy"
    finally:
        for ok in held:
            if ok:
                gate.leave()


def test_search_rate_limit_per_visitor(admin, client, monkeypatch):
    _, token = indexed_event(admin, ["photo_30.jpg"])
    monkeypatch.setattr(settings, "searches_per_10_min", 2)
    assert [search(client, token).status_code for _ in range(3)] == [200, 200, 429]


def test_closed_event_refuses_search_and_links(admin, client):
    event_id, token = indexed_event(admin, ["photo_30.jpg"])
    link = search(client, token).json()["matches"][0]["view"]
    admin.post(f"/admin/events/{event_id}/toggle")
    assert search(client, token).status_code == 403
    assert client.get(link).status_code == 404


def test_links_cannot_cross_events(admin, client):
    a_id, a_token = indexed_event(admin, ["photo_30.jpg"])
    b_id, b_token = indexed_event(admin, ["photo_40.jpg"])
    a_link = search(client, a_token).json()["matches"][0]["view"]
    key = a_link.rsplit("/", 1)[1]
    assert client.get(f"/e/{b_token}/photo/{key}").status_code == 404        # A's key under B's link
    signer = URLSafeTimedSerializer(settings.derived_key("result-links"), salt="result-link")
    with db.connect() as conn:
        b_photo = conn.execute("SELECT id FROM photos WHERE event_id = %s", (b_id,)).fetchone()["id"]
    forged = signer.dumps([a_id, b_photo])                                     # validly signed but wrong event
    assert client.get(f"/e/{a_token}/photo/{forged}").status_code == 404
    assert client.get(f"/e/{a_token}/photo/{key[:-2]}xx").status_code == 404   # tampered


def test_links_expire_and_die_with_a_new_event_link(admin, client, monkeypatch):
    event_id, token = indexed_event(admin, ["photo_30.jpg"])
    link = search(client, token).json()["matches"][0]["view"]
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + settings.result_link_minutes * 60 + 5)
    assert client.get(link).status_code == 404
    monkeypatch.setattr(time, "time", real_time)
    assert client.get(link).status_code == 200
    admin.post(f"/admin/events/{event_id}/new-link")
    assert client.get(link).status_code == 404


def test_download_is_a_metadata_free_copy_not_the_original(admin, client):
    event_id, token = indexed_event(admin, ["photo_30.jpg"])
    match = search(client, token).json()["matches"][0]
    r = client.get(match["download"])
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.content != (NASA / "photo_30.jpg").read_bytes()
    assert r.content[:2] == b"\xff\xd8" and b"Exif" not in r.content[:4096]


def test_missing_files_give_404_not_500(admin, client):
    event_id, token = indexed_event(admin, ["photo_30.jpg"])
    match = search(client, token).json()["matches"][0]
    for p in (security.settings.data_dir / "events" / str(event_id)).rglob("*.jpg"):
        p.unlink()
    assert client.get(match["thumb"]).status_code == 404
    assert client.get(match["download"]).status_code == 404


def test_visitors_cannot_use_admin_photo_urls(admin, client):
    event_id, _ = indexed_event(admin, ["photo_30.jpg"])
    assert client.get("/admin/photos/1/original", follow_redirects=False).status_code in (303, 401)
    assert admin.get("/admin/photos/1/unknown-kind").status_code == 404


def test_matching_cache_is_bounded():
    assert matching.CACHE_EVENTS >= 1
