# Changelog

## 0.3.0 (unreleased)

First release as a standalone package (previously part of a cell-tower mapping project). The package now only finds and downloads imagery: `find_view(lat, lon, height)` returns the views most likely to show the target, ranked by line of sight.

- Mapillary is searched before Google Street View (`providers=("mapillary", "google")`); Google fills only the places Mapillary cannot (`n_images`, `max_google_images`). Mapillary 360° panoramas are cropped locally to a view aimed at the target; perspective photos are used only if the target is inside their horizontal and vertical field of view.
- Views are downloaded only if at least `min_visible_fraction` of the target is predicted visible. `ViewResult.images` lists them best first.
- Global data: terrain falls back from USGS 3DEP to GEDTM30 (30 m, global) instead of flat ground (`terrain_source`), and buildings without an Overture height or floor count get the GHS-BUILT-H 100 m average height instead of a flat 5 m (`use_ghs_heights`). `result.json` records the sources used.
- Providers without credentials are skipped with a message.
- Downloads are cached in the per-user cache folder and runs go to `./streetsight_runs`. `STREETSIGHT_DATA` keeps the earlier single-folder layout.
- Removed: image verification (the Claude verifier, review-CSV verifier, `verifier`, `min_confidence`, `target`, `target_notes`, the `claude` extra); the US-specific FCC lookup (`fcc`, `use_fcc`; pass `target_height_m` and `built_date` instead); the tower-detection evidence modules (`lidar`, `nisar`, `lookalikes`). `n_candidates` is now `n_images`, and `max_images_total` is now `max_google_images_total`.
