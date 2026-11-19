# Changelog

## 0.3.0 (unreleased)

First release as a standalone package (previously part of a cell-tower mapping project).

- Mapillary is searched before Google Street View (`providers=("mapillary", "google")`). Mapillary 360° panoramas are cropped locally to a view aimed at the target; perspective photos are used only if the target is inside their horizontal and vertical field of view. Google is queried only if Mapillary gives no confirmed image.
- Global data: terrain falls back from USGS 3DEP to GEDTM30 (30 m, global) instead of flat ground (`terrain_source`), and buildings without an Overture height or floor count get the GHS-BUILT-H 100 m average height instead of a flat 5 m (`use_ghs_heights`). `result.json` records the sources used.
- Providers without credentials are skipped with a message.
- The target is generic: `target` defaults to `"structure"`, and `target_notes` adds target-specific guidance to the verifier prompt.
- `verifier="auto"` (new default) uses Claude when credentials are set, else downloads images for review.
- Downloads are cached in the per-user cache folder and runs go to `./streetsight_runs`. `STREETSIGHT_DATA` keeps the earlier single-folder layout.
- Removed the US-specific FCC Antenna Structure Registration lookup (`fcc`, `use_fcc`): pass `target_height_m` and `built_date` instead. Removed the tower-detection evidence modules (`lidar`, `nisar`, `lookalikes`).
