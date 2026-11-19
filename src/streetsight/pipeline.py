"""find_view: from a coordinate and height to the street-level images most likely to show the target.

Imagery providers are tried in order (default: free Mapillary, then paid Google Street View). Each provider's
camera positions are ranked by line of sight to the target, past buildings, trees and terrain, and the best
are downloaded, aimed at the target. A later provider is queried only for images an earlier one could not
supply.
"""
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import canopy, ghsl, overture, terrain
from .cache import default_cache_dir, default_runs_dir
from .candidates import discover, discover_ranked, road_sample_points
from .export import write_outputs
from .geometry import bbox_around, bearing_distance, framing
from .mapillary import MapillaryClient, MapillaryError, in_vertical_view
from .streetview import StreetViewClient, StreetViewError
from .visibility import building_los, canopy_los, combine, ground_offsets, score, terrain_los

DEFAULT_TARGET_HEIGHT_M = 30.0
PROVIDERS = ('mapillary', 'google')


def _year_month(text):
    """'2024-09', '2024', '02/21/2000' or '2000-02-21' -> (year, month), or None."""
    text = str(text or '').strip()
    m = re.fullmatch(r'(\d{1,2})/\d{1,2}/(\d{4})', text)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.match(r'(\d{4})(?:-(\d{1,2}))?', text)
    return (int(m.group(1)), int(m.group(2) or 1)) if m else None


def predates(pano_date, built_date):
    """True if the panorama month is before the target's construction month (unknown dates -> False)."""
    a, b = _year_month(pano_date), _year_month(built_date)
    return bool(a and b and a < b)


def assess(items, lat, lon, H, buildings, canopy_tif, ground, z_t, camera_h, built_date):
    """Line of sight and score for camera positions (dicts with pano_id, lat, lon, distance_m, copyright,
    pano_date), updated in place. Used for road points before any Google request, then for panoramas."""
    if not items:
        return items
    offs = ground_offsets(ground, [p['lon'] for p in items], [p['lat'] for p in items], z_t)
    for p, o in zip(items, offs):
        p['ground_offset_m'] = round(float(o), 1)  # camera ground minus target ground
        p['predates_target'] = predates(p.get('pano_date'), built_date)
    b = building_los(items, lat, lon, buildings, camera_h, ground, z_t)
    c = canopy_los(items, lat, lon, canopy_tif, buildings, camera_h, ground=ground, z_target=z_t) if canopy_tif else {}
    tr = terrain_los(items, lat, lon, ground, z_t, camera_h) if ground else {}
    for p in items:
        p.update(b[p['pano_id']], **c.get(p['pano_id'], {}), **tr.get(p['pano_id'], {}))
        p.update(combine(p['building_visible_above_m'], p.get('canopy_visible_above_m'), H,
                         p.get('terrain_visible_above_m')))
        p['score'] = score(p['distance_m'], p['building_visible_above_m'], p.get('canopy_visible_above_m'),
                           H, p.get('copyright'), terrain_above=p.get('terrain_visible_above_m'),
                           predates_target=p['predates_target'])
    return items


def pick_spread(items, n, min_separation_m):
    """The n best-scoring items (already sorted best-first), skipping any within min_separation_m of one
    already picked: consecutive frames of one Mapillary sequence share the same obstructions."""
    picked = []
    for p in items:
        if len(picked) >= n:
            break
        if min_separation_m and any(bearing_distance(p['lat'], p['lon'], q['lat'], q['lon'])[1] < min_separation_m
                                    for q in picked):
            continue
        picked.append(p)
    return picked


