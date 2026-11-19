import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from streetsight import batch, canopy, overture, pipeline, terrain  # noqa: E402
from streetsight.candidates import discover_ranked, road_sample_points  # noqa: E402
from streetsight.geometry import bearing_distance, framing  # noqa: E402
from streetsight.visibility import building_los, canopy_los, combine, score, terrain_los  # noqa: E402

TOWER = (41.0, -88.0)
M = 111_320  # metres per degree latitude


def pano_south(metres, pid=None):
    return {'pano_id': pid or f's{metres}', 'lat': TOWER[0] - metres / M, 'lon': TOWER[1]}


def building(near_edge_from_camera_m, camera_m, height, depth=5):
    lat0 = TOWER[0] - (camera_m - near_edge_from_camera_m) / M
    return {'id': 'b', 'height': height, 'num_floors': None,
            'geometry': box(TOWER[1] - 0.0002, lat0, TOWER[1] + 0.0002, lat0 + depth / M)}


def chm_tif(tmp, tree_centre_from_camera_m=25, camera_m=100, height=12):
    import rasterio
    from rasterio.transform import from_origin
    cell = 1 / M
    lat_top, lon_left = TOWER[0] + 10 * cell, TOWER[1] - 10 * cell
    arr = np.zeros((130, 20), dtype='uint8')
    row = int(round((lat_top - (TOWER[0] - (camera_m - tree_centre_from_camera_m) / M)) / cell))
    arr[row - 1:row + 2, :] = height
    tif = Path(tmp) / 'chm.tif'
    with rasterio.open(tif, 'w', driver='GTiff', height=130, width=20, count=1, dtype='uint8', crs='EPSG:4326',
                       transform=from_origin(lon_left, lat_top, cell * 1.3, cell)) as ds:
        ds.write(arr, 1)
    return tif


class GeometryTests(unittest.TestCase):
    def test_bearing_north_and_distance(self):
        h, d = bearing_distance(TOWER[0] - 100 / M, TOWER[1], *TOWER)
        self.assertAlmostEqual(h, 0, delta=0.01)
        self.assertAlmostEqual(d, 100, delta=0.5)

    def test_framing_fits_whole_tower_at_distance(self):
        pitch, fov = framing(100, 80, 0, 2.5)
        top, bottom = np.degrees(np.arctan2(77.5, 100)), np.degrees(np.arctan2(-2.5, 100))
        self.assertLessEqual(pitch - fov / 2, bottom)
        self.assertGreaterEqual(pitch + fov / 2, top)

    def test_framing_close_keeps_top_in_view(self):
        pitch, fov = framing(15, 80, 0, 2.5)
        self.assertEqual(fov, 100)
        self.assertGreaterEqual(pitch + fov / 2, np.degrees(np.arctan2(77.5, 15)))


class BuildingTests(unittest.TestCase):
    def test_clear_without_buildings(self):
        r = building_los([pano_south(100)], *TOWER, None)['s100']
        self.assertEqual(r['building_visible_above_m'], 0)

    def test_similar_triangles(self):
        b = overture.assign_heights(gpd.GeoDataFrame([building(25, 100, 10)], crs='EPSG:4326'))
        r = building_los([pano_south(100)], *TOWER, b, camera_h=2.5)['s100']
        self.assertAlmostEqual(r['building_visible_above_m'], 32.5, delta=0.5)  # 2.5 + 7.5 * 100/25

    def test_missing_height_uses_floors_then_default(self):
        b = gpd.GeoDataFrame([{'id': 1, 'height': None, 'num_floors': 2, 'geometry': box(0, 0, 1, 1)},
                              {'id': 2, 'height': None, 'num_floors': None, 'geometry': box(0, 0, 1, 1)}],
                             crs='EPSG:4326')
        self.assertEqual(list(overture.assign_heights(b)['height_used_m']), [6.0, 5.0])


class CanopyTests(unittest.TestCase):
    def test_quadkey_matches_meta_tile_for_illinois_case(self):
        self.assertEqual(canopy.quadkey(41.288927, -88.217081), '030222232')

    def test_tree_between_camera_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = canopy_los([pano_south(100)], *TOWER, chm_tif(tmp))['s100']
        # 3 m band, near edge ~23.5 m: 2.5 + 9.5 * 100 / 23.5 = 42.9 m
        self.assertGreater(r['canopy_visible_above_m'], 41)
        self.assertLess(r['canopy_visible_above_m'], 46)

    def test_combine_and_score(self):
        r = combine(5, 40, 80)
        self.assertEqual((r['visibility'], r['visible_fraction']), ('partly_hidden_by_canopy', 0.5))
        self.assertEqual(combine(90, 0, 80)['visibility'], 'hidden_by_building')
        self.assertGreater(score(60, 0, 0, 80, '© Google'), score(60, 0, 40, 80, '© Google'))
        self.assertGreater(score(60, 0, 0, 80, '© Google'), score(250, 0, 0, 80, '© Google'))
        self.assertGreater(score(60, 0, 0, 80, '© Google'), score(60, 0, 0, 80, '© Someone'))


