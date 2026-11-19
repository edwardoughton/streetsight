"""Google Street View Static API client. The key never reaches logs, outputs or exception text."""
import os
import threading

import requests

META_URL = 'https://maps.googleapis.com/maps/api/streetview/metadata'
STATIC_URL = 'https://maps.googleapis.com/maps/api/streetview'
KEY_ENV = 'GOOGLE_MAPS_API_KEY'


class StreetViewError(RuntimeError):
    pass


def resolve_key(key=None, key_file=None):
    if key:
        return key.strip()
    if key_file:
        return open(key_file, encoding='utf-8-sig').read().strip()
    key = os.environ.get(KEY_ENV, '').strip()
    if not key:
        raise StreetViewError(f'No Google key: set the {KEY_ENV} environment variable or pass google_api_key=.')
    return key


class StreetViewClient:
    """Metadata requests are free; Static image requests are billed."""

    def __init__(self, key=None, key_file=None, timeout=30):
        self._key = resolve_key(key, key_file)
        self.timeout = timeout
        self.log = []
        self._lock = threading.Lock()
        self._fatal = None

    def redact(self, text):
        return str(text).replace(self._key, 'REDACTED')

    def _get(self, kind, url, params, tag):
        if self._fatal:
            raise StreetViewError(self._fatal)
        prepared = requests.Request('GET', url, params={**params, 'key': self._key}).prepare()
        row = {'kind': kind, 'tag': tag, 'url': self.redact(prepared.url)}
        try:
            with requests.Session() as s:
                resp = s.send(prepared, timeout=self.timeout)
            row['http_status'] = resp.status_code
        except requests.RequestException as exc:
            resp = None
            row.update(http_status='', api_status='NETWORK_ERROR', detail=self.redact(exc))
        with self._lock:
            self.log.append(row)
        return resp, row

    def metadata(self, lat, lon, radius_m, tag=''):
        """Nearest outdoor panorama within radius_m, or None. Raises on key/quota problems."""
        resp, row = self._get('metadata', META_URL, {'location': f'{lat:.7f},{lon:.7f}', 'radius': int(radius_m),
                                                     'source': 'outdoor'}, tag)
        body = resp.json() if resp is not None and resp.ok else {}
        row['api_status'] = body.get('status', row.get('api_status', 'HTTP_ERROR'))
        row['pano_id'] = body.get('pano_id', '')
        if body.get('error_message'):
            row['detail'] = self.redact(body['error_message'])
        if row['api_status'] in ('REQUEST_DENIED', 'OVER_QUERY_LIMIT', 'OVER_DAILY_LIMIT'):
            self._fatal = f"Street View metadata {row['api_status']}: {row.get('detail', '')}"
            raise StreetViewError(self._fatal)
        if row['api_status'] != 'OK':
            return None
        loc = body['location']
        return {'pano_id': body['pano_id'], 'lat': loc['lat'], 'lon': loc['lng'],
                'pano_date': body.get('date', ''), 'copyright': body.get('copyright', '')}

    def image(self, pano_id, heading, pitch, fov, size='640x640'):
        """JPEG bytes for a view from pano_id. Billed."""
        resp, row = self._get('static', STATIC_URL, {'size': size, 'pano': pano_id, 'heading': heading, 'pitch': pitch,
                                                     'fov': fov, 'return_error_code': 'true'}, pano_id)
        if resp is not None and resp.ok and resp.headers.get('Content-Type', '').startswith('image/'):
            row['api_status'] = 'IMAGE_OK'
            return resp.content
        row['api_status'] = 'IMAGE_FAILED'
        row['detail'] = self.redact(resp.text[:300]) if resp is not None else row.get('detail', '')
        raise StreetViewError(f"Static image failed for {pano_id}: {row['detail']}")
