"""find_views: run find_view over many sites, one folder per site, with resume and a summary table."""
import json
import re
import traceback
from pathlib import Path

import pandas as pd

from .cache import default_runs_dir
from .pipeline import find_view
from .streetview import StreetViewError

ROW_OVERRIDES = ('target', 'target_height_m', 'built_date', 'radius_m', 'n_candidates', 'position_uncertainty_m')


def _sites_table(sites):
    if isinstance(sites, (str, Path)):
        df = pd.read_csv(sites)
    else:
        df = pd.DataFrame(sites)
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.rename(columns={'latitude': 'lat', 'longitude': 'lon', 'lng': 'lon', 'id': 'site_id'})
    missing = {'lat', 'lon'} - set(df.columns)
    if missing:
        raise ValueError(f'Sites table needs columns lat and lon (missing {sorted(missing)})')
    if 'site_id' not in df.columns:
        df['site_id'] = [f'site_{i:04d}' for i in range(1, len(df) + 1)]
    df['site_id'] = [re.sub(r'[^\w.-]+', '_', str(s)) for s in df['site_id']]
    if df['site_id'].duplicated().any():
        raise ValueError(f"Duplicate site_id values: {sorted(df.loc[df['site_id'].duplicated(), 'site_id'])[:5]}")
    return df


def _summary_row(site_id, summary, out_root):
    best = summary.get('best') or {}
    params = summary.get('parameters', {})
    t = summary.get('target', {})
    return {'site_id': site_id, 'lat': t.get('lat'), 'lon': t.get('lon'), 'found': summary.get('found'),
            'target_height_m': t.get('height_m'), 'height_source': t.get('height_source'),
            'n_panoramas': summary.get('n_panoramas'), 'n_images': summary.get('n_image_requests'),
            'n_candidates_requested': params.get('n_candidates'), 'verifier': params.get('verifier'),
            'best_pano_id': best.get('pano_id'), 'best_rank': best.get('rank'),
            'best_distance_m': best.get('distance_m'), 'best_heading': best.get('heading_to_target'),
            'best_confidence': best.get('verified_confidence'), 'best_lat': best.get('lat'),
            'best_lon': best.get('lon'), 'best_pano_date': best.get('pano_date'),
            'best_image': f"{site_id}/{best['image']}" if best.get('image') else None, 'error': None}


def _already_done(result_json, n_candidates, verifier, radius_m=300):
    """Skip a site if it was found before, or already ran with at least as many images, the same verifier and
    at least this search radius (a wider radius reruns sites that got no image)."""
    try:
        s = json.loads(Path(result_json).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    p = s.get('parameters', {})
    wider = radius_m > p.get('radius_m', 300) and not s.get('n_image_requests')
    if s.get('found') or (p.get('n_candidates', -1) >= n_candidates and (p.get('verifier') is None) == (verifier is None)
                          and not wider):
        return s
    return None


def find_views(sites, *, out_root=None, resume=True, max_images_total=None, summary_name='summary', log=print,
               **kwargs):
    """Run find_view for every row of `sites` and write <out_root>/summary.csv and summary.gpkg.

    sites : CSV path, DataFrame or list of dicts with lat, lon and optionally site_id (also accepts
        latitude/longitude/id). Per-row columns override keyword arguments: target, target_height_m,
        built_date, radius_m, n_candidates, position_uncertainty_m.
    out_root : parent folder; each site goes to <out_root>/<site_id>. Default data/streetsight_runs.
    resume : skip sites whose result.json shows they were found, or already ran with at least as many
        images and the same kind of verifier.
    max_images_total : cap on billed images across the whole batch. When it is used up, remaining
        sites are still ranked (free) with n_candidates=0.
    summary_name : base name of the summary files (useful when several batches share out_root).
    **kwargs : passed to find_view (verifier, n_candidates, radius_m, ...).

    A Street View key or quota error stops the batch; any other per-site error is recorded and the
    batch continues. Returns the summary DataFrame.
    """
    df = _sites_table(sites)
    out_root = Path(out_root) if out_root else default_runs_dir()
    out_root.mkdir(parents=True, exist_ok=True)
    rows, used = [], 0
    for i, site in enumerate(df.to_dict('records'), 1):
        sid = site['site_id']
        kw = {**kwargs, **{k: site[k] for k in ROW_OVERRIDES if k in site and pd.notna(site[k])}}
        n = int(kw.get('n_candidates', 5))
        if max_images_total is not None:
            n = max(0, min(n, max_images_total - used))
        kw['n_candidates'] = n
        out_dir = out_root / sid
        prior = (_already_done(out_dir / 'result.json', n, kw.get('verifier', 'claude'), float(kw.get('radius_m', 300)))
                 if resume else None)
        log(f'[{i}/{len(df)}] {sid} ({site["lat"]:.6f}, {site["lon"]:.6f})' + (' - already done' if prior else ''))
        if prior:
            rows.append(_summary_row(sid, prior, out_root))
            continue
        try:
            find_view(float(site['lat']), float(site['lon']), site_id=sid, out_dir=out_dir, log=log, **kw)
            summary = json.loads((out_dir / 'result.json').read_text(encoding='utf-8'))
            used += summary.get('n_image_requests', 0)
            rows.append(_summary_row(sid, summary, out_root))
        except StreetViewError as exc:
            rows.append({'site_id': sid, 'lat': site['lat'], 'lon': site['lon'], 'error': str(exc)})
            log(f'Stopping batch: {exc}')
            break
        except Exception as exc:  # one bad site should not end a long batch
            rows.append({'site_id': sid, 'lat': site['lat'], 'lon': site['lon'], 'error': repr(exc)})
            (out_dir / 'error.txt').parent.mkdir(parents=True, exist_ok=True)
            (out_dir / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
            log(f'{sid}: error {exc!r}; continuing')
    summary = pd.DataFrame(rows)
    _write_summary(summary, out_root, summary_name)
    found = int(summary['found'].fillna(False).astype(bool).sum()) if 'found' in summary else 0
    log(f'Batch done: {found}/{len(summary)} found, {used} images downloaded this run. Summary: {out_root / (summary_name + ".csv")}')
    return summary


def _write_summary(summary, out_root, name='summary'):
    import geopandas as gpd
    from shapely.geometry import LineString, Point
    summary.to_csv(out_root / f'{name}.csv', index=False)
    gpkg = out_root / f'{name}.gpkg'
    gpkg.unlink(missing_ok=True)
    if summary.empty:
        return
    gpd.GeoDataFrame(summary, geometry=[Point(r.lon, r.lat) for r in summary.itertuples()], crs='EPSG:4326') \
        .to_file(gpkg, layer='sites', driver='GPKG')
    best = summary[summary.get('best_lat', pd.Series(dtype=float)).notna()] if 'best_lat' in summary else summary.iloc[:0]
    if len(best):
        gpd.GeoDataFrame(best, geometry=[Point(r.best_lon, r.best_lat) for r in best.itertuples()], crs='EPSG:4326') \
            .to_file(gpkg, layer='best_cameras', driver='GPKG')
        gpd.GeoDataFrame(best, geometry=[LineString([(r.best_lon, r.best_lat), (r.lon, r.lat)]) for r in best.itertuples()],
                         crs='EPSG:4326').to_file(gpkg, layer='best_sight_lines', driver='GPKG')