@dataclass
class ViewResult:
    """images: downloaded views, best first (Mapillary, then Google). candidates: every camera position
    ranked, from every provider queried. target: the target record (location, height, data sources)."""
    images: list = field(repr=False)
    out_dir: Path
    target: dict
    candidates: list = field(repr=False)

    @property
    def best(self):
        """The top-ranked downloaded image (Mapillary before Google), or None."""
        return self.images[0] if self.images else None

    def __str__(self):
        head = f'streetsight result in {self.out_dir}\n'
        if not self.images:
            return head + f'No image downloaded ({len(self.candidates)} camera positions ranked)'
        by = ', '.join(f"{sum(p['source'] == s for p in self.images)} {s}" for s in PROVIDERS
                       if any(p['source'] == s for p in self.images))
        b = self.best
        return head + (f"{len(self.images)} images ({by}); best: {b['source']} {b['pano_id']} at {b['distance_m']} m, "
                       f"{b['visible_fraction']:.0%} predicted visible, {b['image']}")


def load_layers(lat, lon, radius_m, out_dir, cache_dir, use_buildings=True, use_canopy=True, use_terrain=True,
                log=print, terrain_source='auto', use_ghs_heights=True):
    """Fetch buildings, roads, canopy and terrain for the search area (in parallel).

    terrain_source : "auto" (USGS 3DEP in the US, else global GEDTM30), "3dep" or "gedtm30".
    use_ghs_heights : buildings with no Overture height or floor count get the GHS-BUILT-H 100 m average.

    Returns a dict: buildings, roads, height_tif (Meta canopy height), ground sampler, z_t (target ground
    elevation), and info about the sources used.
    """
    out_dir = Path(out_dir)
    bbox = bbox_around(lat, lon, radius_m + 50)
    with ThreadPoolExecutor(5) as ex:
        f_b = ex.submit(overture.buildings, bbox, cache_dir, log=log) if use_buildings else None
        f_g = ex.submit(ghsl.read_window, bbox, log=log) if use_buildings and use_ghs_heights else None
        f_r = ex.submit(overture.roads, bbox, cache_dir, log=log)
        f_c = ex.submit(canopy.fetch, bbox, out_dir / 'canopy_height.tif', log=log) if use_canopy else None
        f_t = (ex.submit(terrain.fetch, bbox, out_dir / 'terrain.tif', source=terrain_source, log=log)
               if use_terrain else None)
        buildings = f_b.result() if f_b else None
        roads = f_r.result()
        canopy_tif = f_c.result() if f_c else None
        terrain_tif, terrain_name = f_t.result() if f_t else (None, None)
        ghs = f_g.result() if f_g else None
    if ghs is not None:
        buildings = ghsl.fill_heights(buildings, bbox, window=ghs, log=log)
    ground = terrain.Ground(terrain_tif) if terrain_tif else None
    z_t = float(ground([lon], [lat])[0]) if ground else math.nan
    heights = (buildings['height_source'].value_counts().to_dict()
               if buildings is not None and len(buildings) else {})
    return {'buildings': buildings, 'roads': roads, 'height_tif': canopy_tif, 'ground': ground, 'z_t': z_t,
            'info': {'canopy': 'meta_wri_1m' if canopy_tif else 'none',
                     'terrain': terrain_name if ground else 'flat',
                     'building_height_sources': heights}}


def _google_candidates(client, roads, los, lat, lon, radius_m, n_candidates, search, road_spacing_m,
                       target_panoramas, max_metadata_queries, max_sample_points, log):
    """Google panoramas near the target, found by free metadata queries at road points.
    Returns (panos, per-point records, sampling info)."""
    if search == 'ranked':
        points, spacing, mode = road_sample_points(roads, lat, lon, radius_m, road_spacing_m, max_sample_points)
        query_radius = max(spacing, 25)
        log(f'Sampling {len(points)} points along {mode} every ~{spacing} m; ranking them before any Google request')
        pts = [{'pano_id': f'pt{i}', 'lat': la, 'lon': lo, 'copyright': 'Google', 'pano_date': None,
                'distance_m': bearing_distance(la, lo, lat, lon)[1]} for i, (la, lo) in enumerate(points)]
        assess(pts, **los)
        want = target_panoramas or max(10, 2 * n_candidates)
        panos, records = discover_ranked(client, points, [q['score'] for q in pts], lat, lon, radius_m,
                                         query_radius, want=want, max_queries=max_metadata_queries, log=log)
        for r, q in zip(records, pts):
            r.update(predicted_visibility=q['visibility'], predicted_visible_above_m=q['visible_above_m'],
                     distance_m=q['distance_m'])
    elif search == 'exhaustive':
        points, spacing, mode = road_sample_points(roads, lat, lon, radius_m, road_spacing_m, max_metadata_queries)
        log(f'Sampling {len(points)} points along {mode} every ~{spacing} m; querying all of them')
        panos, records = discover(client, points, lat, lon, radius_m, query_radius_m=max(spacing, 25), log=log)
    else:
        raise ValueError(f'search must be "ranked" or "exhaustive", not {search!r}')
    for p in panos:
        p['source'] = 'google'
    return panos, records, {'effective_spacing_m': spacing, 'sampling_mode': mode}


