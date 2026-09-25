"""Score face-search results against the answer key of the 40-photo NASA test collection.

The app must be running (./run.sh). Two ways to use it:

  1. Score searches that were done in the browser:
       .venv/bin/python tests/evaluate_40.py --event-id 2 --urls test_data/browser_results.json
     where browser_results.json is {"search1": [result photo URLs], "search2": [...]}

  2. Run everything through the app's web API (upload 40 photos, wait, search twice, score):
       .venv/bin/python tests/evaluate_40.py --api

It also lists the similarity score of the best face in every photo, which explains
why a photo was or wasn't returned (the app's cut-off is 0.363).
"""
import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import httpx
import psycopg
from itsdangerous import URLSafeTimedSerializer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import faces  # noqa: E402  (same face engine the website uses)

TEST_DATA = ROOT / "test_data"
BASE = "http://127.0.0.1:8000"
ENV = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().split() if "=" in line)
SEARCHES = {
    "search1": ("Search 1 - exact copy of photo_30.jpg", TEST_DATA / "search" / "search1_exact_copy.jpg"),
    "search2": ("Search 2 - different photo of Christina Koch (2018 portrait, not in the 40)",
                TEST_DATA / "search" / "search2_koch_2018_portrait.jpg"),
}


def load_answer_key() -> dict[str, dict]:
    with open(TEST_DATA / "manifest.csv") as f:
        return {Path(r["file"]).name: r for r in csv.DictReader(f) if r["file"].startswith("collection_40/")}


def urls_to_files(urls: list[str], event_id: int) -> list[str]:
    """Turn the app's signed result links back into collection file names."""
    signer = URLSafeTimedSerializer(ENV["SECRET_KEY"], salt="result-link")
    names = []
    with psycopg.connect(ENV["DATABASE_URL"]) as db:
        for url in urls:
            key = re.search(r"/photo/([^?]+)", url).group(1)
            linked_event, photo_id = signer.loads(key)
            assert linked_event == event_id, f"result belongs to event {linked_event}, not {event_id}"
            names.append(db.execute("SELECT original_name FROM photos WHERE id = %s", (photo_id,)).fetchone()[0])
    return names


def best_similarities(search_image: Path) -> dict[str, float | None]:
    """Best face similarity per collection photo (None = no face detected)."""
    query = max(faces.find_faces(faces.load_image(search_image.read_bytes())), key=lambda f: f.area).embedding
    result = {}
    for path in sorted((TEST_DATA / "collection_40").glob("photo_*.jpg")):
        found = faces.find_faces(faces.load_image(path.read_bytes()))
        result[path.name] = max((float(f.embedding @ query) for f in found), default=None)
    return result


def score(key: dict[str, dict], returned: list[str]) -> dict[str, list[str]]:
    truth = {name for name, row in key.items() if row["contains_target"] == "yes"}
    got = set(returned)
    return {"correct": sorted(truth & got), "missed": sorted(truth - got), "incorrect": sorted(got - truth)}


def describe(key: dict[str, dict], name: str) -> str:
    row = key[name]
    kind = "group" if row["group_photo"] == "yes" else "solo"
    return f"{name} ({kind}: {row['people_in_caption']})"


def report(label: str, results: dict[str, list[str]]) -> str:
    key = load_answer_key()
    face_counts = {}
    lines = [f"# Face search test - {label}", "",
             f"Collection: 40 photos in test_data/collection_40 "
             f"({sum(r['contains_target'] == 'yes' for r in key.values())} show Christina Koch).",
             "Answer key: test_data/manifest.csv (from NASA captions, checked by eye).", ""]
    for search_id, returned in results.items():
        title, image = SEARCHES[search_id]
        s = score(key, returned)
        sims = best_similarities(image)
        lines += [f"## {title}", f"Search image: {image.relative_to(ROOT)}", "",
                  "| Result | Count |", "|---|---|",
                  f"| Correct matches | {len(s['correct'])} |",
                  f"| Missed matches | {len(s['missed'])} |",
                  f"| Incorrect matches | {len(s['incorrect'])} |", ""]
        for kind in ("correct", "missed", "incorrect"):
            lines.append(f"**{kind.capitalize()}:** " + (", ".join(describe(key, n) for n in s[kind]) or "none"))
            lines.append("")
        lines += ["<details><summary>Best face similarity in each photo (cut-off 0.363)</summary>", "",
                  "| Photo | Koch? | Best similarity | Returned? | Note |", "|---|---|---|---|---|"]
        for name, sim in sorted(sims.items(), key=lambda kv: -(kv[1] if kv[1] is not None else -9)):
            lines.append(f"| {name} | {key[name]['contains_target']} | "
                         f"{'no face found' if sim is None else f'{sim:.3f}'} | "
                         f"{'yes' if name in returned else 'no'} | {key[name]['visual_check_note']} |")
        lines += ["", "</details>", ""]
        print(f"{title}: correct {len(s['correct'])}, missed {len(s['missed'])}, incorrect {len(s['incorrect'])}")
        for kind in ("missed", "incorrect"):
            for n in s[kind]:
                print(f"   {kind}: {describe(key, n)}  similarity={sims[n] if sims[n] is None else round(sims[n], 3)}")
    return "\n".join(lines)


def run_via_api() -> tuple[int, dict[str, list[str]]]:
    client = httpx.Client(base_url=BASE, timeout=120)
    assert client.post("/admin/login", data={"password": ENV["ADMIN_PASSWORD"]}).status_code in (200, 303)
    res = client.post("/admin/events", data={"name": "Test: 40 NASA photos (API run)"})
    event_id = int(res.headers["location"].rsplit("/", 1)[1])
    token = re.search(r'/e/([A-Za-z0-9_-]{20,})"', client.get(f"/admin/events/{event_id}").text).group(1)
    files = sorted((TEST_DATA / "collection_40").glob("photo_*.jpg"))
    for i in range(0, len(files), 5):
        batch = [("files", (p.name, p.read_bytes(), "image/jpeg")) for p in files[i:i + 5]]
        assert not client.post(f"/admin/api/events/{event_id}/photos", files=batch).json()["rejected"]
    while True:
        status = client.get(f"/admin/api/events/{event_id}/status").json()
        if status["pending"] + status["processing"] == 0:
            break
        time.sleep(1)
    print(f"Event {event_id}: {status['done']} photos processed, {status['faces']} faces found")
    results = {}
    for search_id, (_, image) in SEARCHES.items():
        data = client.post(f"/e/{token}/search", content=image.read_bytes()).json()
        results[search_id] = [m["view"] for m in data["matches"]]
    return event_id, results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", action="store_true")
    parser.add_argument("--event-id", type=int)
    parser.add_argument("--urls", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.api:
        event_id, urls = run_via_api()
        label, out = "run through the web API", args.out or TEST_DATA / "results_api.md"
    else:
        event_id, urls = args.event_id, json.loads(args.urls.read_text())
        label, out = "run in the browser", args.out or TEST_DATA / "results_browser.md"
    results = {sid: urls_to_files(u, event_id) for sid, u in urls.items()}
    out.write_text(report(label, results))
    print(f"Report saved to {out.resolve().relative_to(ROOT)}")


if __name__ == "__main__":
    main()
