"""Build the 40-photo test collection from the NASA Image and Video Library API.

Why NASA: images-api.nasa.gov is a public API made for programs to use (no key, no
robots restrictions), and NASA's Media Usage Guidelines say NASA images "generally are
not subject to copyright in the United States" and may be used for educational or
informational purposes, with NASA credited as the source. Only NASA-credited photos are
used here (SpaceX / Gagarin Center photos are left out because they may be third-party).

Target person: NASA astronaut Christina Koch. The "people" column is taken from NASA's
own caption for each photo and was then checked by looking at every image.

Usage: .venv/bin/python tests/fetch_nasa_test_photos.py
Creates:
  test_data/collection_40/photo_01.jpg ... photo_40.jpg   (shuffled, neutral names)
  test_data/search/search1_exact_copy.jpg                  (byte-for-byte copy of one collection photo)
  test_data/search/search2_koch_2018_portrait.jpg          (a different Koch photo, NOT in the collection)
  test_data/manifest.csv                                   (answer key + source + credit for every file)
"""
import csv
import random
import shutil
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "test_data"
API = "https://images-api.nasa.gov"
LICENSE = "NASA media - generally not copyrighted in the US (NASA Media Usage Guidelines)"

# nasa_id, contains Koch?, group photo?, people named in NASA's caption
PHOTOS = [
    # --- Christina Koch alone (6)
    ("NHQ202009160004", True, False, "Christina Koch"),
    ("KSC-20210714-PH-GEB01_0138", True, False, "Christina Koch"),
    ("iss059e035465", True, False, "Christina Koch"),
    ("art002e027808", True, False, "Christina Koch"),
    ("jsc2026e022273", True, False, "Christina Koch"),
    ("NHQ202305180019", True, False, "Christina Koch"),
    # --- Christina Koch in group photos (6)
    ("KSC-20241119-PH-JBS01_0120", True, True, "Victor Glover, Reid Wiseman, Christina Koch, Jeremy Hansen"),
    ("KSC-20241119-PH-JBS02_0162", True, True, "Reid Wiseman, Victor Glover, Christina Koch, Jeremy Hansen"),
    ("130A4062-1", True, True, "Reid Wiseman, Victor Glover, Christina Koch, Jeremy Hansen"),
    ("NHQ202305180024", True, True, "Jeremy Hansen, Christina Koch"),
    ("art002e016247", True, True, "Victor Glover, Christina Koch"),
    ("iss059-s-002", True, True, "Expedition 59 crew incl. Anne McClain, Nick Hague, Christina Koch"),
    # --- other women, alone (12) - deliberately similar-looking people
    ("NHQ202009150005", False, False, "Jessica Meir"),
    ("iss074e0365172", False, False, "Jessica Meir"),
    ("NHQ202009160006", False, False, "Anne McClain"),
    ("iss073e0420808", False, False, "Anne McClain"),
    ("jsc2010e008396", False, False, "Kate Rubins"),
    ("iss064e013899", False, False, "Kate Rubins"),
    ("NHQ202009080004", False, False, "Jasmin Moghbeli"),
    ("NHQ202009150004", False, False, "Nicole Mann"),
    ("NHQ202009150003", False, False, "Jessica Watkins"),
    ("NHQ202009160003", False, False, "Kayla Barron"),
    ("jsc2024e066721", False, False, "Nichole Ayers"),
    ("jsc2021e044312_alt", False, False, "Kayla Barron"),
    # --- other men, alone (6) - incl. Koch's Artemis II crewmates
    ("NHQ202009150002", False, False, "Victor Glover"),
    ("iss064e044145", False, False, "Victor Glover"),
    ("jsc2026e022276", False, False, "Reid Wiseman"),
    ("jsc2026e022272", False, False, "Jeremy Hansen"),
    ("jsc2016e010167", False, False, "Thomas Pesquet"),
    ("jsc2024e066718", False, False, "Kirill Peskov"),
    # --- group photos WITHOUT Koch (10)
    ("iss064e002617", False, True, "Expedition 64 crew (3 people)"),
    ("iss064e020868", False, True, "Expedition 64 crew (7 people)"),
    ("jsc2020e032979", False, True, "Crew-1: Shannon Walker, Victor Glover, Mike Hopkins, Soichi Noguchi"),
    ("jsc2024e077921", False, True, "Crew-10: Kirill Peskov, Nichole Ayers, Anne McClain, Takuya Onishi"),
    ("iss073e0120087", False, True, "Anne McClain, Nichole Ayers"),
    ("iss074e0432750", False, True, "Chris Williams, Sophie Adenot, Jessica Meir"),
    ("iss068e005834", False, True, "Bob Hines, Frank Rubio, Jessica Watkins"),
    ("iss066e081649", False, True, "Kayla Barron, Raja Chari, Anton Shkaplerov"),
    ("jsc2026e022279", False, True, "Reid Wiseman, Vanessa Wyche"),
    ("iss073e0385730", False, True, "Crew-10: Kirill Peskov, Nichole Ayers, Anne McClain, Takuya Onishi"),
]
# Notes from looking at every photo (the answer key was checked by eye, not by the app).
VISUAL_NOTES = {
    "art002e016247": "VERY HARD: Koch's face is lit only by a screen, seen from the side, partly hidden by a camera",
    "jsc2026e022272": "Someone with long hair (possibly Koch) is cut off at the door edge; no face visible, so not counted",
    "KSC-20210714-PH-GEB01_0138": "Hard: Koch wears a hard hat; face small and turned sideways",
    "NHQ202305180019": "Hard: face in profile, wearing glasses",
    "art002e027808": "Hard: face tilted sideways (floating in space), glasses",
    "jsc2026e022273": "Hard: face turned sideways, cap, laughing",
    "130A4062-1": "Koch walks in the back of the group; face small",
    "NHQ202305180024": "Koch looks up and to the side, glasses",
    "iss059e035465": "Koch with glasses",
    "iss059-s-002": "Koch on the far right; Anne McClain also in photo",
    "KSC-20241119-PH-JBS01_0120": "Crew kneeling in front of ~20 staff; Koch in front row",
}
SEARCH2_ID ="jsc2018e095073_alt"      # 2018 official portrait of Christina Koch - not in the collection
SEARCH1_SOURCE_ID = "NHQ202009160004"  # the collection photo copied for search 1