def _make_client(name, google_api_key, mapillary_token):
    if name == 'google':
        return StreetViewClient(google_api_key)
    if name == 'mapillary':
        return MapillaryClient(mapillary_token)
    raise ValueError(f'Unknown provider {name!r}: use "mapillary" and/or "google"')


def find_view(lat, lon, target_height_m=None, *, n_images=3, min_visible_fraction=0.25, providers=PROVIDERS,
              max_google_images=None, radius_m=300, out_dir=None, site_id=None, google_api_key=None,
              mapillary_token=None, road_spacing_m=30, search='ranked', target_panoramas=None,
              max_metadata_queries=60, max_sample_points=1500, camera_height_m=2.5, position_uncertainty_m=10.0,
              min_separation_m=15.0, built_date=None, use_buildings=True, use_canopy=True, use_terrain=True,
              terrain_source='auto', use_ghs_heights=True, cache_dir=None, log=print):
    """Download the street-level images most likely to show the target at (lat, lon).

    Camera positions are ranked by line of sight to the target past buildings, trees and terrain. The best
    n_images with at least min_visible_fraction of the target predicted visible are downloaded, aimed at the
    target: from Mapillary first, then from Google Street View for any places Mapillary could not fill.

    Parameters
    ----------
    lat, lon : target location (WGS84).
    target_height_m : height of the target above ground (default 30 m).
    n_images : how many images to download. 0 ranks camera positions only (no downloads).
    min_visible_fraction : only download views where at least this fraction of the target's height is
        predicted to be visible (0-1).
    providers : imagery sources, tried in order: "mapillary" (free, needs MAPILLARY_ACCESS_TOKEN) and
        "google" (Street View Static API; metadata free, images billed; needs GOOGLE_MAPS_API_KEY). A
        provider without credentials is skipped with a message; it is an error if none can be used.
    max_google_images : cap on billed Google images for this call (default: no cap beyond n_images).
    radius_m : maximum camera-to-target distance searched.
    out_dir : folder for images and outputs. Default <runs>/<site_id>, or <runs>/<timestamp>_<lat>_<lon>
        without a site_id (see cache.py for <runs>).
    site_id : name for the run folder and outputs.
    google_api_key : default from the GOOGLE_MAPS_API_KEY environment variable.
    mapillary_token : default from the MAPILLARY_ACCESS_TOKEN environment variable.
    road_spacing_m : spacing of candidate Google camera points along roads.
    search : Google only. "ranked" (default) scores every road point for line of sight locally first, then
        queries Street View metadata at the best points only, stopping at target_panoramas panoramas
        (default max(10, 2 * images still needed)) or when queries stop finding new ones. "exhaustive" queries
        every road point.
    max_metadata_queries : cap on Google metadata queries per site (free, but they count against quota).
    max_sample_points : cap on local road points; spacing grows to respect it.
    camera_height_m : camera height above the road.
    position_uncertainty_m : how far the true target may be from (lat, lon); close views are widened to
        keep it in frame.
    min_separation_m : Mapillary only: images downloaded for one site are at least this far apart.
    built_date : construction date of the target ('YYYY', 'YYYY-MM' or 'MM/DD/YYYY'), if known. Images
        taken earlier are down-weighted.
    use_buildings, use_canopy, use_terrain : switch clutter and terrain sources off.
    terrain_source : "auto" (USGS 3DEP 10 m in the US, else global GEDTM30 30 m), "3dep" or "gedtm30".
    use_ghs_heights : fill missing Overture building heights from GHS-BUILT-H (100 m average, global).
    cache_dir : download cache for Overture (default per-user cache folder).

    Returns ViewResult; everything is also written to out_dir (see export.write_outputs).
    """
    if not 0 <= min_visible_fraction <= 1:
        raise ValueError('min_visible_fraction must be between 0 and 1')
    if isinstance(providers, str):
        providers = [providers]
    for name in providers:
        if name not in PROVIDERS:
            raise ValueError(f'Unknown provider {name!r}: use "mapillary" and/or "google"')
    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
    if out_dir is None:
        name = re.sub(r'[^\w.-]+', '_', str(site_id)) if site_id else f"{datetime.now():%Y%m%d_%H%M%S}_{lat:.5f}_{lon:.5f}"
        out_dir = default_runs_dir() / name
    out_dir = Path(out_dir)
    (out_dir / 'images').mkdir(parents=True, exist_ok=True)

    clients, skipped = {}, {}
    for name in providers:
        try:
            clients[name] = _make_client(name, google_api_key, mapillary_token)
        except (StreetViewError, MapillaryError) as exc:
            skipped[name] = str(exc)
            log(f'Skipping {name}: {exc}')
    if not clients:
        raise RuntimeError('No imagery provider can be used: ' + '; '.join(f'{k}: {v}' for k, v in skipped.items()))

    # 1. Target height.
    tgt = {'site_id': site_id, 'lat': lat, 'lon': lon, 'height_m': target_height_m,
           'height_source': 'user' if target_height_m else None, 'built_date': built_date}
    if tgt['height_m'] is None:
        tgt.update(height_m=DEFAULT_TARGET_HEIGHT_M, height_source='assumed_default')
    H = float(tgt['height_m'])
    log(f"Target height {H} m ({tgt['height_source']})")

    # 2. Clutter, roads and terrain, fetched in parallel.
    L = load_layers(lat, lon, radius_m, out_dir, cache_dir, use_buildings, use_canopy, use_terrain, log,
                    terrain_source=terrain_source, use_ghs_heights=use_ghs_heights)
    buildings, roads, canopy_tif, ground, z_t = L['buildings'], L['roads'], L['height_tif'], L['ground'], L['z_t']
    tgt['ground_elevation_m'] = None if math.isnan(z_t) else round(z_t, 1)
    tgt.update(L['info'])
    los = dict(lat=lat, lon=lon, H=H, buildings=buildings, canopy_tif=canopy_tif, ground=ground, z_t=z_t,
               camera_h=camera_height_m, built_date=tgt['built_date'])

    # 3-5. Per provider: candidates, line of sight and ranking, then download the best that pass.
    all_panos, images, sample_records, sampling, used = [], [], [], {}, []
    for name, client in clients.items():
        need = n_images - len(images)
        if name == 'google' and max_google_images is not None:
            need = min(need, max_google_images)
        if n_images > 0 and need <= 0:
            break  # enough images already; with n_images=0 every provider is ranked
        used.append(name)
        if name == 'google':
            panos, sample_records, sampling = _google_candidates(
                client, roads, los, lat, lon, radius_m, max(need, 0), search, road_spacing_m, target_panoramas,
                max_metadata_queries, max_sample_points, log)
            sep = 0.0
        else:
            try:
                panos = client.views(lat, lon, radius_m)
            except MapillaryError:
                raise
            except Exception as exc:  # a Mapillary outage should not stop the run: fall through to Google
                log(f'Mapillary search failed ({exc!r}); moving on')
                skipped[name] = repr(exc)
                continue
            log(f'Mapillary: {len(panos)} usable images within {radius_m} m '
                f'({sum(p["is_pano"] for p in panos)} panoramas)')
            sep = min_separation_m
        assess(panos, **los)
        if name == 'mapillary':
            n0 = len(panos)
            panos = [p for p in panos if in_vertical_view(p, camera_height_m)]
            if len(panos) < n0:
                log(f'Mapillary: dropped {n0 - len(panos)} photos whose frame is below the visible part of the target')
        panos.sort(key=lambda p: -p['score'])
        for i, p in enumerate(panos, 1):
            p['rank'] = i
        all_panos += panos
        eligible = [p for p in panos if p['visible_fraction'] >= min_visible_fraction]
        picks = pick_spread(eligible, max(need, 0), sep)
        log(f'{name}: {len(eligible)} of {len(panos)} views have at least {min_visible_fraction:.0%} of the target '
            f'predicted visible; downloading {len(picks)}')
        images += _download(client, name, picks, out_dir, H, camera_height_m, position_uncertainty_m, log)

    result = ViewResult(images=images, out_dir=out_dir, target=tgt, candidates=all_panos)
    params = {'providers': list(providers), 'providers_used': used, 'providers_skipped': skipped,
              'n_images': n_images, 'min_visible_fraction': min_visible_fraction,
              'max_google_images': max_google_images, 'radius_m': radius_m, 'search': search, 'max_metadata_queries': max_metadata_queries, 'road_spacing_m': road_spacing_m,
              **sampling, 'min_separation_m': min_separation_m, 'camera_height_m': camera_height_m,
              'position_uncertainty_m': position_uncertainty_m, 'use_buildings': use_buildings,
              'use_canopy': use_canopy, 'use_terrain': use_terrain, 'terrain_source': terrain_source,
              'use_ghs_heights': use_ghs_heights, **L['info']}
    request_log = [row for c in clients.values() for row in c.log]
    write_outputs(result, params, sample_records, buildings, roads, request_log)
    log(str(result))
    return result


