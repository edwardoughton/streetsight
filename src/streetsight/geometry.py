"""Small geodesic helpers and camera framing. No network access."""
import math

from pyproj import Geod
from shapely.geometry import Polygon

GEOD = Geod(ellps='WGS84')
EARTH_R = 6371008.8


def bearing_distance(lat1, lon1, lat2, lon2):
    """Initial compass bearing (deg, clockwise from north) and haversine distance (m) from 1 to 2."""
    vals = [float(v) for v in (lat1, lon1, lat2, lon2)]
    if not all(math.isfinite(v) for v in vals):
        raise ValueError('Non-finite coordinates')
    a, b, c, d = vals
    if abs(a) > 90 or abs(c) > 90 or abs(b) > 180 or abs(d) > 180:
        raise ValueError('Coordinates out of range')
    p1, p2, dl = math.radians(a), math.radians(c), math.radians(d - b)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    dist = 2 * EARTH_R * math.asin(min(1, math.sqrt(h)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return round((math.degrees(math.atan2(y, x)) + 360) % 360, 2), round(dist, 1)


def offset(lat, lon, azimuth, distance):
    lon2, lat2, _ = GEOD.fwd(lon, lat, azimuth, distance)
    return lat2, lon2


def bbox_around(lat, lon, half_m):
    dlat = half_m / 111_320
    dlon = half_m / (111_320 * max(0.01, abs(math.cos(math.radians(lat)))))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def circle(lat, lon, radius, n=72):
    return Polygon([offset(lat, lon, a, radius)[::-1] for a in range(0, 360, 360 // n)])


def wedge(lat, lon, heading, fov, length, steps=12):
    pts = [(lon, lat)] + [offset(lat, lon, heading - fov / 2 + fov * i / steps, length)[::-1]
                          for i in range(steps + 1)]
    return Polygon(pts)


def framing(distance_m, target_h, visible_above_m=0.0, camera_h=2.5, margin=1.25, min_fov=30.0, max_fov=100.0,
            position_uncertainty_m=0.0):
    """Pitch and field of view (deg) that fit the visible part of the target in a square image.

    camera_h is the camera height relative to the target's base (negative if the camera is lower).
    position_uncertainty_m widens the view so a target that far to either side stays in frame.

    The visible part runs from visible_above_m to target_h. If it is too tall to fit at max_fov,
    the frame keeps the top (where antennas are) and lets the lower part fall out of view.
    Street View Static fov is horizontal; with a square image the vertical fov is the same.
    """
    d = max(distance_m, 1.0)
    top = math.degrees(math.atan2(target_h - camera_h, d))
    bottom = math.degrees(math.atan2(min(max(visible_above_m, 0.0), target_h * 0.9) - camera_h, d))
    side = 2 * math.degrees(math.atan2(max(position_uncertainty_m, 0.0), d)) * margin
    fov = min(max((top - bottom) * margin, min_fov, side), max_fov)
    pitch = (top + bottom) / 2
    if (top - bottom) * margin > max_fov:
        pitch = top - max_fov / 2 / margin
    return round(max(-90.0, min(90.0, pitch)), 1), round(fov, 1)
