"""Bare-earth ground elevation from USGS 3DEP 1/3 arc-second (~10 m) DEM, public COGs.

Tiles are 1 x 1 degree, named by their north-west corner (n42w089 covers 41-42 N, 88-89 W).
Only the clipped window is downloaded. Coverage is the US and territories; outside it, fetch()
returns None and the pipeline falls back to flat ground. Public domain (USGS).
"""
import math
import time
from pathlib import Path

import numpy as np

URL = 'https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{t}/USGS_13_{t}.tif'


def tile_name(lat, lon):
    n = math.floor(lat) + 1
    ns = f'n{n:02d}' if n >= 0 else f's{-n:02d}'
    w = -math.floor(lon)
    ew = f'w{w:03d}' if w > 0 else f'e{-w:03d}'
    return ns + ew


def fetch(bbox, out_tif, log=print):
    """Clip the DEM to bbox=(west, south, east, north) and write out_tif (EPSG:4269, float32 metres).

    Returns out_tif, or None if no 3DEP tile covers the area.
    """
    import rasterio
    from rasterio.merge import merge
    out_tif = Path(out_tif)
    if out_tif.is_file():
        return out_tif
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
