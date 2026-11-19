# streetsight

Give it a coordinate; it finds the Street View panorama most likely to show the target and checks the image.

```python
import streetsight
result = streetsight.find_view(41.288927, -88.217081, target="cell tower", site_id="IL_tower")
print(result)            # FOUND on attempt 1: pano ... image images/rank01_....jpg
result.best              # dict for the verified panorama
result.candidates        # every panorama found, ranked

summary = streetsight.find_views("sites.csv", n_candidates=5, max_images_total=200)
```

From a terminal:
- one site: `streetsight 41.288927 -88.217081 --site-id IL_tower --candidates 5`
- many sites: `streetsight --sites sites.csv --max-images-total 200`
- ranking only, at no cost: add `--candidates 0`.

Run these from the repository root. Runs are saved to `data/streetsight_runs/<site_id>/`, and downloads are cached in `data/cache/`. Set `STREETSIGHT_DATA` to use a different data folder.

## What one call does

1. **Target height.** `target_height_m=` if given. Otherwise the nearest constructed FCC Antenna Structure Registration within 100 m. If neither exists, 30 m is assumed.
2. **Clutter and roads.** These are downloaded in parallel for the search area:
   - Overture buildings, using Overture's height, else floors × 3 m, else 5 m
   - Overture road segments
   - Meta/WRI 1 m canopy height
   - USGS 3DEP 10 m bare-earth elevation (US only; elsewhere the ground is treated as flat)
3. **Candidates, ranked before searching.** Points are sampled every `road_spacing_m` along roads within `radius_m`, plus the road point nearest the target.
   - Each point gets the line-of-sight check from step 4, computed locally, before any Google request.
   - Street View metadata is then queried at the best points first. Points within one search radius of a panorama already found are skipped.
   - The search stops when it has `target_panoramas` panoramas (default 10, or twice `n_candidates` if larger). It also stops after 16 searches in a row that found nothing new, or at `max_metadata_queries`.
   - Metadata requests are free but count against the Google daily quota.
   - On the four sites tested this used 40–60% fewer searches than querying every point, with the same top-ranked panoramas. `search="exhaustive"` queries every point, as before.
4. **Line of sight.** For each road point, and then for each panorama actually found, the module computes how high up the target the view clears buildings, trees and the ground between camera and target. Heights are measured from the ground at the target's base, so a camera lower or higher than the target is handled.
   - The score combines that visible part with the distance into a heuristic between 0 and 1. It ranks candidates; it is not a calibrated probability.
   - Panoramas photographed before the target's construction date are down-weighted to a tenth. The date comes from `built_date`, or else the FCC record.
5. **Download and verify.** Images are downloaded best-first, aimed at the target. Pitch and field of view are chosen so the visible part of the target fits in the frame. Close views are widened so the target stays in frame even if its coordinates are off by `position_uncertainty_m`. Each image goes to the verifier. The run stops at the first image the verifier confirms with confidence of at least `min_confidence`, or after `n_candidates` images.

## Key parameters

| Parameter | Default | Meaning |
|---|---|---|
| `site_id` | none | Run folder name; without it, a timestamp and the coordinates |
| `radius_m` | 300 | Largest camera-to-target distance searched |
| `n_candidates` | 5 | Most images downloaded (billed Static API requests); 0 = rank only |
| `verifier` | `"claude"` | `"claude"`; `None` (download all `n_candidates` for human review); or a callable `f(image_path, context) -> {"visible", "confidence", "reason"}` |
| `min_confidence` | 0.6 | Verifier confidence needed to stop |

