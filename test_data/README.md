# 40-photo face search test

## The photos
- **Source:** NASA Image and Video Library API (`images-api.nasa.gov`), a public API made for
  programs to download from. NASA's Media Usage Guidelines say NASA images "generally are not
  subject to copyright in the United States" and may be used for educational/informational
  purposes, with NASA credited. Only photos credited to NASA were used.
- **Target person:** NASA astronaut **Christina Koch**.
- **Answer key:** `manifest.csv` lists every file with its source link, photographer credit, the people
  named in NASA's caption, whether Koch is in it, whether it is a group photo, and notes
  from checking every photo by eye.

| Folder / file | What it is |
|---|---|
| `collection_40/photo_01.jpg … photo_40.jpg` | The event photos. Shuffled, neutral names, so file names reveal nothing. |
| `search/search1_exact_copy.jpg` | Byte-for-byte copy of `collection_40/photo_30.jpg` |
| `search/search2_koch_2018_portrait.jpg` | A different Koch photo (2018 spacesuit portrait), **not** in the 40 |
| `manifest.csv` | Answer key + source + credit for every file |
| `results_run1_before_fix.md` | Scores of the first browser run |
| `results_run2_after_fix.md` | Scores of the browser run after the fix |
| `results_run2_api_crosscheck.md` | Same test run again without the browser (identical results) |
| `browser_results_run*.json` | The result links the browser showed, used for scoring |
| `screenshots/` | Browser screenshots of both searches after the fix |

**12 of the 40 photos show Koch's face** (6 alone, 6 in group photos):
07, 10, 14, 16, 17, 20, 26, 27, 28, 30, 36, 40. The other 28 show other astronauts, including
similar-looking women and Koch's own crewmates. Some of those are group photos.

## Results

| | Correct | Missed | Incorrect |
|---|---|---|---|
| Search 1 (exact copy), before fix | 10 | 2 (photo_20, photo_27) | 1 (photo_01) |
| Search 2 (different photo), before fix | 10 | 2 (photo_20, photo_27) | 0 |
| **Search 1 (exact copy), after fix** | **11** | **1** (photo_20) | **1** (photo_01) |
| **Search 2 (different photo), after fix** | **11** | **1** (photo_20) | **0** |

- **Fixed:** strongly tilted faces (photo_27, Koch floating sideways in space) were read with wrong
  eye/mouth positions. `app/faces.py` now re-detects unsure faces in rotated copies.
- **Still missed:** photo_20. Koch's face is only a sliver lit by a screen in a dark cabin, and no
  face is detected at all.
- **Still incorrect in Search 1:** photo_01, a solo portrait of Victor Glover (similarity 0.396, just above
  the 0.363 cut-off). It was taken in the same studio session as the search photo. The site labels it
  "Weaker resemblance — please check". Raising the cut-off would remove it but lose real Koch photos.

## Repeat the test
```bash
./run.sh                                             # terminal 1
.venv/bin/python tests/fetch_nasa_test_photos.py     # terminal 2 (only if the photos are missing)
.venv/bin/python tests/evaluate_40.py --api          # upload, search twice, score -> results_api.md
```
