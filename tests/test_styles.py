"""Styled versions (B&W Editorial, Editorial Film): background generation, resumability, viewer links,
downloads of the chosen version, the 3x watermark, and that originals / face data are never touched."""
import hashlib

import numpy as np
from PIL import Image
from conftest import NASA, create_event, files_in_data, process_all, upload
from test_search import KOCH_PHOTOS, indexed_event, search

from app import db, retouch, storage, styles, worker

# SHA-256 of the 256^3 Editorial Film table used for the approved comparison gallery
APPROVED_TABLE_SHA256 = "309cc6d63d3c159ad16ac7c9bd2d017a88a8f65ddf1218485054ae6c625b8fb9"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(event_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM photos WHERE event_id = %s ORDER BY id", (event_id,)).fetchall()


def face_data(event_id):
    with db.connect() as conn:
        return conn.execute("SELECT photo_id, x, y, w, h, embedding FROM faces WHERE event_id = %s ORDER BY id",
                            (event_id,)).fetchall()


def styled_files(event_id, photo_id):
    return [storage.styled_path(event_id, s, photo_id, k) for s in styles.STYLES for k in ("full", "preview")]


def ready_again():
    """Pretend the retry delay has passed."""
    with db.connect() as conn:
        conn.execute("UPDATE photos SET styles_next_at = now()")


def test_new_photo_gets_both_styles_in_the_background_without_touching_anything_else(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    (photo,) = rows(event_id)
    assert photo["styles_version"] == 0                       # the upload itself never waits for styles
    original = storage.original_path(event_id, photo["file_name"])
    kept = [original, storage.display_path(event_id, photo["id"]), storage.preview_path(event_id, photo["id"]),
            storage.thumb_path(event_id, photo["id"])]
    before = {p: sha(p) for p in kept}
    faces_before = face_data(event_id)

    assert worker.run_pending_styles() == 1
    (photo,) = rows(event_id)
    assert photo["styles_version"] == styles.STYLE_VERSION and photo["styles_error"] is None
    assert len(rows(event_id)) == 1                           # one photo stays one photo
    for path in styled_files(event_id, photo["id"]):
        with Image.open(path) as im:
            assert not im.getexif() and max(im.size) <= styles.FULL_SIZE
    assert {p: sha(p) for p in kept} == before                # original, display, preview, thumb unchanged
    assert face_data(event_id) == faces_before                # face search data unchanged
    with Image.open(storage.styled_path(event_id, "bw_editorial", photo["id"])) as im:
        a = np.asarray(im.convert("RGB")).astype(int)
        assert np.abs(a[..., 0] - a[..., 2]).max() <= 12      # black and white (JPEG noise only)
    assert not [f for f in files_in_data() if f.endswith(".tmp")]


def test_styling_is_idempotent_and_resumable(admin, monkeypatch):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg", NASA / "photo_16.jpg"])
    process_all()
    real = styles.render
    calls = []

    def crash_on_second(img, source, photo_id):
        calls.append(photo_id)
        if len(calls) == 2:
            raise MemoryError("simulated crash")
        return real(img, source, photo_id)

    monkeypatch.setattr(styles, "render", crash_on_second)
    assert worker.run_pending_styles() == 2
    first, second = rows(event_id)
    assert first["styles_version"] == styles.STYLE_VERSION
    assert second["styles_version"] == 0 and second["styles_error"]
    assert not any(p.exists() for p in styled_files(event_id, second["id"]))
    assert not [f for f in files_in_data() if f.endswith(".tmp")]

    mtimes = [p.stat().st_mtime_ns for p in styled_files(event_id, first["id"])]
    ready_again()
    assert worker.run_pending_styles() == 1                   # only the unfinished photo is done again
    assert [p.stat().st_mtime_ns for p in styled_files(event_id, first["id"])] == mtimes
    assert all(r["styles_version"] == styles.STYLE_VERSION for r in rows(event_id))
    assert worker.run_pending_styles() == 0


def test_a_crashed_styling_run_is_picked_up_again(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    assert worker.claim_styles() is not None                  # claimed, then the process "dies"
    assert worker.claim_styles() is None                      # locked while it could still be running
    with db.connect() as conn:
        conn.execute("UPDATE photos SET styles_locked_at = now() - interval '1 hour'")
    assert worker.run_pending_styles() == 1
    assert rows(event_id)[0]["styles_version"] == styles.STYLE_VERSION


def test_styles_wait_while_uploads_are_being_processed(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    upload(admin, event_id, [NASA / "photo_16.jpg"])          # a new upload is waiting
    assert worker.claim_styles() is None
    process_all()
    assert worker.claim_styles() is not None


def test_failed_styling_stops_after_max_attempts_and_the_organiser_can_retry(admin, client, monkeypatch):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()

    def boom(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(styles, "render", boom)
    for _ in range(worker.MAX_ATTEMPTS + 1):
        ready_again()
        worker.run_pending_styles()
    (photo,) = rows(event_id)
    assert photo["styles_attempts"] == worker.MAX_ATTEMPTS and photo["styles_version"] == 0
    assert client.get("/readyz").json()["styles"]["failed"] == 1
    monkeypatch.undo()
    assert admin.post(f"/admin/api/events/{event_id}/retry").status_code == 200
    assert worker.run_pending_styles() == 1
    assert rows(event_id)[0]["styles_version"] == styles.STYLE_VERSION
    ready = client.get("/readyz").json()
    assert ready["styles"] == {"version": styles.STYLE_VERSION, "done": 1, "waiting": 0, "failed": 0}
    assert ready["disk_free_mb"] > 0


def test_viewer_offers_three_versions_and_downloads_the_chosen_one(admin, client):
    event_id, token = indexed_event(admin, KOCH_PHOTOS[:1])
    match = search(client, token).json()["matches"][0]
    assert "styles" not in match                              # not styled yet: original only
    assert client.get(match["view"] + "?style=bw_editorial").status_code == 404   # never the original instead

    worker.run_pending_styles()
    match = search(client, token).json()["matches"][0]
    assert [s["key"] for s in match["styles"]] == ["original", "bw_editorial", "editorial_film"]
    assert [s["title"] for s in match["styles"]] == ["Оригинал", "B&W Editorial", "Editorial Film"]
    photo_id = rows(event_id)[0]["id"]

    names, bodies = {}, {}
    for s in match["styles"]:
        r = client.get(s["download"])
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        names[s["key"]] = r.headers["content-disposition"].split("filename=")[1].strip('"')
        bodies[s["key"]] = r.content
        view = client.get(s["view"])
        assert view.status_code == 200 and view.headers["content-type"] == "image/jpeg"
    assert names == {"original": f"photo-{photo_id}-original.jpg",
                     "bw_editorial": f"photo-{photo_id}-bw-editorial.jpg",
                     "editorial_film": f"photo-{photo_id}-editorial-film.jpg"}
    assert bodies["original"] == storage.display_path(event_id, photo_id).read_bytes()
    for s in styles.STYLES:
        assert bodies[s] == storage.styled_path(event_id, s, photo_id, "full").read_bytes()
    assert len(set(bodies.values())) == 3

    assert client.get(match["view"] + "?style=vintage").status_code == 404      # only the approved styles
    storage.styled_path(event_id, "editorial_film", photo_id, "full").unlink()
    assert client.get(match["styles"][2]["download"]).status_code == 404       # missing file: 404, not the original


def test_gallery_lists_styles_only_for_styled_photos(admin, client):
    event_id, token = indexed_event(admin, ["photo_30.jpg", "photo_16.jpg"])
    first = rows(event_id)[0]
    worker.process_styles(worker.claim_styles())
    photos = client.get(f"/e/{token}/gallery").json()["photos"]
    assert len(photos) == 2 and photos[0]["id"] == first["id"]
    assert [("styles" in p) for p in photos] == [True, False]


def test_deleting_a_photo_removes_its_styled_files(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    worker.run_pending_styles()
    photo_id = rows(event_id)[0]["id"]
    assert all(p.exists() for p in styled_files(event_id, photo_id))
    assert admin.post(f"/admin/api/photos/{photo_id}/delete").status_code == 200
    assert not any(p.exists() for p in styled_files(event_id, photo_id))


def _mark_box(img):
    """Bounding box of what the watermark changed on a flat mid-grey picture."""
    diff = np.abs(np.asarray(img).astype(int) - 128).max(axis=2) > 40
    ys, xs = np.nonzero(diff)
    return xs.min(), ys.min(), xs.max(), ys.max()


def test_watermark_is_three_times_the_approved_size_bottom_left(monkeypatch):
    for w, h in ((4096, 2731), (2731, 4096)):                  # landscape and portrait
        grey = Image.new("RGB", (w, h), (128, 128, 128))
        monkeypatch.setattr(retouch, "WATERMARK_SCALE", 1)      # the approved test size
        x0, y0, x1, y1 = _mark_box(retouch.add_watermark(grey))
        monkeypatch.setattr(retouch, "WATERMARK_SCALE", 3)
        X0, Y0, X1, Y1 = _mark_box(retouch.add_watermark(grey))
        assert 2.9 <= (X1 - X0) / (x1 - x0) <= 3.1 and 2.9 <= (Y1 - Y0) / (y1 - y0) <= 3.1
        short = min(w, h)
        assert X0 >= 0.035 * short and h - Y1 >= 0.03 * short  # clear of the edges, bottom-left
        assert X1 < w / 2 and Y0 > h / 2


def test_watermark_stays_readable_on_white_and_black():
    for colour in ((255, 255, 255), (0, 0, 0)):
        img = retouch.add_watermark(Image.new("RGB", (3000, 2000), colour))
        crop = np.asarray(img.crop((0, 1400, 1200, 2000)).convert("L")).astype(int)
        assert crop.max() - crop.min() > 100                   # white text with a dark shadow shows on both


def test_production_styles_match_the_approved_code():
    """The Editorial Film colour table is the one approved in the comparison gallery, and the
    pipeline is deterministic (same photo and id -> the same pixels)."""
    table = styles.build_table(styles.load_cube())
    assert hashlib.sha256(table.tobytes()).hexdigest() == APPROVED_TABLE_SHA256
    img = Image.open(NASA / "photo_30.jpg").convert("RGB")
    a, b = styles.render(img, None, 7), styles.render(img, None, 7)
    for s in styles.STYLES:
        assert np.array_equal(np.asarray(a[s]), np.asarray(b[s]))


def test_styling_runs_in_a_separate_process(admin):
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    photo = worker.claim_styles()
    assert worker.style_in_child(photo) is True
    row = rows(event_id)[0]
    assert row["styles_version"] == styles.STYLE_VERSION and row["styles_locked_at"] is None
    assert all(p.exists() for p in styled_files(event_id, row["id"]))


def test_a_killed_styling_process_only_fails_that_attempt(admin, monkeypatch):
    """E.g. the kernel's out-of-memory killer ends the child: the worker records a retryable failure."""
    event_id, _ = create_event(admin)
    upload(admin, event_id, [NASA / "photo_30.jpg"])
    process_all()
    photo = worker.claim_styles()
    real_run = worker.subprocess.run

    def killed(cmd, **kw):
        return real_run([worker.sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"], **kw)

    monkeypatch.setattr(worker.subprocess, "run", killed)
    assert worker.style_in_child(photo) is False
    row = rows(event_id)[0]
    assert row["styles_version"] == 0 and row["styles_locked_at"] is None and "повторена" in row["styles_error"]
    assert not any(p.exists() for p in styled_files(event_id, row["id"]))