The verifier can also be a review CSV with columns `image`, `visible`, `confidence` and `reason`. Images missing from the CSV, or marked `unclear`, count as unconfirmed. To check images by hand:
1. Run with `n_candidates=1`.
2. Fill in the CSV.
3. Rerun with `n_candidates=2`: images already on disk are reused without being billed again, and the next panorama is downloaded only where none has been confirmed yet.
| `target` | `"cell tower"` | Wording used in the verifier prompt |
| `target_height_m` | FCC, else 30 | Target height above ground |
| `built_date` | FCC construction date | Panoramas taken before this are down-weighted |
| `position_uncertainty_m` | 10 | Possible error in the target coordinates; widens close views |
| `road_spacing_m` | 30 | Spacing of candidate points along roads |
| `search` | `"ranked"` | `"ranked"`: query the best points first and stop early; `"exhaustive"`: query every point |
| `target_panoramas` | max(10, 2 × `n_candidates`) | Ranked search stops after finding this many panoramas |
| `max_metadata_queries` | 60 | Cap on metadata requests per site |
| `use_buildings`, `use_canopy`, `use_terrain`, `use_fcc` | True | Turn individual data sources off |
| `out_dir` | `data/streetsight_runs/<site_id>` | Run folder |
| `cache_dir` | `data/cache`, or `STREETSIGHT_CACHE` | Downloaded Overture and FCC data |

## Batch runs

`find_views(sites, ...)` accepts:
- a CSV path, a DataFrame or a list of dicts;
- columns `lat`/`lon` (or `latitude`/`longitude`), and optionally `site_id` (or `id`);
- optional per-site columns that override the keyword arguments: `target`, `target_height_m`, `built_date`, `radius_m`, `n_candidates` and `position_uncertainty_m`.

Each site gets its own run folder. The parent folder also gets:
- `summary.csv`: one row per site;
- `summary.gpkg`: layers `sites`, `best_cameras` and `best_sight_lines`.

Re-running the same batch skips two kinds of site:
- sites already found;
- sites that already ran with at least as many images and the same kind of verifier.

`max_images_total` caps billed images across the whole batch. Once the cap is reached, the remaining sites are still ranked, at no cost. A Google key or quota error stops the batch. Any other error is written to that site's `error.txt`, and the batch continues.

## Run folder

- `images/`: downloaded views, named by rank, distance, heading and panorama ID
- `results.gpkg`: open in QGIS. Layers:
  - `target`
  - `search_area`
  - `sample_points` (every road point, with its predicted score and whether and in what order it was queried)
  - `panoramas` (all of them, ranked and scored)
  - `sight_lines`
  - `view_wedges`
  - `roads`
  - `buildings`
- `canopy_height.tif`, `terrain.tif`: the canopy and elevation clips used for the line-of-sight check
- `candidates.csv`, `result.json`: every number behind the ranking and the verifier verdicts
- `requests.csv`: every Google request, with the key replaced by `REDACTED`
- `index.html`: contact sheet of the downloaded images

## Setup

```powershell
pip install -e ".[claude]"          # from the repository root
[Environment]::SetEnvironmentVariable('GOOGLE_MAPS_API_KEY', '...', 'User')
[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', '...', 'User')   # only for verifier="claude"
```

The Google key needs the Street View Static API enabled. The Claude verifier uses `claude-opus-5-5`, structured JSON output, and server-side refusal fallback (`fallbacks="default"`).

## Limits

- **Terrain:** the 10 m elevation model smooths narrow features such as embankments and cuttings, and the 20 m nearest each end of the sight line is ignored. Outside the US the ground is treated as flat.
- **Trees:** canopy is treated as opaque, and its effect is discounted in the score. The canopy data comes from roughly 2018–2020 imagery and under-estimates tall single trees, especially street trees beside the camera.
- **Panorama dates:** only the panorama's month is compared with the construction date; demolition and replacement are not checked.
- **Panoramas:** the metadata API returns only the nearest panorama to each sample point, so rarely used side roads may be missed.
- **Registrations:** FCC registration does not prove cellular use, and many shorter towers are not registered.

## Data, licences and terms

| Source | Licence or terms |
|---|---|
| Overture buildings and transportation | ODbL and other open licences; see the Overture attribution guidance |
| Meta/WRI canopy height | CC-BY 4.0 |
| USGS 3DEP elevation | US public domain |
| FCC ASR | US public data |
| Google Street View | Google Maps Platform Terms |

Google's terms limit caching Street View content and may restrict sending it to third-party analysis services. Check your agreement before saving images long-term or using the Claude verifier at scale.
