"""Radar evidence from NASA-ISRO NISAR (L-band) geocoded SLC images (GSLC), read remotely from NASA Earthdata.

A steel tower is a strong, stable radar reflector, so in an average of several passes it stands out from
fields, roads and most low structures. On labelled Story County IA sites the brightest return within 30 m,
relative to the window median, was >= 40 for 36 of 46 towers and 42 of 166 non-towers (AUC 0.83): weaker
than lidar, but current, so it covers towers built after the lidar survey. Each look direction (ascending,
descending) is averaged separately, and the brighter of the two is used.

GSLC files are ~20 GB HDF5; only the 512x512 chunks around each site are read. Opening a file costs a few
seconds, so every site of a county is read from each file in one pass. Needs EARTHDATA_TOKEN (environment,
or on Windows the user's registry environment); it is sent as a bearer header, never printed.
"""
import concurrent.futures
import os

import numpy as np
import requests

SEARCH = 'https://api.daac.asf.alaska.edu/services/search/param'
GRID = 'science/LSAR/GSLC/grids/frequencyA'
HALF = 40  # window half-size in pixels (5 m posting -> 400 m window)
CORE = 6   # +/- pixels counted as "at the site" (30 m)


def token():
    t = os.environ.get('EARTHDATA_TOKEN')
    if not t and os.name == 'nt':
        import winreg
        try:
            t = winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment'), 'EARTHDATA_TOKEN')[0]
        except OSError:
            t = None
    if not t:
        raise RuntimeError('set EARTHDATA_TOKEN')
    return t


def find_gslc(bounds, per_direction=10):
    """Newest full-bandwidth (40 MHz) GSLC granules over a lon/lat box, up to per_direction each way."""
    x0, y0, x1, y1 = bounds
    wkt = f'POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))'
    res = requests.get(SEARCH, params={'platform': 'NISAR', 'processingLevel': 'GSLC', 'intersectsWith': wkt,
                                       'maxResults': 1000, 'output': 'jsonlite'}, timeout=120).json()['results']
    res = [r for r in res if r.get('productType') == 'GSLC']
    wide = [r for r in res if '_4005_' in r['granuleName']] or res
    out = []
    for d in ('ASCENDING', 'DESCENDING'):
        out += sorted((r for r in wide if r['flightDirection'] == d), key=lambda r: r['startTime'])[-per_direction:]
    return out


def _read(url, direction, points, tok):
    import fsspec
    import h5py
    from pyproj import Transformer
    fs = fsspec.filesystem('https', client_kwargs={'headers': {'Authorization': 'Bearer ' + tok}})
    with h5py.File(fs.open(url, 'rb', block_size=2 * 2**20, cache_type='blockcache'), 'r') as h:
        g = h[GRID]
        xs, ys = g['xCoordinates'][:], g['yCoordinates'][:]
        to = Transformer.from_crs(4326, int(g['projection'][()]), always_xy=True).transform
        out = {}
        for k, (lat, lon) in points.items():
            X, Y = to(lon, lat)
            c, r = int(round((X - xs[0]) / (xs[1] - xs[0]))), int(round((Y - ys[0]) / (ys[1] - ys[0])))
            if HALF <= c < len(xs) - HALF and HALF <= r < len(ys) - HALF:
                a = np.abs(g['HH'][r - HALF:r + HALF, c - HALF:c + HALF]).astype(np.float32) ** 2
                if np.isfinite(a).all() and a.mean() > 0:
                    out[k] = a
    return direction, out


def peaks(points, bounds, per_direction=10, workers=6, log=print):
    """points: {key: (lat, lon)}. Returns {key: {'nisar_peak': brighter-direction peak ratio, 'ascending': {...},
    'descending': {...}}} where each direction has the number of passes averaged and its peak ratio."""
    tok = token()
    grans = find_gslc(bounds, per_direction)
    log(f'NISAR: {len(grans)} GSLC passes over the area')
    acc = {}
    with concurrent.futures.ProcessPoolExecutor(workers) as pool:
        futs = [pool.submit(_read, g['downloadUrl'], g['flightDirection'].lower(), points, tok) for g in grans]
        for fu in concurrent.futures.as_completed(futs):
            try:
                d, out = fu.result()
            except Exception as e:  # noqa: BLE001 - a pass that fails to read is skipped
                log(f'NISAR pass skipped: {type(e).__name__}')
                continue
            for k, a in out.items():
                acc.setdefault(k, {}).setdefault(d, []).append(a)
    res = {}
    for k, dirs in acc.items():
        r = {}
        for d, stack in dirs.items():
            m = np.mean(stack, axis=0)
            core = m[HALF - CORE:HALF + CORE, HALF - CORE:HALF + CORE]
            r[d] = {'passes': len(stack), 'peak_ratio': round(float(core.max() / np.median(m)), 1)}
        r['nisar_peak'] = max(v['peak_ratio'] for v in r.values())
        res[k] = r
    return res
