"""Find Street View panoramas near a target by sampling points along roads and querying free metadata."""
from concurrent.futures import ThreadPoolExecutor

import geopandas as gpd
from shapely.geometry import Point

from .geometry import bearing_distance, offset


def road_sample_points(roads, tlat, tlon, radius_m, spacing_m, max_points):
    """Points every spacing_m along roads inside radius_m, plus the road point nearest the target.

    If that would exceed max_points, spacing grows until it fits. Falls back to rings when no roads.
    """
    if roads is None or not len(roads):
        return ring_points(tlat, tlon, radius_m, spacing_m), spacing_m, 'rings'
    target = gpd.GeoSeries([Point(tlon, tlat)], crs='EPSG:4326')
    utm = target.estimate_utm_crs()
    tp = target.to_crs(utm).iloc[0]
    area = tp.buffer(radius_m)
    lines = [g for g in roads.to_crs(utm).geometry.intersection(area) if not g.is_empty]
    parts = [p for g in lines for p in getattr(g, 'geoms', [g]) if p.length > 0]
    total = sum(p.length for p in parts)
    spacing = max(spacing_m, total / max(max_points - 1, 1))
    pts = []
    for p in parts:
        n = max(1, int(p.length // spacing))
        pts += [p.interpolate(i * p.length / n) for i in range(n + 1)]
    nearest = min(parts, key=lambda p: p.distance(tp), default=None)
    if nearest is not None:
        pts.append(nearest.interpolate(nearest.project(tp)))
    kept = []
    for q in pts:
        if all(q.distance(k) >= spacing * 0.5 for k in kept):
            kept.append(q)
    ll = gpd.GeoSeries(kept, crs=utm).to_crs('EPSG:4326')
    return [(g.y, g.x) for g in ll], round(spacing, 1), 'roads'


def ring_points(tlat, tlon, radius_m, step_m):
    pts, d = [(tlat, tlon)], step_m
    while d < radius_m:
        pts += [offset(tlat, tlon, az, d) for az in range(0, 360, 45)]
        d *= 2
    return pts


def discover(client, points, tlat, tlon, radius_m, query_radius_m, workers=8, log=print):
    """Query metadata at each point; return (unique panos within radius_m of the target, per-point records)."""
    def one(i_pt):
        i, (lat, lon) = i_pt
        pano = client.metadata(lat, lon, query_radius_m, tag=f'pt{i}')
        return {'point_id': i, 'lat': lat, 'lon': lon, 'pano_id': pano['pano_id'] if pano else ''}, pano

    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(one, enumerate(points)))
    panos, records = {}, []
    for rec, pano in results:
        records.append(rec)
        if pano and pano['pano_id'] not in panos:
            heading, dist = bearing_distance(pano['lat'], pano['lon'], tlat, tlon)
            if dist <= radius_m:
                panos[pano['pano_id']] = {**pano, 'heading_to_target': heading, 'distance_m': dist,
                                          'found_by_point': rec['point_id']}
    log(f'Street View: {len(points)} metadata queries -> {len(panos)} unique panoramas within {radius_m} m')
    return list(panos.values()), records


def discover_ranked(client, points, scores, tlat, tlon, radius_m, query_radius_m, want=10, max_queries=60,
                    max_dry=16, skip_near_m=None, workers=8, log=print):
    """Query metadata at the best-scoring points first, stopping early.

    points : [(lat, lon)], scores : predicted line-of-sight score per point (computed locally, free).
    Points are queried in descending score order, in parallel batches of `workers`. A point is skipped if
    it lies within skip_near_m (default query_radius_m) of a panorama already found, because it would
    most likely return that panorama again. Stops when `want` unique panoramas within radius_m are found,
    after max_queries queries, or after max_dry consecutive queries that found nothing new (e.g. no coverage).
    Returns (panos, per-point records including predicted score and whether the point was queried).
    """
    skip_near_m = query_radius_m if skip_near_m is None else skip_near_m
    order = sorted(range(len(points)), key=lambda i: -scores[i])
    records = [{'point_id': i, 'lat': la, 'lon': lo, 'predicted_score': scores[i], 'query_order': None,
                'queried': False, 'pano_id': '', 'skipped_near_pano': ''} for i, (la, lo) in enumerate(points)]
    panos, n_q, dry, pos, why = {}, 0, 0, 0, 'all points used'

    def near_found(i):
        la, lo = points[i]
        for p in panos.values():
            if bearing_distance(la, lo, p['lat'], p['lon'])[1] <= skip_near_m:
                return p['pano_id']
        return ''

    def one(i):
        la, lo = points[i]
        return i, client.metadata(la, lo, query_radius_m, tag=f'pt{i}')

    with ThreadPoolExecutor(workers) as ex:
        while pos < len(order):
            if len(panos) >= want:
                why = f'found {want} panoramas'
                break
            if n_q >= max_queries:
                why = f'reached max_metadata_queries={max_queries}'
                break
            if dry >= max_dry:
                why = f'{max_dry} queries in a row found nothing new'
                break
            batch = []
            while pos < len(order) and len(batch) < min(workers, max_queries - n_q):
                i = order[pos]
                pos += 1
                hit = near_found(i)
                if hit:
                    records[i]['skipped_near_pano'] = hit
                else:
                    batch.append(i)
            for i, pano in ex.map(one, batch):
                n_q += 1
                rec = records[i]
                rec.update(queried=True, query_order=n_q, pano_id=pano['pano_id'] if pano else '')
                new = False
                if pano and pano['pano_id'] not in panos:
                    heading, dist = bearing_distance(pano['lat'], pano['lon'], tlat, tlon)
                    if dist <= radius_m:
                        panos[pano['pano_id']] = {**pano, 'heading_to_target': heading, 'distance_m': dist,
                                                  'found_by_point': i}
                        new = True
                dry = 0 if new else dry + 1
    log(f'Street View: {n_q} metadata queries (best-first of {len(points)} road points) -> '
        f'{len(panos)} unique panoramas within {radius_m} m; stopped: {why}')
    return list(panos.values()), records
