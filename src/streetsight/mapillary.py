"""Street-level imagery from Mapillary (free API, CC BY-SA imagery), tried before paid Google Street View.

The access token comes from mapillary_token=, else the MAPILLARY_ACCESS_TOKEN environment variable (on
Windows, also the user's registry environment, so a token set with [Environment]::SetEnvironmentVariable
works in any shell). It is sent in a header, never in a URL, so it does not end up in logs.

Mapillary images cannot be re-aimed on the server the way Google Street View can:
- 360-degree panoramas are downloaded whole and cropped here to an aimed view (heading, pitch, fov).
- Ordinary (perspective) photos are kept only if the target lies inside their field of view. They are used as
  taken, and the verifier is told where in the frame the target should be.
"""
import datetime
import io
import math
import os

import numpy as np
import requests
from pyproj import Geod

API = 'https://graph.mapillary.com'
IMAGES = f'{API}/images'
GEOD = Geod(ellps='WGS84')
FIELDS = ('id,captured_at,computed_geometry,computed_compass_angle,compass_angle,is_pano,camera_type,'
          'camera_parameters,width,height,creator')
DEFAULT_PERSPECTIVE_FOV = 60.0
TOKEN_ENV = 'MAPILLARY_ACCESS_TOKEN'


class MapillaryError(RuntimeError):
    pass


def token(value=None):
    t = (value or os.environ.get(TOKEN_ENV) or '').strip()
    if not t and os.name == 'nt':
        import winreg
        try:
            t = winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment'), TOKEN_ENV)[0].strip()
        except OSError:
            t = ''
    if not t:
        raise MapillaryError(f'No Mapillary token: set the {TOKEN_ENV} environment variable or pass mapillary_token=.')
    return t


def _get(session, url, params, headers, timeout, tries=3):
    for k in range(tries):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code in (401, 403):
                raise MapillaryError(f'Mapillary refused the token (HTTP {r.status_code})')
            r.raise_for_status()
            return r
        except requests.RequestException:
            if k == tries - 1:
                raise


def images_near(lat, lon, radius_m, fields=FIELDS, session=None, access_token=None, log=None):
    """Every image within radius_m: [(distance_m, bearing from image to point, image dict)], nearest first.
    The box is split into 3x3 tiles so dense areas do not hit the API's 2,000-image page limit."""
    s = session or requests.Session()
    hdr = {'Authorization': 'OAuth ' + token(access_token)}
    d = radius_m / 111320
    dx = d / math.cos(math.radians(lat))
    seen, out = set(), []
    for i in range(3):
        for j in range(3):
            bb = (f'{lon - dx + 2 * dx * i / 3:.7f},{lat - d + 2 * d * j / 3:.7f},'
                  f'{lon - dx + 2 * dx * (i + 1) / 3:.7f},{lat - d + 2 * d * (j + 1) / 3:.7f}')
            r = _get(s, IMAGES, {'fields': fields, 'bbox': bb, 'limit': 2000}, hdr, 90)
            data = r.json().get('data', [])
            if log is not None:
                log.append({'kind': 'mapillary_search', 'tag': bb, 'http_status': r.status_code, 'api_status': 'OK',
                            'detail': f'{len(data)} images', 'url': IMAGES})
            for im in data:
                g = im.get('computed_geometry')
                if not g or im['id'] in seen:
                    continue
                seen.add(im['id'])
                az, _, dist = GEOD.inv(*g['coordinates'], lon, lat)
                if dist <= radius_m:
                    out.append((dist, az % 360, im))
    return sorted(out, key=lambda t: t[0])


def coverage(lat, lon, radius_m=1500, access_token=None):
    """Summary of Mapillary coverage around a point."""
    ims = images_near(lat, lon, radius_m, access_token=access_token)
    pano = [t for t in ims if t[2].get('is_pano')]
    newest = max((t[2]['captured_at'] for t in ims), default=None)
    return {'mapillary_images_300m': sum(t[0] <= 300 for t in ims), 'mapillary_panos_300m': sum(t[0] <= 300 for t in pano),
            'mapillary_panos_1500m': len(pano), 'mapillary_nearest_pano_m': round(pano[0][0]) if pano else None,
            'mapillary_newest': _month(newest)}


