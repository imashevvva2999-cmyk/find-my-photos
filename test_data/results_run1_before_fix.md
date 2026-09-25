# Face search test - run in the browser

Collection: 40 photos in test_data/collection_40 (12 show Christina Koch).
Answer key: test_data/manifest.csv (from NASA captions, checked by eye).

## Search 1 - exact copy of photo_30.jpg
Search image: test_data/search/search1_exact_copy.jpg

| Result | Count |
|---|---|
| Correct matches | 10 |
| Missed matches | 2 |
| Incorrect matches | 1 |

**Correct:** photo_07.jpg (solo: Christina Koch), photo_10.jpg (solo: Christina Koch), photo_14.jpg (solo: Christina Koch), photo_16.jpg (group: Expedition 59 crew incl. Anne McClain, Nick Hague, Christina Koch), photo_17.jpg (group: Reid Wiseman, Victor Glover, Christina Koch, Jeremy Hansen), photo_26.jpg (group: Jeremy Hansen, Christina Koch), photo_28.jpg (solo: Christina Koch), photo_30.jpg (solo: Christina Koch), photo_36.jpg (group: Victor Glover, Reid Wiseman, Christina Koch, Jeremy Hansen), photo_40.jpg (group: Reid Wiseman, Victor Glover, Christina Koch, Jeremy Hansen)

**Missed:** photo_20.jpg (group: Victor Glover, Christina Koch), photo_27.jpg (solo: Christina Koch)

**Incorrect:** photo_01.jpg (solo: Victor Glover)

<details><summary>Best face similarity in each photo (cut-off 0.363)</summary>

| Photo | Koch? | Best similarity | Returned? | Note |
|---|---|---|---|---|
| photo_30.jpg | yes | 1.000 | yes |  |
| photo_36.jpg | yes | 0.740 | yes | Crew kneeling in front of ~20 staff; Koch in front row |
| photo_40.jpg | yes | 0.700 | yes |  |
| photo_16.jpg | yes | 0.667 | yes | Koch on the far right; Anne McClain also in photo |
| photo_28.jpg | yes | 0.577 | yes | Koch with glasses |
| photo_14.jpg | yes | 0.504 | yes | Hard: face in profile, wearing glasses |
| photo_26.jpg | yes | 0.504 | yes | Koch looks up and to the side, glasses |
| photo_17.jpg | yes | 0.485 | yes | Koch walks in the back of the group; face small |
| photo_07.jpg | yes | 0.450 | yes | Hard: face turned sideways, cap, laughing |
| photo_01.jpg | no | 0.396 | yes |  |
| photo_10.jpg | yes | 0.389 | yes | Hard: Koch wears a hard hat; face small and turned sideways |
| photo_22.jpg | no | 0.349 | no |  |
| photo_38.jpg | no | 0.298 | no |  |
| photo_09.jpg | no | 0.295 | no |  |
| photo_18.jpg | no | 0.293 | no |  |
| photo_03.jpg | no | 0.289 | no |  |
| photo_15.jpg | no | 0.255 | no |  |
| photo_33.jpg | no | 0.251 | no |  |
| photo_06.jpg | no | 0.234 | no |  |
| photo_08.jpg | no | 0.227 | no |  |
| photo_37.jpg | no | 0.216 | no |  |
| photo_34.jpg | no | 0.203 | no |  |
| photo_21.jpg | no | 0.203 | no |  |
| photo_25.jpg | no | 0.197 | no |  |
| photo_12.jpg | no | 0.170 | no |  |
| photo_31.jpg | no | 0.168 | no |  |
| photo_02.jpg | no | 0.167 | no |  |
| photo_35.jpg | no | 0.159 | no |  |
| photo_24.jpg | no | 0.145 | no |  |
| photo_23.jpg | no | 0.138 | no |  |
| photo_29.jpg | no | 0.128 | no |  |
| photo_04.jpg | no | 0.118 | no |  |
| photo_13.jpg | no | 0.113 | no |  |
| photo_19.jpg | no | 0.099 | no |  |
| photo_27.jpg | yes | 0.094 | no | Hard: face tilted sideways (floating in space), glasses |
| photo_05.jpg | no | 0.072 | no | Someone with long hair (possibly Koch) is cut off at the door edge; no face visible, so not counted |
| photo_39.jpg | no | 0.040 | no |  |
| photo_11.jpg | no | 0.028 | no |  |
| photo_32.jpg | no | 0.002 | no |  |
| photo_20.jpg | yes | no face found | no | VERY HARD: Koch's face is lit only by a screen, seen from the side, partly hidden by a camera |

