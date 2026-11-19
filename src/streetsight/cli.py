"""Command line.

    streetsight LAT LON [options]            one site
    streetsight --sites sites.csv [options]  many sites (columns lat, lon, optional site_id, ...)
"""
import argparse

from .batch import find_views
from .pipeline import find_view


def main(argv=None):
    p = argparse.ArgumentParser(prog='streetsight', description=find_view.__doc__.split('\n')[0])
    p.add_argument('lat', type=float, nargs='?')
    p.add_argument('lon', type=float, nargs='?')
    p.add_argument('--sites', help='CSV of sites for a batch run instead of LAT LON')
    p.add_argument('--site-id', dest='site_id', help='run folder name for a single site')
    p.add_argument('--target', default='cell tower')
    p.add_argument('--target-height', type=float, dest='target_height_m')
    p.add_argument('--built-date', dest='built_date', help="target construction date, e.g. 2015 or 2015-06")
    p.add_argument('--radius', type=float, default=300, dest='radius_m')
    p.add_argument('--candidates', type=int, default=5, dest='n_candidates',
                   help='max images to download per site (billed); 0 = rank only, free')
    p.add_argument('--max-images-total', type=int, help='batch only: cap on billed images across all sites')
    p.add_argument('--verifier', default='claude', help='claude, none, or a review CSV (image, visible, confidence, reason)')
    p.add_argument('--min-confidence', type=float, default=0.6)
    p.add_argument('--position-uncertainty', type=float, default=10.0, dest='position_uncertainty_m')
    p.add_argument('--out', dest='out_dir', help='run folder (single site) or parent folder (batch)')
    p.add_argument('--road-spacing', type=float, default=30, dest='road_spacing_m')
    p.add_argument('--max-metadata-queries', type=int, default=60)
    p.add_argument('--search', choices=['ranked', 'exhaustive'], default='ranked',
                   help='ranked: line of sight on road points first, query only the best (default)')
    p.add_argument('--target-panoramas', type=int, help='ranked search stops after this many panoramas')
    p.add_argument('--cache-dir')
    p.add_argument('--no-resume', action='store_false', dest='resume')
    p.add_argument('--no-buildings', action='store_false', dest='use_buildings')
    p.add_argument('--no-canopy', action='store_false', dest='use_canopy')
    p.add_argument('--no-terrain', action='store_false', dest='use_terrain')
    p.add_argument('--no-fcc', action='store_false', dest='use_fcc')
    a = vars(p.parse_args(argv))
    a['verifier'] = None if a['verifier'] == 'none' else a['verifier']
    sites, lat, lon = a.pop('sites'), a.pop('lat'), a.pop('lon')
    resume, cap = a.pop('resume'), a.pop('max_images_total')
    if sites:
        if lat is not None:
            p.error('give either LAT LON or --sites, not both')
        a.pop('site_id')
        out_root = a.pop('out_dir')
        summary = find_views(sites, out_root=out_root, resume=resume, max_images_total=cap, **a)
        return 0 if 'error' not in summary or summary['error'].isna().all() else 1
    if lat is None or lon is None:
        p.error('give LAT LON, or --sites FILE.csv')
    result = find_view(lat, lon, **a)
    return 0 if result.found or a['verifier'] is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
