# streetsight

Give it the coordinates and height of a tall structure; it finds a street-level photo that shows it.

streetsight downloads the buildings, trees, terrain and roads around the target and checks every candidate camera position for line of sight before downloading anything. It tries free [Mapillary](https://www.mapillary.com/) imagery first and falls back to Google Street View. Images are fetched best-first until a verifier (Claude, a review file, or your own function) confirms the target is visible.

```python
import streetsight

result = streetsight.find_view(42.025504, -93.646039, target_height_m=34, target="campanile")
print(result)            # FOUND on attempt N: <source> image <id> at <distance> m, saved as images/...jpg
result.best              # dict for the confirmed image
result.candidates        # every camera position found, ranked

summary = streetsight.find_views("sites.csv", n_candidates=5, max_images_total=200)
```

From a terminal:
- one site: `streetsight 42.025504 -93.646039 --target-height 34 --target campanile`
- many sites: `streetsight --sites sites.csv --max-images-total 200`
- Mapillary only (no Google costs): add `--providers mapillary`
- ranking only, no image downloads: add `--candidates 0`

## Install

```bash
pip install streetsight            # core
pip install "streetsight[claude]"  # plus the Claude image verifier
```

Credentials are read from environment variables:

| Variable | Needed for |
|---|---|
| `MAPILLARY_ACCESS_TOKEN` | Mapillary (free; create a client token at mapillary.com/dashboard/developers) |
| `GOOGLE_MAPS_API_KEY` | Google Street View (Street View Static API enabled; images are billed) |
| `ANTHROPIC_API_KEY` | the Claude verifier |

A provider without credentials is skipped with a message. On Windows, variables set with `[Environment]::SetEnvironmentVariable(..., 'User')` are picked up for Mapillary even in shells opened before they were set.

## What one call does

1. **Target height.** `target_height_m=` if given. With `use_fcc=True` (US towers), the nearest constructed FCC Antenna Structure Registration within 100 m. Otherwise 30 m is assumed.
2. **Clutter and roads**, downloaded in parallel for the search area:
   - Overture buildings (which include OpenStreetMap buildings), using Overture's height, else floors × 3 m, else 5 m
   - Overture road segments
   - Meta/WRI 1 m canopy height
   - USGS 3DEP 10 m bare-earth elevation (US only; elsewhere the ground is treated as flat)
3. **Line of sight.** For each camera position, streetsight computes how high up the target the view clears buildings, trees and the ground. Heights are measured from the ground at the target's base, so cameras above or below the target are handled. The score combines the visible part with distance. It is a ranking heuristic between 0 and 1, not a calibrated probability. Images taken before the target's `built_date` are down-weighted to a tenth.
4. **Providers, in order** (`providers=("mapillary", "google")`):
   - **Mapillary.** One search returns every image within `radius_m`. 360° panoramas are kept and cropped locally to a view aimed at the target. Ordinary photos are kept only if the target lies within their field of view; they are used as taken, and the verifier is told where in the frame to look. The best-scoring images are downloaded, at least `min_separation_m` apart, because consecutive frames of one sequence share the same obstructions.
   - **Google Street View**, only if Mapillary gave no confirmed image. Points are sampled every `road_spacing_m` along roads, scored locally, and Street View metadata is queried at the best points first (`search="ranked"`). The search stops at `target_panoramas` panoramas, after 16 queries in a row that found nothing new, or at `max_metadata_queries`. Metadata requests are free but count against quota; image requests are billed.
5. **Download and verify.** Images are aimed at the target, with pitch and field of view chosen so the visible part fits in the frame. Close views are widened to allow for `position_uncertainty_m` of error in the target coordinates. The run stops at the first image the verifier confirms with confidence of at least `min_confidence`, or after `n_candidates` images per provider.

## Key parameters

| Parameter | Default | Meaning |
|---|---|---|
| `target_height_m` | FCC if `use_fcc`, else 30 | Target height above ground |
| `target` | `"structure"` | What to look for, used in the verifier prompt |
| `target_notes` | `""` | Extra verifier guidance, e.g. which look-alikes to reject |
| `providers` | `("mapillary", "google")` | Imagery sources, tried in order |
| `site_id` | none | Run folder name; without it, a timestamp and the coordinates |
| `radius_m` | 300 | Largest camera-to-target distance searched |
| `n_candidates` | 5 | Most images downloaded per provider; 0 = rank only |
| `verifier` | `"auto"` | `"auto"` (Claude if `ANTHROPIC_API_KEY` is set, else none), `"claude"`, `None`, a review CSV, or a callable |
| `min_confidence` | 0.6 | Verifier confidence needed to stop |
| `built_date` | FCC date if `use_fcc` | Images taken before this are down-weighted |
| `position_uncertainty_m` | 10 | Possible error in the target coordinates; widens close views |
| `min_separation_m` | 15 | Mapillary: minimum distance between downloaded images |
| `road_spacing_m` | 30 | Google: spacing of candidate points along roads |
| `search` | `"ranked"` | Google: `"ranked"` queries the best points first; `"exhaustive"` queries every point |
| `target_panoramas` | max(10, 2 × `n_candidates`) | Google: ranked search stops after this many panoramas |
| `max_metadata_queries` | 60 | Google: cap on metadata requests per site |
| `use_buildings`, `use_canopy`, `use_terrain` | True | Turn individual data sources off |
| `use_fcc` | False | Look up height and construction date in the FCC registry (US towers) |
| `out_dir` | `./streetsight_runs/<site_id>` | Run folder |
| `cache_dir` | per-user cache folder | Downloaded Overture, canopy and FCC data |

### Verifiers

A verifier is any callable `f(image_path, context) -> {"visible", "confidence", "reason"}`. `context` holds the target, distance, heading, pitch, field of view, height, image source and, for photos that could not be aimed, `target_offset_deg` (where in the frame the target should be).

To check images by hand, pass a review CSV with columns `image`, `visible`, `confidence` and `reason`:
1. Run with `n_candidates=1`.
2. Fill in the CSV.
3. Rerun with `n_candidates=2`. Images already on disk are reused without being downloaded or billed again, and the next image is fetched only where none has been confirmed.

Images missing from the CSV, or marked `unclear`, count as unconfirmed.

For targets with common look-alikes, add guidance, for example for cell towers:

```python
find_view(lat, lon, target="cell tower", use_fcc=True,
          target_notes="Do not count utility poles, streetlights, flagpoles or electricity pylons. "
                       "Disguised towers (a 'monopine' tree, a steeple) count if the structure is identifiable.")
```

## Batch runs

`find_views(sites, ...)` accepts a CSV path, a DataFrame or a list of dicts, with columns `lat`/`lon` (or `latitude`/`longitude`) and optionally `site_id` (or `id`). Per-site columns override keyword arguments: `target`, `target_notes`, `target_height_m`, `built_date`, `radius_m`, `n_candidates` and `position_uncertainty_m`.

Each site gets its own run folder. The parent folder also gets `summary.csv` (one row per site) and `summary.gpkg` (layers `sites`, `best_cameras` and `best_sight_lines`).

Re-running a batch skips sites already found, and sites that already ran with at least as many images and the same kind of verifier. `max_images_total` caps billed Google images across the batch; once it is reached, the remaining sites are still ranked. A Google key or quota error, or a refused Mapillary token, stops the batch. Any other error is written to that site's `error.txt` and the batch continues.

## Run folder

- `images/`: downloaded views. Google images are named `rank01_<distance>m_h<heading>_<pano>.jpg`; Mapillary images start `mly_`.
- `results.gpkg` (open in QGIS): `target`, `search_area`, `sample_points` (Google road points, with predicted score and query order), `panoramas` (every camera position, ranked and scored), `sight_lines`, `view_wedges`, `roads`, `buildings`
- `canopy_height.tif`, `terrain.tif`: the clips used for the line-of-sight check
- `candidates.csv`, `result.json`: every number behind the ranking, and the verifier verdicts
- `requests.csv`: every request, with keys and tokens removed
- `index.html`: contact sheet of the downloaded images

## Where files go

| What | Default | Override |
|---|---|---|
| Run folders | `./streetsight_runs` | `out_dir=`, or `STREETSIGHT_RUNS` |
| Download cache | per-user cache folder (`~/.cache/streetsight`, `%LOCALAPPDATA%\streetsight\Cache`) | `cache_dir=`, or `STREETSIGHT_CACHE` |

`STREETSIGHT_DATA=<folder>` puts both in one project folder: `<folder>/cache` and `<folder>/streetsight_runs`.

## Limits

- **Mapillary orientation:** panoramas are cropped assuming their middle column faces Mapillary's computed compass angle. User-contributed imagery can be mis-oriented or blurry, so a crop may miss the target even with a clear line of sight.
- **Terrain:** the 10 m elevation model smooths narrow features such as embankments and cuttings, and the 20 m nearest each end of the sight line is ignored. Outside the US the ground is treated as flat.
- **Trees:** canopy is treated as opaque, and its effect is discounted in the score. The canopy data comes from roughly 2018–2020 imagery and under-estimates tall single trees, especially street trees beside the camera.
- **Dates:** only the image month is compared with the construction date; demolition and replacement are not checked.
- **Google coverage:** the metadata API returns only the nearest panorama to each sample point, so rarely used side roads may be missed.
- **FCC registrations** do not prove cellular use, and many shorter towers are not registered.

## Data, licences and terms

| Source | Licence or terms |
|---|---|
| Overture buildings and transportation | ODbL and other open licences; see the Overture attribution guidance |
| Meta/WRI canopy height | CC BY 4.0 |
| USGS 3DEP elevation | US public domain |
| FCC ASR | US public data |
| Mapillary imagery | CC BY-SA 4.0; credit Mapillary and the contributor (recorded in `copyright`) |
| Google Street View | Google Maps Platform Terms |

Google's terms limit caching Street View content and may restrict sending it to third-party analysis services. Check your agreement before keeping images long-term or using the Claude verifier at scale.

## Development

```bash
pip install -e ".[claude,test]"
pytest
```

Releases are published to PyPI by a GitHub workflow when a GitHub release is published; see `CHANGELOG.md`.
