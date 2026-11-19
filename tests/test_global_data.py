import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import geopandas as gpd
import numpy as np
from rasterio.transform import from_origin
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from streetsight import ghsl, overture, terrain  # noqa: E402


def buildings():
    rows = [{'id': 'known', 'height': 22.0, 'num_floors': None, 'geometry': box(10.0001, 50.0001, 10.0002, 50.0002)},
            {'id': 'floors', 'height': None, 'num_floors': 4, 'geometry': box(10.0003, 50.0001, 10.0004, 50.0002)},
            {'id': 'unknown', 'height': None, 'num_floors': None, 'geometry': box(10.0005, 50.0001, 10.0006, 50.0002)},
            {'id': 'empty_cell', 'height': None, 'num_floors': None, 'geometry': box(10.0021, 50.0001, 10.0022, 50.0002)}]
    return overture.assign_heights(gpd.GeoDataFrame(rows, crs='EPSG:4326'))


class GhsHeightTests(unittest.TestCase):
    def window(self):
        # Two 0.001-degree cells side by side: 14 m average height, then no buildings (0).
        return np.array([[14.0, 0.0]]), from_origin(10.0, 50.001, 0.001, 0.001)

    def test_fills_only_assumed_heights(self):
        b = ghsl.fill_heights(buildings(), (10, 50, 10.002, 50.001), window=self.window(), log=lambda *_: None)
        got = dict(zip(b['id'], zip(b['height_used_m'], b['height_source'])))
        self.assertEqual(got['known'], (22.0, 'overture_height'))
        self.assertEqual(got['floors'][0], 12.0)
        self.assertEqual(got['unknown'], (14.0, ghsl.SOURCE))
        self.assertEqual(got['empty_cell'][1], 'assumed_5m')  # no GHS value there: default kept

    def test_unavailable_layer_keeps_defaults(self):
        with mock.patch.object(ghsl, 'read_window', return_value=None):
            b = ghsl.fill_heights(buildings(), (10, 50, 10.002, 50.001), log=lambda *_: None)
        self.assertEqual(int((b['height_source'] == 'assumed_5m').sum()), 2)

    def test_no_buildings(self):
        self.assertIsNone(ghsl.fill_heights(None, (0, 0, 1, 1)))


class TerrainSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'terrain.tif'

    def fake(self, path):
        def f(bbox, out_tif, log=print):
            Path(out_tif).write_bytes(b'x')
            return out_tif
        return f if path else (lambda *a, **k: None)

    def test_auto_prefers_3dep_then_gedtm30(self):
        with mock.patch.object(terrain, 'fetch_3dep', self.fake(True)), \
                mock.patch.object(terrain, 'fetch_gedtm30', self.fake(True)) as g:
            self.assertEqual(terrain.fetch((0, 0, 1, 1), self.out, log=lambda *_: None)[1], 'usgs_3dep_13')
        self.out.unlink()
        self.out.with_suffix('.source.txt').unlink()
        with mock.patch.object(terrain, 'fetch_3dep', self.fake(False)), \
                mock.patch.object(terrain, 'fetch_gedtm30', self.fake(True)):
            self.assertEqual(terrain.fetch((0, 0, 1, 1), self.out, log=lambda *_: None)[1], 'gedtm30_v1.2')
        # A second call reuses the clip and remembers its source.
        self.assertEqual(terrain.fetch((0, 0, 1, 1), self.out)[1], 'gedtm30_v1.2')
        del g

    def test_no_data_anywhere_is_flat(self):
        with mock.patch.object(terrain, 'fetch_3dep', self.fake(False)), \
                mock.patch.object(terrain, 'fetch_gedtm30', self.fake(False)):
            self.assertEqual(terrain.fetch((0, 0, 1, 1), self.out, log=lambda *_: None), (None, None))

    def test_explicit_source(self):
        called = []
        with mock.patch.object(terrain, 'fetch_3dep', lambda *a, **k: called.append('3dep')), \
                mock.patch.object(terrain, 'fetch_gedtm30', self.fake(True)):
            self.assertEqual(terrain.fetch((0, 0, 1, 1), self.out, source='gedtm30')[1], 'gedtm30_v1.2')
        self.assertEqual(called, [])
        with self.assertRaises(ValueError):
            terrain.fetch((0, 0, 1, 1), self.out, source='srtm')


if __name__ == '__main__':
    unittest.main()