</details>

## Search 2 - different photo of Christina Koch (2018 portrait, not in the 40)
Search image: test_data/search/search2_koch_2018_portrait.jpg

| Result | Count |
|---|---|
| Correct matches | 10 |
| Missed matches | 2 |
| Incorrect matches | 0 |

**Correct:** photo_07.jpg (solo: Christina Koch), photo_10.jpg (solo: Christina Koch), photo_14.jpg (solo: Christina Koch), photo_16.jpg (group: Expedition 59 crew incl. Anne McClain, Nick Hague, Christina Koch), photo_17.jpg (group: Reid Wiseman, Victor Glover, Christina Koch, Jeremy Hansen), photo_26.jpg (group: Jeremy Hansen, Christina Koch), photo_28.jpg (solo: Christina Koch), photo_30.jpg (solo: Christina Koch), photo_36.jpg (group: Victor Glover, Reid Wiseman, Christina Koch, Jeremy Hansen), photo_40.jpg (group: Reid Wiseman, Victor Glover, Christina Koch, Jeremy Hansen)

**Missed:** photo_20.jpg (group: Victor Glover, Christina Koch), photo_27.jpg (solo: Christina Koch)

**Incorrect:** none

<details><summary>Best face similarity in each photo (cut-off 0.363)</summary>

| Photo | Koch? | Best similarity | Returned? | Note |
|---|---|---|---|---|
| photo_30.jpg | yes | 0.796 | yes |  |
| photo_40.jpg | yes | 0.731 | yes |  |
| photo_36.jpg | yes | 0.723 | yes | Crew kneeling in front of ~20 staff; Koch in front row |
| photo_16.jpg | yes | 0.716 | yes | Koch on the far right; Anne McClain also in photo |
| photo_28.jpg | yes | 0.608 | yes | Koch with glasses |
| photo_17.jpg | yes | 0.469 | yes | Koch walks in the back of the group; face small |
| photo_14.jpg | yes | 0.456 | yes | Hard: face in profile, wearing glasses |
| photo_26.jpg | yes | 0.440 | yes | Koch looks up and to the side, glasses |
| photo_07.jpg | yes | 0.383 | yes | Hard: face turned sideways, cap, laughing |
| photo_10.jpg | yes | 0.379 | yes | Hard: Koch wears a hard hat; face small and turned sideways |
| photo_03.jpg | no | 0.328 | no |  |
| photo_15.jpg | no | 0.266 | no |  |
| photo_38.jpg | no | 0.260 | no |  |
| photo_34.jpg | no | 0.243 | no |  |
| photo_09.jpg | no | 0.230 | no |  |
| photo_22.jpg | no | 0.221 | no |  |
| photo_35.jpg | no | 0.209 | no |  |
| photo_33.jpg | no | 0.209 | no |  |
| photo_21.jpg | no | 0.200 | no |  |
| photo_01.jpg | no | 0.197 | no |  |
| photo_06.jpg | no | 0.192 | no |  |
| photo_37.jpg | no | 0.189 | no |  |
| photo_08.jpg | no | 0.184 | no |  |
| photo_31.jpg | no | 0.165 | no |  |
| photo_24.jpg | no | 0.164 | no |  |
| photo_18.jpg | no | 0.150 | no |  |
| photo_25.jpg | no | 0.140 | no |  |
| photo_04.jpg | no | 0.139 | no |  |
| photo_05.jpg | no | 0.122 | no | Someone with long hair (possibly Koch) is cut off at the door edge; no face visible, so not counted |
| photo_29.jpg | no | 0.107 | no |  |
| photo_13.jpg | no | 0.096 | no |  |
| photo_32.jpg | no | 0.091 | no |  |
| photo_02.jpg | no | 0.089 | no |  |
| photo_12.jpg | no | 0.080 | no |  |
| photo_23.jpg | no | 0.077 | no |  |
| photo_11.jpg | no | 0.059 | no |  |
| photo_19.jpg | no | 0.051 | no |  |
| photo_27.jpg | yes | 0.039 | no | Hard: face tilted sideways (floating in space), glasses |
| photo_39.jpg | no | 0.013 | no |  |
| photo_20.jpg | yes | no face found | no | VERY HARD: Koch's face is lit only by a screen, seen from the side, partly hidden by a camera |

</details>
