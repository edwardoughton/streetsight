"""find_view: one call from a coordinate to a verified street-level image of the target.

Imagery providers are tried in order (default: free Mapillary, then paid Google Street View). Each provider's
camera positions are ranked by line of sight to the target, past buildings, trees and terrain, and
downloaded best-first until the verifier confirms the target. Later providers are not queried at all once
an earlier one has succeeded.
"""
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import canopy, fcc, overture, terrain
from .cache import default_cache_dir, default_runs_dir
from .candidates import discover, discover_ranked, road_sample_points
from .export import write_outputs
from .geometry import bbox_around, bearing_distance, framing
from .mapillary import MapillaryClient, MapillaryError
from .streetview import StreetViewClient, StreetViewError
from .verify import resolve
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
class FindViewResult:
    found: bool
    best: dict | None
    out_dir: Path
    target: dict
    candidates: list = field(repr=False)
    attempts: list = field(repr=False)

    def __str__(self):
        head = f'streetsight result in {self.out_dir}\n'
        if self.found:
            b = self.best
            return head + (f"FOUND on attempt {len(self.attempts)}: {b.get('source', 'google')} image {b['pano_id']} "
                           f"at {b['distance_m']} m, heading {b['heading_to_target']}, saved as {b['image']}")
        return head + f'NOT FOUND after {len(self.attempts)} attempts ({len(self.candidates)} camera positions ranked)'


def load_layers(lat, lon, radius_m, out_dir, cache_dir, use_buildings=True, use_canopy=True, use_terrain=True,
                log=print):
    """Fetch buildings, roads, canopy and terrain for the search area (in parallel).

    Returns a dict: buildings, roads, height_tif (Meta canopy height), ground sampler, z_t (target ground
    elevation), and info about the sources used.
    """
    out_dir = Path(out_dir)
    bbox = bbox_around(lat, lon, radius_m + 50)
    with ThreadPoolExecutor(4) as ex:
        f_b = ex.submit(overture.buildings, bbox, cache_dir, log=log) if use_buildings else None
        f_r = ex.submit(overture.roads, bbox, cache_dir, log=log)
        f_c = ex.submit(canopy.fetch, bbox, out_dir / 'canopy_height.tif', log=log) if use_canopy else None
        f_t = ex.submit(terrain.fetch, bbox, out_dir / 'terrain.tif', log=log) if use_terrain else None
        buildings = f_b.result() if f_b else None
        roads = f_r.result()
        canopy_tif = f_c.result() if f_c else None
        terrain_tif = f_t.result() if f_t else None
    ground = terrain.Ground(terrain_tif) if terrain_tif else None
    z_t = float(ground([lon], [lat])[0]) if ground else math.nan
    return {'buildings': buildings, 'roads': roads, 'height_tif': canopy_tif, 'ground': ground, 'z_t': z_t,
            'info': {'canopy': 'meta_wri_1m' if canopy_tif else 'none',
                     'terrain': 'usgs_3dep_13' if ground else 'flat'}}


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


