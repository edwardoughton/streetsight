# Changelog

## 0.3.0 (unreleased)

First release as a standalone package (previously part of a cell-tower mapping project).

- Mapillary is searched before Google Street View (`providers=("mapillary", "google")`). Mapillary 360° panoramas are cropped locally to a view aimed at the target; perspective photos are used only if they face it. Google is queried only if Mapillary gives no confirmed image.
- Providers without credentials are skipped with a message.
- The target is generic: `target` defaults to `"structure"`, and `target_notes` adds target-specific guidance to the verifier prompt.
- FCC Antenna Structure Registration lookup is opt-in (`use_fcc=True`, `--use-fcc`).
- `verifier="auto"` (new default) uses Claude when credentials are set, else downloads images for review.
- Downloads are cached in the per-user cache folder and runs go to `./streetsight_runs`. `STREETSIGHT_DATA` keeps the earlier single-folder layout.
- Removed the tower-detection evidence modules (`lidar`, `nisar`, `lookalikes`).
