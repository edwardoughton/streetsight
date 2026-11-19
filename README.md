# streetsight

Wouldn't it be nice to have an actual street image of something you're interested in? 

The aim of streetsight is to help us optimize the probability of getting a low cost image for a target of interest, accounting for image availability and line-of-sight blockages (from buildings, trees, etc.). 

Simply give streetsight your target coordinates and it downloads street images most likely to show it. You can also provide an expected height for your target. I built this package (with a little help from Claude) to preference using free [Mapillary](https://www.mapillary.com/) imagery first, and if this isn't available, it can use Google Street View as a backup option.

You can use streetsight anywhere in the world, as every data source is global, read remotely a small window at a time, and needs no account (except for your Mapillary/Streetview API keys). For now, I do use a higher resolution terrain model for the USA.

```python
import streetsight

result = streetsight.find_view(42.025504, -93.646039, 34)   # latitude, longitude, height in metres
print(result)            # N images (by provider); best: <source> <id> at <distance> m, <share> predicted visible
result.images            # downloaded views, best first, each with its predicted visibility
result.candidates        # every camera position found, ranked

summary = streetsight.find_views("sites.csv")    # columns lat, lon, and optionally site_id, target_height_m
```

From a terminal:
- one site: `streetsight 42.025504 -93.646039 34`
- many sites: `streetsight --sites sites.csv --max-google-images 200`
- Mapillary only (no Google costs): add `--providers mapillary`
- rank camera positions without downloading: add `--images 0`

streetsight predicts what each camera can see, without looking at the images. The only caveat is that a view it ranks highly can still miss the target if the map data is out of date or the target's coordinates are off.

## Install

```bash
pip install streetsight
```

Credentials are read from environment variables:

| Variable | Needed for |
|---|---|
| `MAPILLARY_ACCESS_TOKEN` | Mapillary (free, just create a client token at mapillary.com/dashboard/developers) |
| `GOOGLE_MAPS_API_KEY` | Google Street View (Street View Static API enabled, images are billed beyond ~5k/month) |

A provider without credentials is skipped with a message. On Windows, a Mapillary token set with `[Environment]::SetEnvironmentVariable(..., 'User')` is picked up even in shells opened before it was set.

## What one call does

1. **Clutter and roads**, downloaded in parallel for the search area:

   | Layer | Source | Resolution | Coverage |
   |---|---|---|---|
   | Building footprints | Overture buildings (OpenStreetMap, Microsoft and Google footprints, and others) | per building | global |
   | Building heights | Overture `height`, else `num_floors` × 3 m, else the GHS-BUILT-H average building height of the surrounding 100 m cell, else 5 m | per building / 100 m | global |
   | Trees | Meta/WRI canopy height | 1 m | global |
   | Terrain (bare earth) | USGS 3DEP where available, else GEDTM30 | 10 m / 30 m | US / global except Antarctica |
   | Roads | Overture transportation segments | per segment | global |

2. **Line of sight.** For each camera position, streetsight computes how high up the target the view clears buildings, trees and the ground, giving the fraction of the target predicted visible. Heights are measured from the ground at the target's base, so cameras above or below the target are handled. A score combines visibility with distance (closer is better, scaled to the target's height) and ranks the views. Images taken before the target's `built_date`, if given, are decreased in weight.
3. **Mapillary.** One search returns every image within `radius_m`. 360° panoramas are cropped locally to a view aimed at the target. Ordinary photos are kept only if the target lies inside their field of view. They are then used as taken, and `target_offset_deg` records where in the frame the target should be. The best views with at least `min_visible_fraction` of the target predicted visible are downloaded, at least `min_separation_m` apart, because neighbouring frames of one sequence share the same obstructions.
4. **Google Street View**, only if Mapillary supplied fewer than `n_images`. Points are sampled every `road_spacing_m` along roads and scored locally first. Street View metadata is then queried at the best points only (`search="ranked"`), which is free but counts against quota. The best panoramas fill the remaining places.
5. **Aiming.** Pitch and field of view are chosen so the visible part of the target fits in the frame. Close views are widened to allow for `position_uncertainty_m` of error in the target coordinates.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `target_height_m` | 30 | Target height above ground (third positional argument) |
| `n_images` | 3 | Images to download; 0 ranks camera positions only |
| `min_visible_fraction` | 0.25 | Download only views with at least this fraction of the target predicted visible |
| `providers` | `("mapillary", "google")` | Imagery sources, in order of preference |
| `max_google_images` | none | Cap on billed Google images for the call |
| `site_id` | none | Run folder name; without it, a timestamp and the coordinates |
| `radius_m` | 300 | Largest camera-to-target distance searched |
| `built_date` | none | Construction date, if known; images taken before it are down-weighted |
| `position_uncertainty_m` | 10 | Possible error in the target coordinates; widens close views |
| `min_separation_m` | 15 | Mapillary: minimum distance between downloaded images |
| `road_spacing_m` | 30 | Google: spacing of candidate points along roads |
| `search` | `"ranked"` | Google: `"ranked"` queries the best points first; `"exhaustive"` queries every point |
| `max_metadata_queries` | 60 | Google: cap on metadata requests per site |
| `use_buildings`, `use_canopy`, `use_terrain` | True | Turn individual data sources off |
| `terrain_source` | `"auto"` | `"auto"` (3DEP in the US, else GEDTM30), `"3dep"` or `"gedtm30"` |
| `use_ghs_heights` | True | Fill missing building heights from GHS-BUILT-H |
| `out_dir` | `./streetsight_runs/<site_id>` | Run folder |
| `cache_dir` | per-user cache folder | Downloaded Overture data |

