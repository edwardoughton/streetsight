"""Bare-earth ground elevation, read remotely from public cloud-optimised GeoTIFFs (only the window needed).

- USGS 3DEP 1/3 arc-second (~10 m), US and territories, public domain. Tiles are 1 x 1 degree, named by
  their north-west corner (n42w089 covers 41-42 N, 88-89 W).
- GEDTM30 v1.2 (OpenGeoHub, Ho et al. 2025), 30 m global ensemble digital terrain model, CC BY 4.0: one
  global file, used wherever 3DEP has no tile. Antarctica is not covered.

source="auto" (default) uses 3DEP where available, else GEDTM30. If neither has data, fetch() returns None
and the pipeline treats the ground as flat.
"""
import math
import time
from pathlib import Path

import numpy as np

URL = 'https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{t}/USGS_13_{t}.tif'
GEDTM30_URL = ('https://s3.opengeohub.org/global/dtm/v1.2/'
               'gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif')
SOURCES = {'3dep': 'usgs_3dep_13', 'gedtm30': 'gedtm30_v1.2'}


def tile_name(lat, lon):
    n = math.floor(lat) + 1
    ns = f'n{n:02d}' if n >= 0 else f's{-n:02d}'
    w = -math.floor(lon)
    ew = f'w{w:03d}' if w > 0 else f'e{-w:03d}'
    return ns + ew


def fetch(bbox, out_tif, source='auto', log=print):
    """Clip a DEM to bbox=(west, south, east, north) and write out_tif (float32 metres).

    source : "auto" (3DEP, else GEDTM30), "3dep" or "gedtm30".
    Returns (out_tif, source name) or (None, None) if no elevation data covers the area.
    """
    if source not in ('auto', '3dep', 'gedtm30'):
        raise ValueError(f'terrain source must be "auto", "3dep" or "gedtm30", not {source!r}')
    out_tif = Path(out_tif)
    used = out_tif.with_suffix('.source.txt')
    if out_tif.is_file() and used.is_file():
        return out_tif, used.read_text().strip()
    got = fetch_3dep(bbox, out_tif, log) if source in ('auto', '3dep') else None
    name = SOURCES['3dep'] if got else None
    if got is None and source in ('auto', 'gedtm30'):
        got = fetch_gedtm30(bbox, out_tif, log)
        name = SOURCES['gedtm30'] if got else None
    if got:
        used.write_text(name)
    return got, name


def fetch_gedtm30(bbox, out_tif, log=print):
    """Clip GEDTM30 to bbox and write out_tif (EPSG:4326, float32 metres); None if no data there."""
    import rasterio
    from rasterio.windows import from_bounds
    out_tif = Path(out_tif)
    west, south, east, north = bbox
    t = time.time()
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', GDAL_HTTP_MAX_RETRY='3'):
        try:
            with rasterio.open('/vsicurl/' + GEDTM30_URL) as ds:
                win = from_bounds(west, south, east, north, ds.transform).round_offsets().round_lengths()
                arr = ds.read(1, window=win, boundless=True, fill_value=-999999.0).astype('float32')
                tr, crs, nodata = ds.window_transform(win), ds.crs, ds.nodata
        except rasterio.errors.RasterioIOError as exc:
            log(f'Terrain: GEDTM30 unavailable ({exc})')
            return None
    if nodata is not None:
        arr[arr == nodata] = -999999.0
    arr[~np.isfinite(arr) | (arr > 1e5)] = -999999.0
    valid = arr[arr > -1e5]
    if not valid.size:
        log('Terrain: GEDTM30 has no data here')
        return None
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_tif, 'w', driver='GTiff', dtype='float32', count=1, crs=crs, transform=tr,
                       height=arr.shape[0], width=arr.shape[1], nodata=-999999.0, compress='deflate') as dst:
        dst.write(arr, 1)
    log(f'Terrain: GEDTM30, {arr.shape[1]}x{arr.shape[0]} px, {valid.min():.0f}-{valid.max():.0f} m '
        f'in {time.time() - t:.0f}s')
    return out_tif


def fetch_3dep(bbox, out_tif, log=print):
    """Clip USGS 3DEP to bbox and write out_tif (EPSG:4269, float32 metres); None if no 3DEP tile covers it."""
    import rasterio
    from rasterio.merge import merge
    out_tif = Path(out_tif)
    west, south, east, north = bbox
    names = sorted({tile_name(y, x) for x in (west, east) for y in (south, north)})
    t = time.time()
    srcs = []
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', GDAL_HTTP_MAX_RETRY='3'):
        try:
            for name in names:
                try:
                    srcs.append(rasterio.open('/vsicurl/' + URL.format(t=name)))
                except rasterio.errors.RasterioIOError:
                    log(f'Terrain: no 3DEP tile {name}')
            if not srcs:
                return None
            arr, tr = merge(srcs, bounds=(west, south, east, north), nodata=-999999.0)
            profile = {'driver': 'GTiff', 'dtype': 'float32', 'count': 1, 'crs': srcs[0].crs, 'transform': tr,
                       'height': arr.shape[1], 'width': arr.shape[2], 'nodata': -999999.0, 'compress': 'deflate'}
        finally:
            for s in srcs:
                s.close()
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_tif, 'w', **profile) as dst:
        dst.write(arr[:1].astype('float32'))
    valid = arr[0][arr[0] > -1e5]
    log(f'Terrain: 3DEP {names}, {arr.shape[2]}x{arr.shape[1]} px, '
        f'{valid.min():.0f}-{valid.max():.0f} m in {time.time() - t:.0f}s' if valid.size else 'Terrain: no data')
    return out_tif if valid.size else None


class Ground:
    """Bilinear ground elevation sampler: Ground(tif)(lons, lats) -> metres. Missing data -> nan."""

    def __init__(self, tif):
        import rasterio
        from pyproj import Transformer
        with rasterio.open(tif) as ds:
            self.z = ds.read(1).astype(float)
            self.inv = ~ds.transform
            nodata = ds.nodata
            self.to = Transformer.from_crs('EPSG:4326', ds.crs, always_xy=True)
        if nodata is not None:
            self.z[self.z == nodata] = np.nan
        self.z[self.z < -1e5] = np.nan

    def __call__(self, lons, lats):
        lons, lats = np.atleast_1d(np.asarray(lons, float)), np.atleast_1d(np.asarray(lats, float))
        c, r = self.inv * self.to.transform(lons, lats)
        c, r = np.asarray(c) - 0.5, np.asarray(r) - 0.5  # pixel centres
        c0, r0 = np.floor(c).astype(int), np.floor(r).astype(int)
        fc, fr = c - c0, r - r0
        h, w = self.z.shape
        out = np.full(lons.shape, np.nan)
        ok = (c0 >= 0) & (r0 >= 0) & (c0 + 1 < w) & (r0 + 1 < h)
        z, a, b, fa, fb = self.z, r0[ok], c0[ok], fr[ok], fc[ok]
        out[ok] = (z[a, b] * (1 - fa) * (1 - fb) + z[a, b + 1] * (1 - fa) * fb +
                   z[a + 1, b] * fa * (1 - fb) + z[a + 1, b + 1] * fa * fb)
        return out