class RoadSamplingTests(unittest.TestCase):
    def test_points_along_road_include_nearest_point(self):
        road = LineString([(TOWER[1] - 0.003, TOWER[0] - 50 / M), (TOWER[1] + 0.003, TOWER[0] - 50 / M)])
        roads = gpd.GeoDataFrame({'subtype': ['road'], 'class': ['residential']}, geometry=[road], crs='EPSG:4326')
        pts, spacing, mode = road_sample_points(roads, *TOWER, 200, 30, 250)
        self.assertEqual(mode, 'roads')
        self.assertTrue(10 <= len(pts) <= 16)  # ~2 * sqrt(200^2 - 50^2) / 30
        nearest = min(bearing_distance(la, lo, *TOWER)[1] for la, lo in pts)
        self.assertAlmostEqual(nearest, 50, delta=1)

    def test_spacing_grows_to_respect_query_cap(self):
        road = LineString([(TOWER[1] - 0.003, TOWER[0]), (TOWER[1] + 0.003, TOWER[0])])
        roads = gpd.GeoDataFrame({'subtype': ['road'], 'class': ['x']}, geometry=[road], crs='EPSG:4326')
        pts, spacing, _ = road_sample_points(roads, *TOWER, 200, 10, 5)
        self.assertLessEqual(len(pts), 6)
        self.assertGreater(spacing, 10)

    def test_rings_without_roads(self):
        self.assertEqual(road_sample_points(None, *TOWER, 200, 50, 100)[2], 'rings')


class GridClient:
    """Each query returns a panorama snapped to a 40 m grid, or nothing if empty=True."""

    def __init__(self, empty=False):
        self.queries, self.empty = [], empty

    def metadata(self, lat, lon, radius_m, tag=''):
        self.queries.append(tag)
        if self.empty:
            return None
        g = 40 / M
        la, lo = round(lat / g) * g, round(lon / g) * g
        return {'pano_id': f'{la:.6f},{lo:.6f}', 'lat': la, 'lon': lo, 'pano_date': '2024-01', 'copyright': '© Google'}


class RankedDiscoveryTests(unittest.TestCase):
    POINTS = [(TOWER[0] - k * 10 / M, TOWER[1]) for k in range(1, 26)]  # every 10 m south, 10-250 m

    def test_best_points_first_skip_duplicates_and_stop_at_want(self):
        scores = [1 / (k + 1) for k in range(len(self.POINTS))]  # nearest first
        c = GridClient()
        panos, recs = discover_ranked(c, self.POINTS, scores, *TOWER, 300, 25, want=3, workers=1, log=lambda *_: None)
        self.assertEqual(len(panos), 3)
        self.assertEqual(c.queries[0], 'pt0')
        self.assertLess(len(c.queries), 10)
        self.assertTrue(any(r['skipped_near_pano'] for r in recs))
        self.assertEqual([r['query_order'] for r in recs if r['queried']][:1], [1])

    def test_stops_after_dry_run_without_coverage(self):
        c = GridClient(empty=True)
        panos, _ = discover_ranked(c, self.POINTS, [1.0] * 25, *TOWER, 300, 25, max_dry=6, workers=2,
                                   log=lambda *_: None)
        self.assertEqual(panos, [])
        self.assertEqual(len(c.queries), 6)

    def test_respects_max_queries(self):
        c = GridClient()
        discover_ranked(c, self.POINTS, [1.0] * 25, *TOWER, 300, 5, want=99, max_queries=4, skip_near_m=0,
                        log=lambda *_: None)
        self.assertEqual(len(c.queries), 4)


class FakeClient:
    """Two panoramas: one clear at 60 m south, one behind a tall building at 80 m north."""
    PANOS = {'clear': pano_south(60, 'clear'), 'blocked': {'pano_id': 'blocked', 'lat': TOWER[0] + 80 / M, 'lon': TOWER[1]}}

    def __init__(self, *a, **k):
        self.log, self.images = [], []

    def metadata(self, lat, lon, radius_m, tag=''):
        self.log.append({'kind': 'metadata', 'tag': tag, 'url': 'x&key=REDACTED'})
        p = self.PANOS['clear' if lat < TOWER[0] else 'blocked']
        return {**p, 'pano_date': '2024-09', 'copyright': '© Google'}

    def image(self, pano_id, heading, pitch, fov):
        self.images.append(pano_id)
        self.log.append({'kind': 'static', 'tag': pano_id, 'url': 'x&key=REDACTED'})
        return b'\xff\xd8fakejpeg'


