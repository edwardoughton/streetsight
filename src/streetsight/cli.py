"""Command line.

    streetsight LAT LON [HEIGHT] [options]   one site; HEIGHT is the target height in metres (default 30)
    streetsight --sites sites.csv [options]  many sites (columns lat, lon, optional site_id, target_height_m)
"""
import argparse

from .batch import find_views
from .pipeline import find_view


def build_parser(prog='streetsight'):
    """The command-line parser, for wrappers that add their own defaults or options."""
    p = argparse.ArgumentParser(prog=prog, description=find_view.__doc__.split('\n')[0])
    p.add_argument('lat', type=float, nargs='?')
    p.add_argument('lon', type=float, nargs='?')
    p.add_argument('height', type=float, nargs='?', help='target height above ground in metres (default 30)')
    p.add_argument('--target-height', type=float, dest='target_height_m', help='same as HEIGHT')
    p.add_argument('--sites', help='CSV of sites for a batch run instead of LAT LON')
    p.add_argument('--site-id', dest='site_id', help='run folder name for a single site')
    p.add_argument('--images', type=int, default=3, dest='n_images',
                   help='images to download per site (default 3); 0 = rank camera positions only')
    p.add_argument('--min-visible', type=float, default=0.25, dest='min_visible_fraction',
                   help='download only views with at least this fraction of the target predicted visible')
    p.add_argument('--providers', default='mapillary,google',
                   help='imagery sources tried in order (default mapillary,google)')
    p.add_argument('--max-google-images', type=int,
                   help='cap on billed Google images: per site, or across the whole batch with --sites')
    p.add_argument('--built-date', dest='built_date', help='target construction date, e.g. 2015 or 2015-06')
    p.add_argument('--radius', type=float, default=300, dest='radius_m')
    p.add_argument('--position-uncertainty', type=float, default=10.0, dest='position_uncertainty_m')
    p.add_argument('--out', dest='out_dir', help='run folder (single site) or parent folder (batch)')
    p.add_argument('--min-separation', type=float, default=15.0, dest='min_separation_m',
                   help='Mapillary: minimum distance between images downloaded for one site')
    p.add_argument('--road-spacing', type=float, default=30, dest='road_spacing_m')
    p.add_argument('--max-metadata-queries', type=int, default=60)
    p.add_argument('--search', choices=['ranked', 'exhaustive'], default='ranked',
                   help='Google: line of sight on road points first, query only the best (default)')
    p.add_argument('--target-panoramas', type=int, help='ranked search stops after this many panoramas')
    p.add_argument('--cache-dir')
    p.add_argument('--no-resume', action='store_false', dest='resume')
    p.add_argument('--no-buildings', action='store_false', dest='use_buildings')
    p.add_argument('--no-canopy', action='store_false', dest='use_canopy')
    p.add_argument('--no-terrain', action='store_false', dest='use_terrain')
    p.add_argument('--terrain-source', choices=['auto', '3dep', 'gedtm30'], default='auto',
                   help='auto: USGS 3DEP in the US, else global GEDTM30')
    p.add_argument('--no-ghs-heights', action='store_false', dest='use_ghs_heights',
                   help='do not fill missing building heights from GHS-BUILT-H')
    return p


def main(argv=None, parser=None):
    p = parser or build_parser()
    a = vars(p.parse_args(argv))
    a['providers'] = [s.strip() for s in a['providers'].split(',') if s.strip()]
    height = a.pop('height')
    if height is not None:
        if a['target_height_m'] is not None and a['target_height_m'] != height:
            p.error('give the height once: HEIGHT or --target-height')
        a['target_height_m'] = height
    sites, lat, lon = a.pop('sites'), a.pop('lat'), a.pop('lon')
    resume = a.pop('resume')
    if sites:
        if lat is not None:
            p.error('give either LAT LON or --sites, not both')
        a.pop('site_id')
        out_root, cap = a.pop('out_dir'), a.pop('max_google_images')
        if a['target_height_m'] is None:
            a.pop('target_height_m')  # per-site heights come from the CSV
        summary = find_views(sites, out_root=out_root, resume=resume, max_google_images_total=cap, **a)
        return 0 if 'error' not in summary or summary['error'].isna().all() else 1
    if lat is None or lon is None:
        p.error('give LAT LON [HEIGHT], or --sites FILE.csv')
    result = find_view(lat, lon, **a)
    return 0 if result.images or a['n_images'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
