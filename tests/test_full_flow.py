"""End-to-end test of the whole flow against the running app.

Start the app first (./run.sh), then in another terminal:
    .venv/bin/python tests/test_full_flow.py          # test, then delete the test event
    .venv/bin/python tests/test_full_flow.py --keep   # keep the event so you can look at it
"""
import io
import re
import sys
import time
from pathlib import Path

import httpx
from itsdangerous import URLSafeTimedSerializer
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "sample_photos"
BASE = "http://127.0.0.1:8000"
ENV = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().split() if "=" in line)

EVENT_PHOTOS = ["obama.jpg", "obama2.jpg", "obama3.jpg", "biden.jpg", "two_people.jpg", "alex-lacamoire.png"]
EXPECTED_MATCHES = {"obama.jpg", "obama2.jpg", "obama3.jpg", "two_people.jpg"}  # every photo showing the selfie person

passed = 0


def check(condition: bool, label: str) -> None:
    global passed
    if not condition:
        print(f"  FAIL  {label}")
        sys.exit(1)
    passed += 1
    print(f"  ok    {label}")


def landscape_jpeg() -> bytes:
    """A picture with no face in it (sky, sun and hills)."""
    img = Image.new("RGB", (1200, 800), (135, 190, 235))
    draw = ImageDraw.Draw(img)
    draw.ellipse((900, 80, 1040, 220), fill=(255, 220, 90))
    draw.polygon([(0, 800), (350, 420), (700, 800)], fill=(70, 130, 80))
    draw.polygon([(450, 800), (850, 380), (1200, 800)], fill=(50, 110, 70))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def files_on_disk() -> set[Path]:
    """Every stored photo file (the database server's own internal files are not photos)."""
    return {p for p in (ROOT / "data" / "events").rglob("*") if p.is_file()}