class PipelineTests(unittest.TestCase):
    def run_find(self, verifier, **kw):
        road = LineString([(TOWER[1], TOWER[0] - 100 / M), (TOWER[1], TOWER[0] + 100 / M)])
        roads = gpd.GeoDataFrame({'id': ['r'], 'subtype': ['road'], 'class': ['residential']},
                                 geometry=[road], crs='EPSG:4326')
        tall = {'id': 'tall', 'height': 30.0, 'num_floors': None,
                'geometry': box(TOWER[1] - 0.0003, TOWER[0] + 40 / M, TOWER[1] + 0.0003, TOWER[0] + 50 / M)}
        bld = overture.assign_heights(gpd.GeoDataFrame([tall], crs='EPSG:4326'))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with mock.patch.object(pipeline, 'StreetViewClient', FakeClient), \
                mock.patch.object(pipeline.overture, 'roads', return_value=roads), \
                mock.patch.object(pipeline.overture, 'buildings', return_value=bld):
            return pipeline.find_view(*TOWER, target_height_m=40, radius_m=150, verifier=verifier,
                                      out_dir=Path(self.tmp.name) / 'run', use_canopy=False, use_fcc=False,
                                      use_terrain=False, log=lambda *_: None, **kw)

    def test_ranks_clear_view_first_and_stops_when_verified(self):
        seen = []
        result = self.run_find(lambda path, ctx: seen.append(ctx) or {'visible': True, 'confidence': 0.9, 'reason': 'ok'})
        self.assertTrue(result.found)
        self.assertEqual(result.best['pano_id'], 'clear')
        self.assertEqual(len(result.attempts), 1)
        self.assertAlmostEqual(seen[0]['heading'], 0, delta=0.1)
        out = result.out_dir
        for f in ('results.gpkg', 'candidates.csv', 'requests.csv', 'result.json', 'index.html'):
            self.assertTrue((out / f).is_file(), f)
        self.assertTrue((out / result.best['image']).is_file())
        layers = {row[0] for row in __import__('pyogrio').list_layers(out / 'results.gpkg')}
        self.assertTrue({'target', 'panoramas', 'sight_lines', 'view_wedges', 'buildings', 'roads'} <= layers)

    def test_moves_to_next_candidate_when_not_visible(self):
        result = self.run_find(lambda p, c: {'visible': False, 'confidence': 0.9, 'reason': 'tree'})
        self.assertFalse(result.found)
        self.assertEqual([a['pano_id'] for a in result.attempts], ['clear', 'blocked'])

    def test_second_pass_reuses_rank1_and_downloads_rank2_only_if_unconfirmed(self):
        from streetsight.verify import ReviewFileVerifier
        first = self.run_find(None, n_candidates=1)
        out = first.out_dir
        img = Path(first.attempts[0]['image']).name
        review = out.parent / 'review.csv'
        review.write_text('image,visible,confidence,reason\n' + f'{img},no,0.9,tree\n', encoding='utf-8')
        with mock.patch.object(pipeline, 'StreetViewClient', FakeClient):
            road = LineString([(TOWER[1], TOWER[0] - 100 / M), (TOWER[1], TOWER[0] + 100 / M)])
            roads = gpd.GeoDataFrame({'id': ['r'], 'subtype': ['road'], 'class': ['x']}, geometry=[road], crs='EPSG:4326')
            with mock.patch.object(pipeline.overture, 'roads', return_value=roads),                     mock.patch.object(pipeline.overture, 'buildings', return_value=None):
                second = pipeline.find_view(*TOWER, target_height_m=40, radius_m=150, n_candidates=2,
                                            verifier=ReviewFileVerifier(review), out_dir=out, use_canopy=False,
                                            use_fcc=False, use_terrain=False, log=lambda *_: None)
        statics = [r for r in __import__('json').loads((out / 'result.json').read_text())['attempts']]
        self.assertEqual([a['verified_visible'] for a in statics], [False, None])
        n_static = sum(1 for line in (out / 'requests.csv').read_text().splitlines() if line.startswith('static'))
        self.assertEqual(n_static, 1)  # only rank 2 was billed on the second pass

    def test_no_verifier_downloads_all_candidates(self):
        result = self.run_find(None, n_candidates=1)
        self.assertFalse(result.found)
        self.assertEqual(len(result.attempts), 1)



