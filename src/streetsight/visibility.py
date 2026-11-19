"""2.5D line of sight from Street View cameras to a target, and a ranking score.

All heights are measured relative to the ground at the target's base. The camera sits at
ch = camera_h + ground(camera) - ground(target). Buildings are prisms, canopy is an opaque height
field, and terrain is a bare-earth profile, each placed on the local ground. For an obstruction
whose top is o at distance d along a sight line of length D, the target is hidden below
ch + (o - ch) * D / d. The worst obstruction sets visible_above_m. Without a ground model
(ground=None) the ground is flat.
"""
import math

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point

from .geometry import bearing_distance


def ground_offsets(ground, lons, lats, z_target):
    """Ground elevation minus target-base elevation at each point; 0 where unknown or ground is None."""
    if ground is None or not np.isfinite(z_target):
        return np.zeros(np.size(lons))
    g = ground(lons, lats) - z_target
    return np.where(np.isfinite(g), g, 0.0)


def _need(ch, o, D, d):
    return ch + (o - ch) * D / np.maximum(d, 0.5)


def building_los(panos, tlat, tlon, buildings, camera_h=2.5, ground=None, z_target=np.nan):
    out = {}
    if buildings is None or not len(buildings):
        return {p['pano_id']: {'building_visible_above_m': 0.0, 'blocking_building': '', 'buildings_crossed': 0}
                for p in panos}
    tower = gpd.GeoSeries([Point(tlon, tlat)], crs='EPSG:4326')
    utm = tower.estimate_utm_crs()
    tp = tower.to_crs(utm).iloc[0]
    bl = buildings.to_crs(utm)
    heights = bl['height_used_m'].astype(float).to_numpy()
    cams = gpd.GeoSeries([Point(p['lon'], p['lat']) for p in panos], crs='EPSG:4326').to_crs(utm)
    for p, cp in zip(panos, cams):
        line = LineString([cp, tp])
        ch = camera_h + p.get('ground_offset_m', 0.0)
        D, worst, blocker, crossed = line.length, 0.0, '', 0
        for i in bl.sindex.query(line, predicate='intersects'):
            poly = bl.geometry.iloc[i]
            if poly.contains(cp):
                continue
            hit = line.intersection(poly)
            if hit.is_empty:
                continue
            d = min(line.project(Point(c)) for g in getattr(hit, 'geoms', [hit]) for c in g.coords)
            crossed += 1
            f = d / D
            go = ground_offsets(ground, [p['lon'] + (tlon - p['lon']) * f], [p['lat'] + (tlat - p['lat']) * f],
                                z_target)[0]
            need = float(_need(ch, heights[i] + go, D, d))
            if need > worst:
                worst, blocker = need, str(bl['id'].iloc[i])
        out[p['pano_id']] = {'building_visible_above_m': round(max(worst, 0.0), 1),
                             'blocking_building': blocker, 'buildings_crossed': crossed}
    return out


def canopy_los(panos, tlat, tlon, canopy_tif, buildings=None, camera_h=2.5, step_m=1.0,
               target_buffer_m=15.0, camera_buffer_m=3.0, ground=None, z_target=np.nan):
    """Treat canopy as opaque (leaf-on worst case). Pixels near the target/camera or in buildings are ignored."""
    import rasterio
    from pyproj import Transformer
    from shapely import contains_xy
    from shapely.ops import unary_union
    with rasterio.open(canopy_tif) as ds:
        chm, inv, crs = ds.read(1).astype(float), ~ds.transform, ds.crs
    chm[chm >= 255] = 0
    to_r = Transformer.from_crs('EPSG:4326', crs, always_xy=True)
    footprint = unary_union(list(buildings.geometry)) if buildings is not None and len(buildings) else None
    out = {}
    for p in panos:
        D = bearing_distance(p['lat'], p['lon'], tlat, tlon)[1]
        n = max(2, int(D / step_m) + 1)
        t = np.linspace(0, 1, n)
        lats, lons, d = p['lat'] + (tlat - p['lat']) * t, p['lon'] + (tlon - p['lon']) * t, t * D
        cols, rows = inv * to_r.transform(lons, lats)
        rows, cols = np.floor(rows).astype(int), np.floor(cols).astype(int)
        ok = (rows >= 0) & (rows < chm.shape[0]) & (cols >= 0) & (cols < chm.shape[1])
        h = np.zeros(n)
        h[ok] = chm[rows[ok], cols[ok]]
        ch = camera_h + p.get('ground_offset_m', 0.0)
        o = h + ground_offsets(ground, lons, lats, z_target)
        mask = (d > camera_buffer_m) & (D - d > target_buffer_m) & (h > 0) & (o > ch)
        if footprint is not None:
            mask &= ~contains_xy(footprint, lons, lats)
        need = np.where(mask, _need(ch, o, D, d), 0)
        i = int(need.argmax())
        out[p['pano_id']] = {'canopy_visible_above_m': round(float(need[i]), 1),
                             'canopy_worst_at_m': round(float(d[i]), 1) if need[i] > 0 else None,
                             'canopy_worst_height_m': float(h[i]) if need[i] > 0 else None,
                             'canopy_m_on_line': round(float(mask.sum() * D / (n - 1)), 1)}
    return out


