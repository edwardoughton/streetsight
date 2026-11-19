"""Write a run folder: results.gpkg, candidates.csv, requests.csv, result.json, index.html, images/."""
import csv
import html
import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point

from .geometry import circle, wedge

CRS = 'EPSG:4326'


def _flat(v):
    return json.dumps(v, default=str) if isinstance(v, (list, dict, tuple)) else v


def _gdf(rows, geoms):
    return gpd.GeoDataFrame(pd.DataFrame([{k: _flat(v) for k, v in r.items()} for r in rows]), geometry=geoms, crs=CRS)


def write_outputs(result, params, sample_records, buildings, roads, request_log):
    out, t, panos = result.out_dir, result.target, result.candidates
    gpkg = out / 'results.gpkg'
    gpkg.unlink(missing_ok=True)
    layers = {
        'target': _gdf([{**t, **params}], [Point(t['lon'], t['lat'])]),
        'search_area': _gdf([{'radius_m': params['radius_m']}], [circle(t['lat'], t['lon'], params['radius_m'])]),
    }
    if sample_records:
        layers['sample_points'] = _gdf(sample_records, [Point(r['lon'], r['lat']) for r in sample_records])
    if panos:
        layers['panoramas'] = _gdf(panos, [Point(p['lon'], p['lat']) for p in panos])
        layers['sight_lines'] = _gdf(
            [{k: p.get(k) for k in ('source', 'rank', 'pano_id', 'score', 'visibility', 'visible_above_m', 'distance_m',
                                    'heading_to_target', 'verified_visible')} for p in panos],
            [LineString([(p['lon'], p['lat']), (t['lon'], t['lat'])]) for p in panos])
    if result.attempts:
        layers['view_wedges'] = _gdf(
            [{k: p.get(k) for k in ('source', 'rank', 'pano_id', 'image_heading', 'pitch', 'fov', 'image',
                                    'verified_visible', 'verified_confidence')} for p in result.attempts],
            [wedge(p['lat'], p['lon'], p.get('image_heading', p['heading_to_target']), p['fov'], p['distance_m'] * 1.4 + 20)
             for p in result.attempts])
    if roads is not None and len(roads):
        layers['roads'] = roads
    if buildings is not None and len(buildings):
        layers['buildings'] = buildings
    for name, gdf in layers.items():
        gdf.to_file(gpkg, layer=name, driver='GPKG')

    if panos:
        pd.DataFrame(panos).to_csv(out / 'candidates.csv', index=False)
    with open(out / 'requests.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['kind', 'tag', 'http_status', 'api_status', 'pano_id', 'detail', 'url'],
                           extrasaction='ignore')
        w.writeheader()
        w.writerows(request_log)
    summary = {'found': result.found, 'target': t, 'parameters': params, 'best': result.best,
               'attempts': result.attempts, 'n_panoramas': len(panos),
               'n_metadata_requests': sum(r['kind'] == 'metadata' for r in request_log),
               'n_image_requests': sum(r['kind'] == 'static' for r in request_log),
               'n_mapillary_images': sum(r['kind'] == 'mapillary_image' for r in request_log)}
    (out / 'result.json').write_text(json.dumps(summary, indent=2, default=str), encoding='utf-8')

    cards = ''.join(
        f"<figure class='{'ok' if p is result.best else ''}'><img src='{html.escape(p['image'])}'><figcaption>"
        f"<b>{html.escape(p.get('source', 'google').title())} rank {p['rank']}</b> &middot; score {p['score']} &middot; {p['distance_m']} m &middot; "
        f"{html.escape(p['visibility'])} (visible above {p['visible_above_m']} m)<br>"
        f"Verifier: visible={p.get('verified_visible')} conf={p.get('verified_confidence')} &mdash; "
        f"{html.escape(str(p.get('verified_reason', '')))}<br>"
        f"heading {p.get('image_heading', p['heading_to_target'])}&deg; pitch {p['pitch']}&deg; fov {p['fov']}&deg; &middot; "
        f"{html.escape(str(p.get('pano_date') or ''))} &middot; {html.escape(str(p.get('copyright') or ''))}<br><code>{html.escape(p['pano_id'])}</code>"
        f"</figcaption></figure>" for p in result.attempts)
    status = 'FOUND' if result.found else 'NOT FOUND'
    (out / 'index.html').write_text(
        "<!DOCTYPE html><meta charset='utf-8'><title>streetsight</title><style>body{font-family:sans-serif;margin:1.5rem}"
        "main{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:1rem}img{width:100%}"
        "figure{margin:0;padding:.5rem;border:2px solid #ddd}figure.ok{border-color:#1a7f37}"
        "figcaption{font-size:.85rem}</style>"
        f"<h1>{html.escape(t['target'])} at {t['lat']}, {t['lon']}: {status}</h1>"
        f"<p>Height {t['height_m']} m ({html.escape(str(t['height_source']))}). {len(panos)} panoramas ranked; "
        f"{len(result.attempts)} downloaded. Images are centred on the computed bearing to the target, except "
        f"Mapillary photos that could not be re-aimed (used as taken).</p>"
        f"<main>{cards}</main>", encoding='utf-8')