def _download(client, source, picks, out_dir, H, camera_height_m, position_uncertainty_m, log):
    """Download picks best-first, aimed at the target (reusing images already on disk). Returns the picks,
    each with image, image_heading, pitch and fov added."""
    prefix = '' if source == 'google' else 'mly_'
    for p in picks:
        if source == 'mapillary' and not p.get('is_pano'):
            # A photo as taken: its own heading and field of view, the target off-centre by target_offset_deg.
            p['image_heading'], p['pitch'], p['fov'] = p['camera_heading'], 0.0, p['camera_fov']
        else:
            p['pitch'], p['fov'] = framing(p['distance_m'], H, p['visible_above_m'],
                                           camera_height_m + p.get('ground_offset_m', 0.0),
                                           position_uncertainty_m=position_uncertainty_m)
            p['image_heading'] = p['heading_to_target']
        name = f"{prefix}rank{p['rank']:02d}_{p['distance_m']:.0f}m_h{p['heading_to_target']:.0f}_{p['pano_id']}.jpg"
        path = out_dir / 'images' / name
        earlier = [f for f in sorted(path.parent.glob(f"{prefix}rank*_{p['pano_id']}.jpg"))
                   if f != path and f.stat().st_size]
        if not (path.is_file() and path.stat().st_size) and earlier:
            earlier[0].rename(path)  # same view downloaded before under another rank: not billed again
        if path.is_file() and path.stat().st_size:
            log(f'{source} rank {p["rank"]}: reusing {name} (already downloaded)')
        elif source == 'google':
            path.write_bytes(client.image(p['pano_id'], p['heading_to_target'], p['pitch'], p['fov']))
        else:
            path.write_bytes(client.image(p, p['heading_to_target'], p['pitch'], p['fov']))
        p['image'] = f'images/{name}'
        log(f"{source} rank {p['rank']}: score {p['score']}, {p['distance_m']} m, {p['visibility']}, "
            f"{p['visible_fraction']:.0%} visible -> {name}")
    return picks