def find_view(lat, lon, *, target_height_m=None, target='structure', target_notes='', providers=PROVIDERS,
              radius_m=300, n_candidates=5, verifier='auto', min_confidence=0.6, out_dir=None, site_id=None,
              google_api_key=None, mapillary_token=None, road_spacing_m=30, search='ranked', target_panoramas=None,
              max_metadata_queries=60, max_sample_points=1500, camera_height_m=2.5, position_uncertainty_m=10.0,
              min_separation_m=15.0, built_date=None, use_buildings=True, use_canopy=True, use_terrain=True,
              use_fcc=False, cache_dir=None, log=print):
    """Find a street-level image that shows the target at (lat, lon).

    For each provider in turn, camera positions are ranked by line of sight to the target, then images are
    downloaded best-first until the verifier confirms the target is visible.

    Parameters
    ----------
    lat, lon : target location (WGS84).
    target_height_m : height above ground. Default: the nearest constructed FCC Antenna Structure
        Registration within 100 m if use_fcc (US only), else 30 m.
    target : short description used in the verifier prompt, e.g. "water tower", "cell tower".
    target_notes : extra guidance for the verifier, e.g. what look-alikes to reject.
    providers : imagery sources, tried in order: "mapillary" (free, needs MAPILLARY_ACCESS_TOKEN) and
        "google" (Street View Static API; metadata free, images billed; needs GOOGLE_MAPS_API_KEY). A
        provider without credentials is skipped with a message; it is an error if none can be used.
    radius_m : maximum camera-to-target distance searched.
    n_candidates : maximum images downloaded per provider. 0 ranks only (no image downloads).
    verifier : "auto" (default: Claude if credentials are set, else None), "claude", None (download all
        n_candidates for human review, no early stop), a review CSV path, or a callable
        verifier(image_path, context) -> {"visible", "confidence", "reason"}.
    min_confidence : verifier confidence required to stop.
    out_dir : folder for images and outputs. Default <runs>/<site_id>, or <runs>/<timestamp>_<lat>_<lon>
        without a site_id (see cache.py for <runs>).
    site_id : name for the run folder and outputs.
    google_api_key : default from the GOOGLE_MAPS_API_KEY environment variable.
    mapillary_token : default from the MAPILLARY_ACCESS_TOKEN environment variable.
    road_spacing_m : spacing of candidate Google camera points along roads.
    search : Google only. "ranked" (default) scores every road point for line of sight locally first, then
        queries Street View metadata at the best points only, stopping at target_panoramas panoramas
        (default max(10, 2 * n_candidates)) or when queries stop finding new ones. "exhaustive" queries
        every road point.
    max_metadata_queries : cap on Google metadata queries per site (free, but they count against quota).
    max_sample_points : cap on local road points; spacing grows to respect it.
    camera_height_m : camera height above the road.
    position_uncertainty_m : how far the true target may be from (lat, lon); close views are widened to
        keep it in frame.
    min_separation_m : Mapillary only: images downloaded for one site are at least this far apart.
    built_date : construction date of the target ('YYYY', 'YYYY-MM' or 'MM/DD/YYYY'). Default: the FCC
        record's construction date, if use_fcc. Images taken earlier are down-weighted.
    use_buildings, use_canopy, use_terrain : switch clutter and terrain sources off.
    use_fcc : look up height and construction date in the FCC Antenna Structure Registration (US towers).
    cache_dir : download cache for Overture, canopy and FCC (default per-user cache folder).

    Returns FindViewResult; everything is also written to out_dir (see export.write_outputs).
    """
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
    check = resolve(verifier, log=log)

    # 1. Target height.
    tgt = {'site_id': site_id, 'lat': lat, 'lon': lon, 'target': target, 'height_m': target_height_m,
           'height_source': 'user' if target_height_m else None, 'built_date': built_date}
    if target_height_m is None and use_fcc:
        h, m = fcc.tower_height(fcc.load_asr(cache_dir, log=log), lat, lon)
        if h:
            tgt.update(height_m=h, height_source='fcc_asr_overall_agl', fcc_registration=m['registration'],
                       fcc_distance_m=m['distance_m'], fcc_constructed=m['constructed_date'])
            tgt['built_date'] = built_date or m['constructed_date'] or None
    if tgt['height_m'] is None:
        tgt.update(height_m=DEFAULT_TARGET_HEIGHT_M, height_source='assumed_default')
    H = float(tgt['height_m'])
    log(f"Target height {H} m ({tgt['height_source']})")

    # 2. Clutter, roads and terrain, fetched in parallel.
    L = load_layers(lat, lon, radius_m, out_dir, cache_dir, use_buildings, use_canopy, use_terrain, log)
    buildings, roads, canopy_tif, ground, z_t = L['buildings'], L['roads'], L['height_tif'], L['ground'], L['z_t']
    tgt['ground_elevation_m'] = None if math.isnan(z_t) else round(z_t, 1)
    tgt.update(L['info'])
    los = dict(lat=lat, lon=lon, H=H, buildings=buildings, canopy_tif=canopy_tif, ground=ground, z_t=z_t,
               camera_h=camera_height_m, built_date=tgt['built_date'])

    # 3-5. Per provider: candidates, line of sight and ranking, then download best-first until verified.
    all_panos, attempts, best, sample_records, sampling, used = [], [], None, [], {}, []
    for name, client in clients.items():
        if best is not None:
            break
        used.append(name)
        if name == 'google':
            panos, sample_records, sampling = _google_candidates(
                client, roads, los, lat, lon, radius_m, n_candidates, search, road_spacing_m, target_panoramas,
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
        panos.sort(key=lambda p: -p['score'])
        for i, p in enumerate(panos, 1):
            p['rank'] = i
        all_panos += panos
        best = _download_and_verify(client, name, pick_spread(panos, n_candidates, sep), out_dir, check, attempts,
                                    target, target_notes, H, camera_height_m, position_uncertainty_m,
                                    min_confidence, log)

    result = FindViewResult(found=best is not None, best=best, out_dir=out_dir, target=tgt,
                            candidates=all_panos, attempts=attempts)
    params = {'providers': list(providers), 'providers_used': used, 'providers_skipped': skipped,
              'radius_m': radius_m, 'n_candidates': n_candidates, 'min_confidence': min_confidence,
              'search': search, 'max_metadata_queries': max_metadata_queries, 'road_spacing_m': road_spacing_m,
              **sampling, 'min_separation_m': min_separation_m, 'camera_height_m': camera_height_m,
              'position_uncertainty_m': position_uncertainty_m, 'use_buildings': use_buildings,
              'use_canopy': use_canopy, 'use_terrain': use_terrain, **L['info'], 'use_fcc': use_fcc,
              'verifier': getattr(check, 'model', None) or (None if check is None else 'custom')}
    request_log = [row for c in clients.values() for row in c.log]
    write_outputs(result, params, sample_records, buildings, roads, request_log)
    log(str(result))
    return result


def _download_and_verify(client, source, picks, out_dir, check, attempts, target, target_notes, H, camera_height_m,
                         position_uncertainty_m, min_confidence, log):
    """Download picks best-first (reusing images already on disk), verify each, and return the first
    confirmed one, or None. Appends every attempt to `attempts`."""
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
        if path.is_file() and path.stat().st_size:
            log(f'{source} rank {p["rank"]}: reusing {name} (already downloaded)')
        elif source == 'google':
            path.write_bytes(client.image(p['pano_id'], p['heading_to_target'], p['pitch'], p['fov']))
        else:
            path.write_bytes(client.image(p, p['heading_to_target'], p['pitch'], p['fov']))
        p['image'] = f'images/{name}'
        verdict = {'visible': None, 'confidence': None, 'reason': 'not verified'}
        if check:
            ctx = {'target': target, 'target_notes': target_notes, 'distance_m': p['distance_m'],
                   'heading': p['image_heading'], 'pitch': p['pitch'], 'fov': p['fov'],
                   'visible_above_m': p['visible_above_m'], 'target_height_m': H,
                   'source': 'Google Street View' if source == 'google' else 'Mapillary street-level',
                   'target_offset_deg': p.get('target_offset_deg')}
            verdict = check(path, ctx)
        p.update({f'verified_{k}': v for k, v in verdict.items()})
        attempts.append(p)
        log(f"{source} rank {p['rank']}: score {p['score']} {p['distance_m']} m {p['visibility']} -> "
            f"visible={verdict.get('visible')} conf={verdict.get('confidence')} ({verdict.get('reason')})")
        if check and verdict.get('visible') and (verdict.get('confidence') or 0) >= min_confidence:
            return p
    return None
