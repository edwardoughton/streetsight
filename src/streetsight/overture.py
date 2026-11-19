"""Overture Maps buildings and road segments for a bounding box, read anonymously from public S3.

Uses pyarrow's dataset filter on Overture's per-feature bbox column, so only matching row groups
are downloaded. A first query takes ~20-60 s; results are cached as GeoParquet.
Licences: buildings and transportation are ODbL (OpenStreetMap-derived) plus other open sources.
"""
import hashlib
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import wkb

BUCKET = 'overturemaps-us-west-2'
FLOOR_HEIGHT_M = 3.0
DEFAULT_BUILDING_HEIGHT_M = 5.0
NON_VEHICLE_ROADS = {'steps'}


def _s3():
    from pyarrow import fs
    return fs.S3FileSystem(anonymous=True, region='us-west-2')


def latest_release():
    from pyarrow import fs
    infos = _s3().get_file_info(fs.FileSelector(f'{BUCKET}/release/', recursive=False))
    return sorted(i.base_name for i in infos)[-1]


_FRAGMENTS = {}
_LOCK = __import__('threading').Lock()


def _fragments(theme, ftype, release, log):
    """Parquet fragments with footers (row-group bbox statistics) loaded once per process.

    The first call reads every file footer (about a minute); later queries in the same process
    only download the row groups whose statistics intersect the query box.
    """
    import pyarrow.dataset as ds
    k = (theme, ftype, release)
    with _LOCK:
        if k not in _FRAGMENTS:
            t = time.time()
            d = ds.dataset(f'{BUCKET}/release/{release}/theme={theme}/type={ftype}/', filesystem=_s3(),
                           format='parquet')
            frags = list(d.get_fragments())
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(16) as ex:
                list(ex.map(lambda f: f.ensure_complete_metadata(), frags))
            _FRAGMENTS[k] = (d.schema, frags)
            log(f'Overture {ftype}: indexed {len(frags)} files in {time.time() - t:.0f}s (reused for later queries)')
    return _FRAGMENTS[k]


def fetch(theme, ftype, bbox, columns, cache_dir, release=None, log=print):
    """GeoDataFrame (EPSG:4326) of Overture features whose bbox intersects bbox=(west, south, east, north)."""
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    release = release or latest_release()
    west, south, east, north = bbox
    key = hashlib.sha1(f'{release}|{ftype}|{west:.6f}|{south:.6f}|{east:.6f}|{north:.6f}'.encode()).hexdigest()[:16]
    cache = Path(cache_dir) / 'overture' / f'{ftype}_{release}_{key}.parquet'
    if cache.is_file():
        gdf = gpd.read_parquet(cache)
        log(f'Overture {ftype}: {len(gdf)} features (cached)')
        return gdf
    t = time.time()
    log(f'Overture {ftype}: querying release {release}...')
    schema, frags = _fragments(theme, ftype, release, log)
    b = lambda k: pc.field('bbox', k)
    flt = (b('xmin') < east) & (b('xmax') > west) & (b('ymin') < north) & (b('ymax') > south)
    parts = [f.subset(filter=flt) for f in frags]
    parts = [f for f in parts if f.num_row_groups]
    cols = [*columns, 'sources', 'geometry']
    if parts:
        df = ds.FileSystemDataset(parts, schema, ds.ParquetFileFormat(), filesystem=_s3())             .to_table(filter=flt, columns=cols).to_pandas()
    else:
        df = schema.empty_table().select(cols).to_pandas()
    df['source_dataset'] = df.pop('sources').map(
        lambda s: ';'.join(sorted({x['dataset'] for x in s})) if s is not None and len(s) else '')
    gdf = gpd.GeoDataFrame(df, geometry=[wkb.loads(g) for g in df.pop('geometry')], crs='EPSG:4326')
    gdf['overture_release'] = release
    cache.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(cache)
    log(f'Overture {ftype}: {len(gdf)} features in {time.time() - t:.0f}s')
    return gdf


def buildings(bbox, cache_dir, release=None, log=print):
    gdf = fetch('buildings', 'building', bbox, ['id', 'height', 'num_floors', 'class', 'subtype'],
                cache_dir, release, log)
    return assign_heights(gdf)


def roads(bbox, cache_dir, release=None, log=print):
    gdf = fetch('transportation', 'segment', bbox, ['id', 'subtype', 'class'], cache_dir, release, log)
    return gdf[(gdf['subtype'] == 'road') & ~gdf['class'].isin(NON_VEHICLE_ROADS)].reset_index(drop=True)


def assign_heights(gdf, floor_h=FLOOR_HEIGHT_M, default_h=DEFAULT_BUILDING_HEIGHT_M):
    h = pd.to_numeric(gdf.get('height'), errors='coerce')
    floors = pd.to_numeric(gdf.get('num_floors'), errors='coerce')
    gdf['height_used_m'] = h.fillna(floors * floor_h).fillna(default_h).round(2)
    gdf['height_source'] = 'overture_height'
    gdf.loc[h.isna() & floors.notna(), 'height_source'] = f'num_floors_x{floor_h:g}m'
    gdf.loc[h.isna() & floors.isna(), 'height_source'] = f'assumed_{default_h:g}m'
    return gdf