## Batch runs

`find_views(sites, ...)` accepts a CSV path, a DataFrame or a list of dicts, with columns `lat`/`lon` (or `latitude`/`longitude`) and optionally `site_id` (or `id`) and `target_height_m` (or `height`). Per-site columns override keyword arguments: `target_height_m`, `built_date`, `radius_m`, `n_images`, `min_visible_fraction` and `position_uncertainty_m`.

Each site gets its own run folder. The parent folder also gets `summary.csv` (one row per site: images found per provider, and the best view) and `summary.gpkg` (layers `sites`, `best_cameras` and `best_sight_lines`).

Re-running a batch skips sites that already have their images. Images already on disk are never downloaded again. `max_google_images_total` (`--max-google-images` with `--sites`) caps billed Google images across the batch. Once it is used up, the remaining sites still get Mapillary images. A Google key or quota error, or a refused Mapillary token, stops the batch. Any other error is written to that site's `error.txt` and the batch continues.

## Run folder

- `images/`: the downloaded views. Google images are named `rank01_<distance>m_h<heading>_<pano>.jpg`; Mapillary images start `mly_`.
- `results.gpkg` (open in QGIS): `target`, `search_area`, `sample_points` (Google road points, with predicted score and query order), `panoramas` (every camera position, ranked and scored), `sight_lines`, `view_wedges`, `roads`, `buildings`
- `canopy_height.tif`, `terrain.tif`: the clips used for the line-of-sight check
- `candidates.csv`, `result.json`: every number behind the ranking, the data sources used, and the downloaded images
- `requests.csv`: every request, with keys and tokens removed
- `index.html`: contact sheet of the downloaded images

## Where files go

| What | Default | Override |
|---|---|---|
| Run folders | `./streetsight_runs` | `out_dir=`, or `STREETSIGHT_RUNS` |
| Download cache | per-user cache folder (`~/.cache/streetsight`, `%LOCALAPPDATA%\streetsight\Cache`) | `cache_dir=`, or `STREETSIGHT_CACHE` |

`STREETSIGHT_DATA=<folder>` puts both in one project folder: `<folder>/cache` and `<folder>/streetsight_runs`.

## Limits

- **Predicted, not checked:** visibility is computed from map data; the images themselves are not analysed.
- **Building heights:** outside North America and Europe most buildings have no Overture height or floor count, and get the GHS-BUILT-H average for their 100 m cell (2018). A tall building in a low-rise cell is under-estimated, and new buildings are missed.
- **Trees:** canopy is treated as opaque, and its effect is discounted in the score. The canopy data comes from roughly 2018–2020 imagery and under-estimates tall single trees, especially street trees beside the camera.
- **Terrain:** the 10 m (US) and 30 m (global) elevation models smooth narrow features such as embankments and cuttings, and the 20 m nearest each end of the sight line is ignored.
- **Other clutter** (walls, noise barriers, bridges, signs, vehicles) is not modelled.
- **Mapillary** photos are contributed by users: some are blurred or mis-oriented. Panoramas are cropped assuming their middle column faces Mapillary's computed compass angle.
- **Google coverage:** the metadata API returns only the nearest panorama to each sample point, so rarely used side roads may be missed.

## Data, licences and terms

| Source | Licence or terms |
|---|---|
| Overture buildings and transportation | ODbL and other open licences; see the Overture attribution guidance |
| Meta/WRI canopy height (Tolan et al. 2024) | CC BY 4.0 |
| GHS-BUILT-H R2023A, European Commission JRC | CC BY 4.0 |
| GEDTM30 v1.2, OpenGeoHub (Ho et al. 2025) | CC BY 4.0 |
| USGS 3DEP elevation | US public domain |
| Mapillary imagery | CC BY-SA 4.0; credit Mapillary and the contributor (recorded in `copyright`) |
| Google Street View | Google Maps Platform Terms |

Google's terms limit caching Street View content. Check your agreement before keeping Google images long-term.

## Development

```bash
pip install -e ".[test]"
pytest
```

Releases are published to PyPI by a GitHub workflow when a GitHub release is published; see `CHANGELOG.md`.