def dem_tif(tmp, ridge_from_camera_m=50, camera_m=100, ridge_h=20.0, base=200.0):
    """EPSG:4326 DEM at 1 m: flat at `base`, with an east-west ridge between camera (south) and tower."""
    import rasterio
    from rasterio.transform import from_origin
    cell = 1 / M
    lat_top, lon_left = TOWER[0] + 10 * cell, TOWER[1] - 10 * cell
    arr = np.full((130, 20), base, dtype='float32')
    row = int(round((lat_top - (TOWER[0] - (camera_m - ridge_from_camera_m) / M)) / cell))
    arr[row - 2:row + 3, :] = base + ridge_h
    tif = Path(tmp) / 'dem.tif'
    with rasterio.open(tif, 'w', driver='GTiff', height=130, width=20, count=1, dtype='float32', crs='EPSG:4326',
                       transform=from_origin(lon_left, lat_top, cell * 1.3, cell)) as ds:
        ds.write(arr, 1)
    return tif


class TerrainTests(unittest.TestCase):
    def test_tile_name(self):
        self.assertEqual(terrain.tile_name(41.288927, -88.217081), 'n42w089')
        self.assertEqual(terrain.tile_name(21.3, -157.8), 'n22w158')

    def test_ridge_blocks_lower_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = terrain.Ground(dem_tif(tmp))
            z = float(g([TOWER[1]], [TOWER[0]])[0])
            self.assertAlmostEqual(z, 200, delta=0.01)
            r = terrain_los([pano_south(100)], *TOWER, g, z)['s100']
        # ridge top 20 m at ~48-52 m: 2.5 + 17.5 * 100 / 50 = 37.5 m; 5 m sampling and bilinear
        # interpolation across a 5 m wide ridge read its top a little low
        self.assertGreater(r['terrain_visible_above_m'], 30)
        self.assertLess(r['terrain_visible_above_m'], 42)

    def test_flat_dem_is_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = terrain.Ground(dem_tif(tmp, ridge_h=0))
            r = terrain_los([pano_south(100)], *TOWER, g, 200.0)['s100']
        self.assertEqual(r['terrain_visible_above_m'], 0)

    def test_camera_below_target_frames_higher(self):
        flat, low = framing(100, 40, 0, 2.5), framing(100, 40, 0, 2.5 - 20)
        self.assertGreater(low[0], flat[0])


class AgeAndFramingTests(unittest.TestCase):
    def test_predates(self):
        self.assertTrue(pipeline.predates('1999-05', '02/21/2000'))
        self.assertFalse(pipeline.predates('2024-09', '02/21/2000'))
        self.assertFalse(pipeline.predates('2024-09', None))
        self.assertGreater(score(60, 0, 0, 80, '© Google'), 5 * score(60, 0, 0, 80, '© Google', predates_target=True))

    def test_position_uncertainty_widens_close_views(self):
        _, fov = framing(15, 10, 0, 2.5, position_uncertainty_m=10)
        self.assertGreaterEqual(fov, 2 * np.degrees(np.arctan2(10, 15)))
        self.assertEqual(framing(200, 10, 0, 2.5, position_uncertainty_m=10)[1], 30)


class BatchTests(unittest.TestCase):
    def test_batch_writes_summary_and_resumes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls = []

        def fake_find_view(lat, lon, *, site_id, out_dir, log, **kw):
            calls.append((site_id, kw['n_candidates']))
            out_dir.mkdir(parents=True, exist_ok=True)
            best = {'pano_id': 'p', 'rank': 1, 'distance_m': 50, 'heading_to_target': 10, 'lat': lat - 0.0005,
                    'lon': lon, 'image': 'images/x.jpg', 'verified_confidence': 0.9}
            (out_dir / 'result.json').write_text(__import__('json').dumps({
                'found': True, 'best': best, 'target': {'lat': lat, 'lon': lon, 'height_m': 30},
                'parameters': {'n_candidates': kw['n_candidates'], 'verifier': 'm'},
                'n_panoramas': 3, 'n_image_requests': 2}))

        sites = [{'id': 'A', 'latitude': 41.0, 'longitude': -88.0}, {'id': 'B/2', 'latitude': 41.1, 'longitude': -88.1}]
        with mock.patch.object(batch, 'find_view', fake_find_view):
            s = batch.find_views(sites, out_root=Path(tmp.name), max_images_total=3, log=lambda *_: None)
            self.assertEqual(calls, [('A', 3), ('B_2', 1)])
            self.assertEqual(list(s['found']), [True, True])
            self.assertTrue((Path(tmp.name) / 'summary.gpkg').is_file())
            layers = {r[0] for r in __import__('pyogrio').list_layers(Path(tmp.name) / 'summary.gpkg')}
            self.assertEqual(layers, {'sites', 'best_cameras', 'best_sight_lines'})
            batch.find_views(sites, out_root=Path(tmp.name), log=lambda *_: None)
            self.assertEqual(len(calls), 2)  # both resumed

    def test_bad_columns(self):
        with self.assertRaises(ValueError):
            batch.find_views([{'x': 1}], out_root=tempfile.gettempdir(), log=lambda *_: None)


if __name__ == '__main__':
    unittest.main()
