"""Height evidence from USGS 3DEP airborne lidar, read from the public Entwine (EPT) point clouds on AWS.

A cell tower is a tall, narrow structure: in lidar it is a column of returns tens of metres above the ground,
whereas look-alike pads (gas regulator stations, tanks, culverts, slabs) are no more than a few metres high.
Lidar dates from the survey (often several years before the imagery), so towers built since will not show.
"""
import json
import math
from functools import lru_cache

import numpy as np
import requests
from pyproj import Transformer
from shapely.geometry import Point, shape

RESOURCES = 'https://raw.githubusercontent.com/hobuinc/usgs-lidar/master/boundaries/resources.geojson'
UA = {'User-Agent': 'tower-research-client/0.1'}
TO_MERC = Transformer.from_crs(4326, 3857, always_xy=True)


def _get(url, binary=False):
    for k in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content if binary else r.json()
        except requests.RequestException:
            if k == 3:
                raise


@lru_cache(maxsize=1)
def _resources():
    return [(f['properties'], shape(f['geometry'])) for f in _get(RESOURCES)['features']]


def projects_at(lon, lat):
    """EPT projects covering a point, newest first (by the year in the project name, if any)."""
    p = Point(lon, lat)
    hits = [props for props, geom in _resources() if geom.contains(p)]
    year = lambda n: max([int(t) for t in n.replace('-', '_').split('_') if t.isdigit() and len(t) == 4] or [0])
    return sorted(hits, key=lambda h: -year(h['name']))


@lru_cache(maxsize=16)
def _ept(url):
    return _get(url)


@lru_cache(maxsize=4096)
def _hierarchy(base, key):
    return _get(f'{base}/ept-hierarchy/{key}.json')


def _nodes(base, info, box):
    """Keys of every EPT node whose cube intersects the 2D box (xmin, ymin, xmax, ymax) in EPSG:3857."""
    b = info['bounds']
    out, stack = [], [(0, 0, 0, 0)]
    hier = dict(_hierarchy(base, '0-0-0-0'))
    while stack:
        d, x, y, z = stack.pop()
        key = f'{d}-{x}-{y}-{z}'
        n = hier.get(key)
        if n is None:
            continue
        if n == -1:  # this subtree's counts live in their own file
            hier.update(_hierarchy(base, key))
            n = hier.get(key, 0)
        size = (b[3] - b[0]) / 2 ** d
        x0, y0 = b[0] + x * size, b[1] + y * size
        if x0 > box[2] or x0 + size < box[0] or y0 > box[3] or y0 + size < box[1]:
            continue
        if n > 0:
            out.append(key)
        for dx in (0, 1):
            for dy in (0, 1):
                for dz in (0, 1):
                    stack.append((d + 1, 2 * x + dx, 2 * y + dy, 2 * z + dz))
    return out


def points(lon, lat, radius_m=40, project=None):
    """Lidar returns within radius_m of a point: (project name, array of [dx_m, dy_m, z_m, classification]).
    dx, dy are ground metres east and north of the point. Returns (None, None) where 3DEP has no coverage."""
    if project is None:
        # project outlines are coarse, so a project can "cover" a point it has no returns at: try the next one
        for proj in projects_at(lon, lat):
            name, pts = points(lon, lat, radius_m, proj)
            if len(pts):
                return name, pts
        return None, None
    import laspy
    proj = project
    base = proj['url'].rsplit('/', 1)[0]
    info = _ept(proj['url'])
    x, y = TO_MERC.transform(lon, lat)
    k = 1 / math.cos(math.radians(lat))  # Web Mercator metres per ground metre
    r = radius_m * k
    rows = []
    for key in _nodes(base, info, (x - r, y - r, x + r, y + r)):
        raw = _get(f'{base}/ept-data/{key}.laz', binary=True)
        if not raw:
            continue
        las = laspy.read(__import__('io').BytesIO(raw))
        px, py = np.asarray(las.x), np.asarray(las.y)
        m = (px - x) ** 2 + (py - y) ** 2 <= r * r
        if m.any():
            rows.append(np.column_stack([(px[m] - x) / k, (py[m] - y) / k, np.asarray(las.z)[m],
                                         np.asarray(las.classification)[m]]))
    pts = np.vstack(rows) if rows else np.empty((0, 4))
    return proj['name'], pts


def height_features(pts, core_m=40):
    """Summarise returns within core_m of a candidate (detection points can sit 20-30 m from the mast).
    Ground is the 5th percentile of ground-classified returns (class 2), or of all returns if none are;
    heights are metres above it. Noise classes (7, 18) are kept: survey processing often files a thin
    tower's returns as noise. top_width_m is the spread of returns in the top quarter of the tallest object:
    a mast or lattice tower is a few metres wide there, a tree crown, grain elevator or water tank is wider."""
    if pts is None or not len(pts):
        return {'lidar_points': 0}
    ground_z = pts[pts[:, 3] == 2, 2]
    g = np.percentile(ground_z if len(ground_z) > 20 else pts[:, 2], 5)
    h = pts[:, 2] - g
    core = np.hypot(pts[:, 0], pts[:, 1]) <= core_m
    if not core.any():
        return {'lidar_points': int(len(pts)), 'core_points': 0}
    hc = h[core]
    top = float(hc.max())
    out = {'lidar_points': int(len(pts)), 'core_points': int(core.sum()), 'max_height_m': round(top, 1),
           'points_above_15m': int((core & (h >= 15)).sum()), 'points_above_30m': int((core & (h >= 30)).sum()),
           'noise_points_above_15m': int((core & (h >= 15) & np.isin(pts[:, 3], (7, 18))).sum())}
    if top >= 10:
        i = np.argmax(np.where(core, h, -np.inf))
        sel = core & (h >= 0.75 * top) & (np.hypot(pts[:, 0] - pts[i, 0], pts[:, 1] - pts[i, 1]) <= 15)
        xy = pts[sel][:, :2]
        out['top_width_m'] = round(float(np.hypot(*(xy.max(0) - xy.min(0)))), 1)
        out['top_offset_m'] = round(float(np.hypot(pts[i, 0], pts[i, 1])), 1)
    return out


def tower_evidence(lon, lat, radius_m=60):
    """height_features for every 3DEP survey with returns at the point, plus the strongest of them.
    Surveys differ: some drop or withhold the sparse high returns from thin masts, and a tower built after a
    survey is absent from it, so one survey without a tall return is weak evidence; any survey with one is strong."""
    surveys = []
    for proj in projects_at(lon, lat):
        name, pts = points(lon, lat, radius_m, proj)
        if len(pts):
            surveys.append({'project': name, **height_features(pts)})
    narrow = [s for s in surveys if is_tower_like(s)]
    best = max(narrow or surveys, key=lambda s: s.get('max_height_m') or 0, default={'project': None, 'lidar_points': 0})
    return {**best, 'tower_like': bool(narrow), 'surveys': surveys}


def is_tower_like(f, min_height_m=35, max_width_m=15):
    """A tall, narrow structure: on labelled Story County IA sites this held for 29 of 41 known towers (most of the
    rest were built after the survey) and 2 of 166 user-confirmed non-towers."""
    w = f.get('top_width_m')
    return ((f.get('max_height_m') or 0) >= min_height_m and (w is None or w <= max_width_m)
            and f.get('points_above_30m', 0) >= 2)
