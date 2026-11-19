import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from streetsight import cache, pipeline, verify  # noqa: E402
from streetsight.mapillary import MapillaryError, perspective_fov, perspective_from_equirect, to_view  # noqa: E402

TOWER = (41.0, -88.0)
M = 111_320


def mly_image(iid, metres_south, compass=0.0, pano=True, params=None, w=4000, h=3000):
    return {'id': iid, 'captured_at': 1_725_000_000_000, 'is_pano': pano, 'camera_type': 'spherical' if pano else 'perspective',
            'computed_geometry': {'coordinates': [TOWER[1], TOWER[0] - metres_south / M]},
            'computed_compass_angle': compass, 'camera_parameters': params or [0.8, 0, 0], 'width': w, 'height': h,
            'creator': {'username': 'someone'}}


class MapillaryViewTests(unittest.TestCase):
    def test_pano_kept_whatever_its_heading(self):
        v = to_view(50, 0.0, mly_image(1, 50, compass=200))
        self.assertTrue(v['is_pano'])
        self.assertEqual(v['source'], 'mapillary')
        self.assertEqual(v['pano_date'], '2024-08')
        self.assertIn('someone', v['copyright'])

    def test_perspective_photo_facing_target_gets_offset(self):
        v = to_view(50, 10.0, mly_image(2, 50, compass=0, pano=False))
        self.assertAlmostEqual(v['target_offset_deg'], 10.0)
        self.assertGreater(v['camera_fov'], 40)

    def test_perspective_photo_facing_away_is_dropped(self):
        self.assertIsNone(to_view(50, 0.0, mly_image(3, 50, compass=180, pano=False)))
        self.assertIsNone(to_view(50, 0.0, {**mly_image(4, 50, pano=False), 'computed_compass_angle': None}))

    def test_fov_from_normalised_focal(self):
        self.assertAlmostEqual(perspective_fov({'camera_parameters': [0.5], 'width': 400, 'height': 300}), 90.0)
        self.assertEqual(perspective_fov({}), 60.0)

    def test_equirect_crop_looks_in_requested_direction(self):
        W, H = 720, 360
        pano = np.zeros((H, W, 3), dtype=np.uint8)
        pano[:, :, 0] = (np.arange(W) // 4)[None, :]          # red = column / 4 = 0..179
        pano[:, :, 1] = (np.arange(H) * 255 // H)[:, None]    # green = row (top 0)
        # Centre column of the panorama faces 100 deg; ask for 190 deg -> 90 deg right of centre -> column 540.
        view = perspective_from_equirect(pano, 100, 190, 0, 60, size=64)
        self.assertAlmostEqual(int(view[32, 32, 0]), 135, delta=1)
        view = perspective_from_equirect(pano, 100, 100, 0, 60, size=64)
        self.assertAlmostEqual(int(view[32, 32, 0]), 90, delta=1)
        up = perspective_from_equirect(pano, 100, 100, 30, 60, size=64)
        self.assertLess(int(up[32, 32, 1]), int(view[32, 32, 1]))  # pitched up -> higher row in the pano

    def test_pick_spread_skips_neighbours(self):
        items = [{'lat': TOWER[0] - d / M, 'lon': TOWER[1]} for d in (50, 55, 80)]
        self.assertEqual([round((TOWER[0] - p['lat']) * M) for p in pipeline.pick_spread(items, 2, 15)], [50, 80])
        self.assertEqual(len(pipeline.pick_spread(items, 3, 0)), 3)


class FakeMapillary:
    instances = []

    def __init__(self, *a, **k):
        self.log, self.images = [], []
        FakeMapillary.instances.append(self)

    def views(self, lat, lon, radius_m):
        self.log.append({'kind': 'mapillary_search', 'tag': 'bb', 'url': 'x'})
        return [to_view(50, 0.0, mly_image(11, 50)), to_view(52, 0.0, mly_image(12, 52)),
                to_view(90, 0.0, mly_image(13, 90))]

    def image(self, view, heading, pitch, fov):
        self.images.append(view['pano_id'])
        self.log.append({'kind': 'mapillary_image', 'tag': view['pano_id'], 'url': 'x'})
        return b'\xff\xd8fake'


class FakeGoogle:
    instances = []

    def __init__(self, *a, **k):
        self.log, self.images = [], []
        FakeGoogle.instances.append(self)

    def metadata(self, lat, lon, radius_m, tag=''):
        self.log.append({'kind': 'metadata', 'tag': tag, 'url': 'x'})
        return {'pano_id': 'g1', 'lat': TOWER[0] - 70 / M, 'lon': TOWER[1], 'pano_date': '2024-09', 'copyright': '© Google'}

    def image(self, pano_id, heading, pitch, fov):
        self.images.append(pano_id)
        self.log.append({'kind': 'static', 'tag': pano_id, 'url': 'x'})
        return b'\xff\xd8fake'


def no_mapillary(*a, **k):
    raise MapillaryError('No Mapillary token')


class ProviderOrderTests(unittest.TestCase):
    def setUp(self):
        FakeMapillary.instances.clear()
        FakeGoogle.instances.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_find(self, verifier, mapillary=FakeMapillary, **kw):
        road = LineString([(TOWER[1], TOWER[0] - 100 / M), (TOWER[1], TOWER[0] + 100 / M)])
        roads = gpd.GeoDataFrame({'id': ['r'], 'subtype': ['road'], 'class': ['x']}, geometry=[road], crs='EPSG:4326')
        with mock.patch.object(pipeline, 'MapillaryClient', mapillary), \
                mock.patch.object(pipeline, 'StreetViewClient', FakeGoogle), \
                mock.patch.object(pipeline.overture, 'roads', return_value=roads), \
                mock.patch.object(pipeline.overture, 'buildings', return_value=None):
            return pipeline.find_view(*TOWER, target_height_m=40, radius_m=150, verifier=verifier,
                                      out_dir=Path(self.tmp.name) / 'run', use_canopy=False, use_terrain=False,
                                      log=lambda *_: None, **kw)

    def test_mapillary_success_never_touches_google(self):
        seen = []
        result = self.run_find(lambda p, c: seen.append(c) or {'visible': True, 'confidence': 0.9, 'reason': 'ok'})
        self.assertTrue(result.found)
        self.assertEqual(result.best['source'], 'mapillary')
        self.assertTrue(Path(result.best['image']).name.startswith('mly_rank01_'))
        self.assertEqual(seen[0]['source'], 'Mapillary street-level')
        self.assertEqual(FakeGoogle.instances[0].log, [])  # no metadata, no billed images
        summary = json.loads((result.out_dir / 'result.json').read_text())
        self.assertEqual(summary['parameters']['providers_used'], ['mapillary'])
        self.assertEqual((summary['n_image_requests'], summary['n_mapillary_images']), (0, 1))

    def test_falls_back_to_google_and_spreads_mapillary_picks(self):
        result = self.run_find(lambda p, c: {'visible': False, 'confidence': 0.9, 'reason': 'trees'}, n_candidates=3)
        self.assertFalse(result.found)
        self.assertEqual(FakeMapillary.instances[0].images, ['11', '13'])  # 12 is 2 m from 11: skipped
        self.assertEqual(FakeGoogle.instances[0].images, ['g1'])
        self.assertEqual([a['source'] for a in result.attempts], ['mapillary', 'mapillary', 'google'])
        layers = {row[0] for row in __import__('pyogrio').list_layers(result.out_dir / 'results.gpkg')}
        self.assertTrue({'target', 'panoramas', 'view_wedges', 'sample_points'} <= layers)

    def test_provider_without_credentials_is_skipped(self):
        result = self.run_find(lambda p, c: {'visible': True, 'confidence': 0.9, 'reason': 'ok'}, mapillary=no_mapillary)
        self.assertEqual(result.best['source'], 'google')
        summary = json.loads((result.out_dir / 'result.json').read_text())
        self.assertIn('mapillary', summary['parameters']['providers_skipped'])

    def test_no_usable_provider_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.run_find(None, mapillary=no_mapillary, providers=['mapillary'])

    def test_unknown_provider(self):
        with self.assertRaises(ValueError):
            self.run_find(None, providers=['bing'])

    def test_mapillary_only_run_writes_outputs(self):
        result = self.run_find(None, providers='mapillary', n_candidates=1)
        self.assertEqual(len(result.attempts), 1)
        self.assertTrue((result.out_dir / 'results.gpkg').is_file())
        self.assertIn('Mapillary', (result.out_dir / 'index.html').read_text(encoding='utf-8'))


class SettingsTests(unittest.TestCase):
    def test_cache_and_runs_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ('STREETSIGHT_DATA', 'STREETSIGHT_CACHE', 'STREETSIGHT_RUNS'):
                os.environ.pop(k, None)
            self.assertEqual(cache.default_runs_dir(), Path('streetsight_runs'))
            self.assertIn('streetsight', str(cache.default_cache_dir()))
            os.environ['STREETSIGHT_DATA'] = 'proj'
            self.assertEqual(cache.default_cache_dir(), Path('proj/cache'))
            self.assertEqual(cache.default_runs_dir(), Path('proj/streetsight_runs'))
            os.environ['STREETSIGHT_CACHE'] = 'c'
            self.assertEqual(cache.default_cache_dir(), Path('c'))

    def test_auto_verifier_without_credentials_is_none(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'ANTHROPIC_AUTH_TOKEN': ''}):
            msgs = []
            self.assertIsNone(verify.resolve('auto', log=msgs.append))
            self.assertTrue(msgs)

    def test_prompt_for_unaimed_photo_says_where_to_look(self):
        text = verify.prompt({'target': 'mast', 'distance_m': 60, 'heading': 10, 'pitch': 0, 'fov': 70,
                              'target_height_m': 20, 'source': 'Mapillary street-level', 'target_offset_deg': -12,
                              'target_notes': 'Reject streetlights.'})
        self.assertIn('12 deg to the left', text)
        self.assertIn('Reject streetlights.', text)
        self.assertIn('Mapillary', text)


if __name__ == '__main__':
    unittest.main()
