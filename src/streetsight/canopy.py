"""Meta / WRI global 1 m canopy height (Tolan et al. 2024, CC-BY 4.0), public COGs on AWS.

Tiles are Web-Mercator zoom-9 quadkeys, uint8 metres, ~1.19 m pixels, imagery mostly 2018-2020.
Only the clipped window is downloaded. The model tends to under-estimate tall isolated trees and
can mark towers, poles and buildings as canopy; visibility.py masks those areas.
"""
import math
import time
from pathlib import Path

PREFIX = '/vsis3/dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/chm'


def quadkey(lat, lon, z=9):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return ''.join(str(((x >> i) & 1) + 2 * ((y >> i) & 1)) for i in range(z - 1, -1, -1))


def fetch(bbox, out_tif, log=print):
    """Clip canopy height to bbox=(west, south, east, north) and write out_tif (EPSG:3857, uint8 m)."""
    import rasterio
    from rasterio.merge import merge
    from rasterio.warp import transform_bounds
    out_tif = Path(out_tif)
    if out_tif.is_file():
        return out_tif
    west, south, east, north = bbox
    keys = sorted({quadkey(y, x) for x in (west, east) for y in (south, north)})
    t = time.time()
    with rasterio.Env(AWS_NO_SIGN_REQUEST='YES', AWS_REGION='us-east-1', GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR'):
        srcs = [rasterio.open(f'{PREFIX}/{k}.tif') for k in keys]
        try:
            bounds = transform_bounds('EPSG:4326', srcs[0].crs, west, south, east, north)
            arr, tr = merge(srcs, bounds=bounds)
            profile = {'driver': 'GTiff', 'dtype': arr.dtype, 'count': 1, 'crs': srcs[0].crs, 'transform': tr,
                       'height': arr.shape[1], 'width': arr.shape[2], 'compress': 'deflate'}
        finally:
            for s in srcs:
                s.close()
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_tif, 'w', **profile) as dst:
        dst.write(arr[:1])
    log(f'Canopy height: tiles {keys}, {arr.shape[2]}x{arr.shape[1]} px in {time.time() - t:.0f}s')
    return out_tif
