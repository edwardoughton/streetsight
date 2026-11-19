import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from streetsight.lidar import height_features, is_tower_like  # noqa: E402


def cloud(extra):
    rng = np.random.default_rng(0)
    ground = np.column_stack([rng.uniform(-60, 60, 4000), rng.uniform(-60, 60, 4000), rng.normal(300, 0.1, 4000), np.full(4000, 2)])
    return np.vstack([ground, np.array(extra, dtype=float)])


def test_mast_is_tall_and_narrow():
    mast = [[20 + 0.3 * k % 2, 5, 300 + h, 1] for k, h in enumerate(range(5, 80, 3))]
    f = height_features(cloud(mast))
    assert 76 <= f['max_height_m'] <= 78 and f['top_width_m'] < 3 and is_tower_like(f)


def test_tree_crown_and_low_pad_are_not():
    rng = np.random.default_rng(1)
    crown = [[x, y, 300 + rng.uniform(15, 22), 1] for x, y in rng.uniform(-12, 12, (300, 2))]
    assert not is_tower_like(height_features(cloud(crown)))
    pad = [[x, y, 303, 6] for x, y in rng.uniform(-8, 8, (200, 2))]
    f = height_features(cloud(pad))
    assert f['max_height_m'] < 4 and not is_tower_like(f)


def test_no_points():
    assert height_features(np.empty((0, 4))) == {'lidar_points': 0}