def terrain_los(panos, tlat, tlon, ground, z_target, camera_h=2.5, step_m=5.0, end_buffer_m=20.0):
    """Bare-earth terrain between camera and target. Ends are skipped: 10 m DEM cells are noisy there."""
    out = {}
    for p in panos:
        D = bearing_distance(p['lat'], p['lon'], tlat, tlon)[1]
        n = max(2, int(D / step_m) + 1)
        t = np.linspace(0, 1, n)
        lats, lons, d = p['lat'] + (tlat - p['lat']) * t, p['lon'] + (tlon - p['lon']) * t, t * D
        ch = camera_h + p.get('ground_offset_m', 0.0)
        o = ground_offsets(ground, lons, lats, z_target)
        mask = (d > end_buffer_m) & (D - d > end_buffer_m) & (o > ch)
        need = np.where(mask, _need(ch, o, D, d), 0)
        i = int(need.argmax())
        out[p['pano_id']] = {'terrain_visible_above_m': round(float(need[i]), 1),
                             'terrain_worst_at_m': round(float(d[i]), 1) if need[i] > 0 else None}
    return out


def combine(vb, vc, target_h, vt=0.0):
    """Visibility status from building, canopy and terrain visible-above heights (m)."""
    vals = {'building': vb or 0.0, 'canopy': vc or 0.0, 'terrain': vt or 0.0}
    cause = max(vals, key=vals.get)
    va = max(vals[cause], 0.0)
    cause = cause if va > 0 else 'none'
    if va <= 0:
        status = 'clear'
    elif va >= target_h:
        status = f'hidden_by_{cause}'
    else:
        status = f'partly_hidden_by_{cause}'
    return {'visibility': status, 'visible_above_m': round(va, 1),
            'visible_fraction': round(max(0.0, 1 - va / target_h), 3), 'limiting_factor': cause}


def score(distance_m, building_above, canopy_above, target_h, copyright='', canopy_weight=0.7,
          min_distance_m=10.0, terrain_above=0.0, predates_target=False):
    """Heuristic 0-1 score for "an image aimed from here will show the target". Not a calibrated probability.

    - visible fraction of the target, with canopy obstruction discounted (trees are partly see-through,
      and the canopy data may be years old);
    - distance decay scaled to target height (taller targets stay recognisable further away);
    - user-contributed panoramas (non-Google copyright) are down-weighted: they are often mis-oriented;
    - panoramas taken before the target was built are heavily down-weighted.
    """
    blocked = max(building_above or 0.0, terrain_above or 0.0, canopy_weight * (canopy_above or 0.0))
    vis = max(0.0, 1 - blocked / target_h)
    scale = max(150.0, 4 * target_h)
    dist = math.exp(-distance_m / scale) * (0.4 if distance_m < min_distance_m else 1.0)
    src = 1.0 if 'google' in (copyright or '').lower() else 0.6
    age = 0.1 if predates_target else 1.0
    return round(vis * dist * src * age, 3)

