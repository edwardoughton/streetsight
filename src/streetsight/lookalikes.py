"""Mapped OpenStreetMap infrastructure that the tower detector confuses with tower compounds.

From above, gas regulator and valve stations, substations, wind-turbine pads, pumping stations, tanks and
culverts are fenced or concrete squares like a tower site. A mapped one under a detection is evidence
against a tower; an unmapped one is common, so absence proves nothing.
"""
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon

QUERY = ('[out:json][timeout:240];('
         'nwr["power"~"^(substation|plant|generator|transformer|switch)$"]({bb});'
         'nwr["pipeline"~"^(substation|valve|marker)$"]({bb});nwr["substation"]({bb});nwr["utility"]({bb});'
         'nwr["industrial"~"^(natural_gas|oil|gas)$"]({bb});'
         'nwr["man_made"~"^(pipeline_station|pumping_station|wastewater_plant|water_works|works|petroleum_well|'
         'storage_tank|silo|water_tower|reservoir_covered|street_cabinet|culvert)$"]({bb});'
         'way["tunnel"="culvert"]({bb});nwr["amenity"="fuel"]({bb}););out geom tags;')


def kind(tags):
    """(label, match radius in metres) for an OSM element, or (None, 0) if it is not a look-alike."""
    t = tags
    if t.get('tunnel') == 'culvert' or t.get('man_made') == 'culvert':
        return 'culvert', 15
    if ('pipeline' in t or t.get('utility') == 'gas' or t.get('industrial') in ('natural_gas', 'gas', 'oil')
            or t.get('man_made') in ('pipeline_station', 'petroleum_well')):
        return 'oil/gas facility', 30
    if t.get('power') == 'generator':
        return f"{t.get('generator:source', 'power')} generator", 30
    if t.get('power') in ('substation', 'transformer', 'switch') or 'substation' in t:
        return 'electricity substation', 30
    if t.get('power') == 'plant':
        return f"{t.get('plant:source', 'power')} plant", 30
    if t.get('man_made') in ('pumping_station', 'water_works', 'wastewater_plant', 'works', 'water_tower', 'reservoir_covered'):
        return t['man_made'].replace('_', ' '), 30
    if t.get('man_made') in ('storage_tank', 'silo'):
        return t['man_made'].replace('_', ' ') + (f" ({t['content']})" if 'content' in t else ''), 30
    if t.get('amenity') == 'fuel':
        return 'fuel station', 30
    if t.get('man_made') == 'street_cabinet':
        return 'utility cabinet', 15
    return None, 0


class Lookalikes:
    """Index of look-alike features from an Overpass `out geom` response, in an equal-area projection (metres).
    Relations are skipped: in practice they are wind or solar farm outlines spanning many km², and their
    individual turbines and panels are mapped and matched separately."""

    def __init__(self, elements, crs=5070):
        self.to_m = Transformer.from_crs(4326, crs, always_xy=True).transform
        self.features = []
        for e in elements:
            label, radius = kind(e.get('tags', {}))
            if not label or e['type'] == 'relation':
                continue
            if e['type'] == 'node':
                geom = Point(*self.to_m(e['lon'], e['lat']))
            else:
                pts = [self.to_m(p['lon'], p['lat']) for p in e['geometry']]
                closed = len(pts) > 3 and pts[0] == pts[-1] and label != 'culvert'
                geom = Polygon(pts) if closed else LineString(pts)
            self.features.append((label, radius, f"{e['type']}/{e['id']}", geom))

    def near(self, lon, lat, limit=3):
        """Look-alikes within each one's match radius: [{'kind', 'm', 'osm'}], nearest first."""
        p = Point(*self.to_m(lon, lat))
        hits = sorted((round(g.distance(p)), label, fid) for label, radius, fid, g in self.features if g.distance(p) <= radius)
        return [{'kind': label, 'm': m, 'osm': fid} for m, label, fid in hits[:limit]]
