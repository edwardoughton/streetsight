"""Street-level imagery from Mapillary (free API, CC BY-SA imagery), used before paid Google Street View.

The access token is read from the MAPILLARY_ACCESS_TOKEN environment variable (on Windows, also from the
user's registry environment, so a token set with [Environment]::SetEnvironmentVariable works in any shell).
It is sent in a header, never in a URL, so it does not end up in logs.
"""
import datetime
import math
import os

import requests
from pyproj import Geod

API = 'https://graph.mapillary.com/images'
GEOD = Geod(ellps='WGS84')


def token():
    t = os.environ.get('MAPILLARY_ACCESS_TOKEN')
    if not t and os.name == 'nt':
        import winreg
        try:
            t = winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment'), 'MAPILLARY_ACCESS_TOKEN')[0]
        except OSError:
            t = None
    if not t:
        raise RuntimeError('set MAPILLARY_ACCESS_TOKEN')
    return t


def images_near(lat, lon, radius_m, fields='id,captured_at,computed_geometry,computed_compass_angle,is_pano', session=None):
    """Every image within radius_m: [(distance_m, bearing from image to point, image dict)], nearest first.
    The box is split into 3x3 tiles so dense areas do not hit the API's 2,000-image page limit."""
    s = session or requests.Session()
    hdr = {'Authorization': 'OAuth ' + token()}
    d = radius_m / 111320
    dx = d / math.cos(math.radians(lat))
    seen, out = set(), []
    for i in range(3):
        for j in range(3):
            bb = (f'{lon - dx + 2 * dx * i / 3},{lat - d + 2 * d * j / 3},'
                  f'{lon - dx + 2 * dx * (i + 1) / 3},{lat - d + 2 * d * (j + 1) / 3}')
            for k in range(3):
                try:
                    r = s.get(API, params={'fields': fields, 'bbox': bb, 'limit': 2000}, headers=hdr, timeout=90)
                    r.raise_for_status()
                    break
                except requests.RequestException:
                    if k == 2:
                        raise
            for im in r.json().get('data', []):
                g = im.get('computed_geometry')
                if not g or im['id'] in seen:
                    continue
                seen.add(im['id'])
                az, _, dist = GEOD.inv(*g['coordinates'], lon, lat)
                if dist <= radius_m:
                    out.append((dist, az % 360, im))
    return sorted(out, key=lambda t: t[0])


def coverage(lat, lon, radius_m=1500):
    """Summary of street-level coverage around a point, for the evidence pack."""
    ims = images_near(lat, lon, radius_m)
    pano = [t for t in ims if t[2].get('is_pano')]
    newest = max((t[2]['captured_at'] for t in ims), default=None)
    return {'mapillary_images_300m': sum(t[0] <= 300 for t in ims), 'mapillary_panos_300m': sum(t[0] <= 300 for t in pano),
            'mapillary_panos_1500m': len(pano), 'mapillary_nearest_pano_m': round(pano[0][0]) if pano else None,
            'mapillary_newest': (datetime.datetime.fromtimestamp(newest / 1000, datetime.timezone.utc).strftime('%Y-%m')
                                 if newest else None)}
