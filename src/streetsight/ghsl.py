"""Average building height from the JRC Global Human Settlement Layer, used where Overture has no height.

GHS-BUILT-H R2023A, average net building height (ANBH), epoch 2018, 3 arc-second (~100 m) grid in WGS84,
CC BY 4.0 (Pesaresi et al., European Commission JRC). One global cloud-optimised GeoTIFF on the AWS Open
Data registry; only the window around the target is read.

Each cell holds the mean height of the buildings in it, so it is an estimate for any one building: it fills
the gap between "unknown" and a flat default, and is recorded as such in height_source.
"""
import time

import numpy as np

URL = ('https://jrc-ghsl.s3.amazonaws.com/ghs-built-h-anbh/r2023a/4326/3asec/2018/'
       'GHS_BUILT_H_ANBH_E2018_GLOBE_R2023A_4326_3ss_V1_0.tif')
SOURCE = 'ghs_built_h_anbh_100m'


def read_window(bbox, log=print):
    """(array, transform) of average building height (m) for bbox=(west, south, east, north), or None."""
    import rasterio
    from rasterio.windows import from_bounds
    west, south, east, north = bbox
    t = time.time()
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', GDAL_HTTP_MAX_RETRY='3'):
            with rasterio.open('/vsicurl/' + URL) as ds:
                win = from_bounds(west, south, east, north, ds.transform).round_offsets().round_lengths()
                arr = ds.read(1, window=win, boundless=True, fill_value=0).astype(float)
                tr = ds.window_transform(win)
    except rasterio.errors.RasterioIOError as exc:
        log(f'GHS building height unavailable ({exc}); missing heights keep the default')
        return None
    log(f'GHS building height: {arr.shape[1]}x{arr.shape[0]} cells in {time.time() - t:.0f}s')
    return arr, tr


def fill_heights(buildings, bbox, window=None, log=print):
    """Give buildings whose height_source starts with "assumed" the GHS cell height at their centroid
    (where that is above 0). Updates height_used_m and height_source in place and returns buildings."""
    if buildings is None or not len(buildings):
        return buildings
    todo = buildings['height_source'].astype(str).str.startswith('assumed')
    if not todo.any():
        return buildings
    window = window if window is not None else read_window(bbox, log=log)
    if window is None:
        return buildings
    arr, tr = window
    pts = buildings.loc[todo].geometry.representative_point()
    cols, rows = ~tr * (pts.x.to_numpy(), pts.y.to_numpy())
    cols, rows = np.floor(cols).astype(int), np.floor(rows).astype(int)
    ok = (rows >= 0) & (cols >= 0) & (rows < arr.shape[0]) & (cols < arr.shape[1])
    h = np.zeros(len(pts))
    h[ok] = arr[rows[ok], cols[ok]]
    use = h > 0
    idx = pts.index[use]
    buildings.loc[idx, 'height_used_m'] = np.round(h[use], 2)
    buildings.loc[idx, 'height_source'] = SOURCE
    log(f'GHS building height used for {int(use.sum())} of {int(todo.sum())} buildings without Overture height')
    return buildings