def image_url(client: httpx.Client, nasa_id: str) -> str:
    hrefs = [item["href"] for item in client.get(f"{API}/asset/{nasa_id}").json()["collection"]["items"]]
    for suffix in ("~large.jpg", "~medium.jpg", "~orig.jpg"):
        for href in hrefs:
            if href.lower().endswith(suffix):
                return href.replace("http://", "https://")
    raise RuntimeError(f"No JPEG found for {nasa_id}")


def credit(client: httpx.Client, nasa_id: str) -> str:
    """The photo credit exactly as NASA lists it; refuses photos owned by other organisations."""
    data = client.get(f"{API}/search", params={"nasa_id": nasa_id}).json()["collection"]["items"][0]["data"][0]
    who = (data.get("photographer") or data.get("secondary_creator") or f"NASA {data.get('center', '')}").strip()
    caption = data.get("description", "").lower()
    if (any(word in who.lower() for word in ("spacex", "gagarin", "courtesy"))
            or any(word in caption for word in ("courtesy", "copyright", "©", "credit: spacex", "credit: gagarin"))):
        raise RuntimeError(f"{nasa_id} may be third-party content (credit: {who}) - choose another photo")
    return who


def download(client: httpx.Client, nasa_id: str, target: Path) -> str:
    who = credit(client, nasa_id)  # check the credit before downloading anything
    if not target.exists():
        target.write_bytes(client.get(image_url(client, nasa_id)).content)
        time.sleep(0.2)
    return who


def main() -> None:
    assert len(PHOTOS) == 40 and len({p[0] for p in PHOTOS}) == 40
    collection = OUT / "collection_40"
    search = OUT / "search"
    collection.mkdir(parents=True, exist_ok=True)
    search.mkdir(parents=True, exist_ok=True)

    order = list(PHOTOS)
    random.Random(2026).shuffle(order)  # fixed shuffle so file numbers never reveal the answer

    rows = []
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for number, (nasa_id, has_koch, group, people) in enumerate(order, 1):
            name = f"photo_{number:02d}.jpg"
            who = download(client, nasa_id, collection / name)
            rows.append({"file": f"collection_40/{name}", "nasa_id": nasa_id,
                         "source_url": f"https://images.nasa.gov/details/{nasa_id}", "credit": who,
                         "license": LICENSE, "people_in_caption": people,
                         "contains_target": "yes" if has_koch else "no", "group_photo": "yes" if group else "no",
                         "visual_check_note": VISUAL_NOTES.get(nasa_id, "")})
            print(f"{name}  {'KOCH ' if has_koch else '     '}{'group' if group else 'solo '}  {nasa_id}")

        who = download(client, SEARCH2_ID, search / "search2_koch_2018_portrait.jpg")
        rows.append({"file": "search/search2_koch_2018_portrait.jpg", "nasa_id": SEARCH2_ID,
                     "source_url": f"https://images.nasa.gov/details/{SEARCH2_ID}", "credit": who, "license": LICENSE,
                     "people_in_caption": "Christina Koch", "contains_target": "yes", "group_photo": "no",
                     "visual_check_note": "Different photo (2018, spacesuit portrait); not in the collection"})

    source = next(r for r in rows if r["nasa_id"] == SEARCH1_SOURCE_ID)
    shutil.copyfile(OUT / source["file"], search / "search1_exact_copy.jpg")
    rows.append({**source, "file": "search/search1_exact_copy.jpg",
                 "people_in_caption": f"Christina Koch (exact copy of {source['file']})"})

    with open(OUT / "manifest.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {len(PHOTOS)} collection photos + 2 search photos in {OUT}")


if __name__ == "__main__":
    main()