def main() -> None:
    keep = "--keep" in sys.argv
    admin = httpx.Client(base_url=BASE, follow_redirects=False, timeout=60)
    visitor = httpx.Client(base_url=BASE, follow_redirects=False, timeout=60)

    print("\n1. Admin area is protected")
    check(admin.get("/admin").headers.get("location") == "/admin/login", "admin page redirects to login")
    check(admin.get("/admin/api/events/1/status").status_code == 401, "admin API refuses anonymous requests")
    check(admin.post("/admin/login", data={"password": "wrong"}).status_code == 401, "wrong password is rejected")
    check(admin.post("/admin/login", data={"password": ENV["ADMIN_PASSWORD"]}).status_code == 303, "correct password logs in")

    print("\n2. Create event")
    res = admin.post("/admin/events", data={"name": "Demo: sample photos"})
    event_id = int(res.headers["location"].rsplit("/", 1)[1])
    page = admin.get(f"/admin/events/{event_id}").text
    token = re.search(r'/e/([A-Za-z0-9_-]{20,})"', page).group(1)
    check(len(token) >= 20, f"event {event_id} created with a private link token")

    print("\n3. Upload photos (with some bad files)")
    upload = [("files", (n, (SAMPLES / n).read_bytes(), "image/jpeg")) for n in EVENT_PHOTOS]
    upload += [
        ("files", ("landscape.jpg", landscape_jpeg(), "image/jpeg")),
        ("files", ("broken.jpg", b"this is not really a photo", "image/jpeg")),
        ("files", ("notes.txt", b"hello", "text/plain")),
    ]
    result = admin.post(f"/admin/api/events/{event_id}/photos", files=upload).json()
    rejected = {r["name"] for r in result["rejected"]}
    check(len(result["accepted"]) == 7, "7 real photos accepted")
    check(rejected == {"broken.jpg", "notes.txt"}, "broken image and text file rejected")
    names = {p["id"]: p["name"] for p in result["accepted"]}

    print("\n4. Face processing")
    for _ in range(120):
        status = admin.get(f"/admin/api/events/{event_id}/status").json()
        if status["pending"] + status["processing"] == 0:
            break
        time.sleep(0.5)
    faces = {p["original_name"]: p["face_count"] for p in status["photos"]}
    check(status["done"] == 7, "all 7 photos processed")
    check(faces["two_people.jpg"] == 2, "group photo: 2 faces found")
    check(faces["landscape.jpg"] == 0, "landscape: 0 faces found")
    check(all(faces[n] == 1 for n in ["obama.jpg", "obama2.jpg", "obama3.jpg", "biden.jpg", "alex-lacamoire.png"]),
          "portraits: 1 face each")

    print("\n5. Visitor search")
    check(visitor.get(f"/e/{token}").status_code == 200, "visitor page opens with the private link")
    check(visitor.get("/e/not-a-real-token").status_code == 404, "a made-up link does not work")
    check(visitor.get(f"/admin/api/events/{event_id}/status").status_code == 401, "visitor cannot use the admin API")

    before = files_on_disk()
    res = visitor.post(f"/e/{token}/search", content=(SAMPLES / "obama_small.jpg").read_bytes())
    data = res.json()
    signer = URLSafeTimedSerializer(ENV["SECRET_KEY"], salt="result-link")
    found = {names[signer.loads(m["view"].rsplit("/", 1)[1])[1]] for m in data["matches"]}
    check(res.status_code == 200, "search succeeds")
    check(found == EXPECTED_MATCHES, f"found exactly the right photos, incl. the group photo: {sorted(found)}")
    check(files_on_disk() == before, "visitor photo was NOT saved anywhere on disk")

    match = data["matches"][0]
    check(visitor.get(match["thumb"]).headers["content-type"] == "image/jpeg", "result thumbnail opens")
    check(visitor.get(match["view"]).status_code == 200, "result photo opens")
    download = visitor.get(match["download"])
    check("attachment" in download.headers.get("content-disposition", ""), "download is offered as a file")
    original_name = names[signer.loads(match["view"].rsplit("/", 1)[1])[1]]
    check(download.content == (SAMPLES / original_name).read_bytes(), "downloaded file is the full original photo")

    other_id = next(i for i, n in names.items() if n == "biden.jpg")
    forged = URLSafeTimedSerializer("guess", salt="result-link").dumps([event_id, other_id])
    check(visitor.get(f"/e/{token}/photo/{forged}").status_code == 404, "cannot open non-matching photos with a forged link")

    print("\n6. Problem photos give clear messages")
    for file_name, content, code in [
        ("two_people.jpg", (SAMPLES / "two_people.jpg").read_bytes(), "multiple_faces"),
        ("landscape", landscape_jpeg(), "no_face"),
        ("text", b"not an image", "not_image"),
        ("empty", b"", "empty"),
    ]:
        res = visitor.post(f"/e/{token}/search", content=content)
        check(res.status_code >= 400 and res.json()["error"] == code, f"{file_name} -> {code}: \"{res.json()['message'][:60]}…\"")

    res = visitor.post(f"/e/{token}/search", content=(SAMPLES / "lin-manuel-miranda.png").read_bytes())
    check(res.status_code == 200 and res.json()["matches"] == [], "person not at the event -> no matches")

    print("\n7. Admin controls")
    admin.post(f"/admin/events/{event_id}/toggle")
    check(visitor.post(f"/e/{token}/search", content=(SAMPLES / "obama_small.jpg").read_bytes()).status_code == 403,
          "search turned off -> visitors are refused")
    check(visitor.get(match["view"]).status_code == 404, "search turned off -> result links stop working")
    admin.post(f"/admin/events/{event_id}/toggle")

    headers = visitor.get(f"/e/{token}").headers
    check(headers.get("x-robots-tag") == "noindex, nofollow" and headers.get("referrer-policy") == "no-referrer",
          "pages tell search engines to stay away and never leak the link")

    if not keep:
        admin.post(f"/admin/events/{event_id}/delete")
        check(not (ROOT / "data" / "events" / str(event_id)).exists(), "deleting the event removes its photos")
        check(visitor.get(f"/e/{token}").status_code == 404, "deleted event link no longer works")

    print(f"\nAll {passed} checks passed.")
    if keep:
        print(f"Event kept. Visitor link: {BASE}/e/{token}")


if __name__ == "__main__":
    main()