def _month(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime('%Y-%m') if ms else None


def _is_pano(im):
    return bool(im.get('is_pano')) or im.get('camera_type') in ('spherical', 'equirectangular')


def perspective_fov(im):
    """Horizontal field of view (deg) of a perspective image from its camera parameters, else 60.
    Mapillary's focal length is normalised by the larger image dimension."""
    params, w, h = im.get('camera_parameters') or [], im.get('width'), im.get('height')
    f = params[0] if params else None
    if not f or f <= 0:
        return DEFAULT_PERSPECTIVE_FOV
    across = 1.0 if not (w and h) or w >= h else w / h
    return round(math.degrees(2 * math.atan(across / (2 * f))), 1)


def to_view(dist, bearing, im, edge_margin=0.8):
    """Candidate dict for one image (same keys as a Google panorama), or None if a perspective photo does not
    face the target (target outside edge_margin x half its field of view) or has no compass angle."""
    lon, lat = im['computed_geometry']['coordinates']
    compass = im.get('computed_compass_angle', im.get('compass_angle'))
    user = (im.get('creator') or {}).get('username')
    v = {'pano_id': str(im['id']), 'lat': lat, 'lon': lon, 'pano_date': _month(im.get('captured_at')) or '',
         'copyright': 'Mapillary' + (f' / {user}' if user else ''), 'source': 'mapillary',
         'heading_to_target': round(bearing, 1), 'distance_m': round(dist, 1), 'is_pano': _is_pano(im),
         'camera_heading': None if compass is None else round(float(compass) % 360, 1)}
    if v['is_pano']:
        return v
    if compass is None:
        return None
    fov = perspective_fov(im)
    off = (bearing - float(compass) + 180) % 360 - 180
    if abs(off) > edge_margin * fov / 2:
        return None
    w, h = im.get('width'), im.get('height')
    aspect = h / w if w and h else 0.75
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(fov) / 2) * aspect))
    return {**v, 'camera_fov': fov, 'camera_vfov': round(vfov, 1), 'target_offset_deg': round(off, 1)}


def in_vertical_view(view, camera_h, edge_margin=0.9):
    """For a level perspective photo: does the unobstructed part of the target (from visible_above_m up to its
    top, after line of sight) reach into the frame? Panoramas are always True (they are cropped to fit)."""
    if view.get('is_pano') or view.get('camera_vfov') is None:
        return True
    d = max(view['distance_m'], 1.0)
    cam = camera_h + view.get('ground_offset_m', 0.0)
    bottom = math.degrees(math.atan2(view.get('visible_above_m', 0.0) - cam, d))
    return bottom < edge_margin * view['camera_vfov'] / 2


def perspective_from_equirect(pano, centre_heading, heading, pitch, fov, size=640):
    """Rectilinear view (size x size) cut from an equirectangular panorama (H x W x 3 uint8 array).

    centre_heading : compass heading of the panorama's middle column. heading, pitch, fov : the view wanted
    (deg; fov is horizontal, and the output is square). Nearest-neighbour sampling.
    """
    H, W = pano.shape[:2]
    f = (size / 2) / math.tan(math.radians(fov) / 2)
    xs = np.arange(size) - (size - 1) / 2
    x, y = np.meshgrid(xs, -xs)  # right, up
    z = np.full_like(x, f)
    p = math.radians(pitch)
    y2, z2 = y * math.cos(p) + z * math.sin(p), -y * math.sin(p) + z * math.cos(p)
    az = np.degrees(np.arctan2(x, z2)) + (heading - centre_heading)
    el = np.degrees(np.arctan2(y2, np.hypot(x, z2)))
    u = (((az / 360 + 0.5) % 1.0) * W).astype(int) % W
    v = np.clip(((0.5 - el / 180) * H).astype(int), 0, H - 1)
    return pano[v, u]


class MapillaryClient:
    """Free Mapillary search and image download, with a request log like StreetViewClient's."""
    source = 'mapillary'

    def __init__(self, access_token=None, timeout=90, session=None):
        self._token = token(access_token)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.log = []

    @property
    def _hdr(self):
        return {'Authorization': 'OAuth ' + self._token}

    def views(self, lat, lon, radius_m):
        """Usable images within radius_m of (lat, lon): panoramas, and perspective photos facing the target."""
        found = images_near(lat, lon, radius_m, session=self.session, access_token=self._token, log=self.log)
        return [v for v in (to_view(*t) for t in found) if v]

    def image(self, view, heading, pitch, fov, size=640):
        """JPEG bytes. Panoramas are cropped to the requested view; perspective photos are returned as taken."""
        from PIL import Image
        field = 'thumb_original_url' if view.get('is_pano') else 'thumb_2048_url'
        meta = _get(self.session, f"{API}/{view['pano_id']}", {'fields': field}, self._hdr, self.timeout).json()
        url = meta.get(field)
        if not url:
            raise MapillaryError(f"No {field} for Mapillary image {view['pano_id']}")
        r = _get(self.session, url, None, None, self.timeout)
        self.log.append({'kind': 'mapillary_image', 'tag': view['pano_id'], 'http_status': r.status_code,
                         'api_status': 'IMAGE_OK', 'pano_id': view['pano_id'], 'url': url.split('?')[0]})
        if not view.get('is_pano'):
            return r.content
        pano = np.asarray(Image.open(io.BytesIO(r.content)).convert('RGB'))
        centre = view.get('camera_heading') or 0.0
        out = Image.fromarray(perspective_from_equirect(pano, centre, heading, pitch, fov, size))
        buf = io.BytesIO()
        out.save(buf, 'JPEG', quality=90)
        return buf.getvalue()
