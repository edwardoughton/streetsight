"""FCC Antenna Structure Registration (ASR) lookup: nearest registered structure and its height.

Downloads the public weekly ASR archive (~38 MB), parses RA (registration) and CO (coordinates)
records once into a cached Parquet table, then answers nearest-structure queries.
Field positions were checked against registration 1202772 (Braidwood IL, 79.2 m overall AGL).
FCC coordinates are NAD83, treated here as WGS84: fine for screening (sub-metre in CONUS).
Registration does not prove cellular use, and many shorter cell towers are not registered.
"""
import io
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ASR_URL = 'https://data.fcc.gov/download/pub/uls/complete/r_tower.zip'
STATUS = {'C': 'constructed'}  # other codes are passed through raw; see the FCC patower schema


def _dms(deg, mins, secs, hemi):
    v = pd.to_numeric(deg, errors='coerce') + pd.to_numeric(mins, errors='coerce') / 60 + \
        pd.to_numeric(secs, errors='coerce') / 3600
    return v.where(~hemi.isin(['S', 'W']), -v)


def load_asr(cache_dir, max_age_days=30, log=print):
    cache_dir = Path(cache_dir) / 'fcc'
    table = cache_dir / 'fcc_asr.parquet'
    if table.is_file() and time.time() - table.stat().st_mtime < max_age_days * 86400:
        return pd.read_parquet(table)
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / 'r_tower.zip'
    if not archive.is_file() or time.time() - archive.stat().st_mtime > max_age_days * 86400:
        log('FCC ASR: downloading weekly archive (~38 MB)...')
        resp = requests.get(ASR_URL, timeout=300)
        resp.raise_for_status()
        archive.write_bytes(resp.content)
    z = zipfile.ZipFile(archive)
    read = lambda n: pd.read_csv(io.TextIOWrapper(z.open(n), encoding='latin-1'), sep='|', header=None,
                                 dtype=str, quoting=3, keep_default_na=False)
    ra = read('RA.dat')
    ra = pd.DataFrame({'registration': ra[3], 'status_code': ra[8], 'constructed_date': ra[12],
                       'dismantled_date': ra[13], 'address': ra[23], 'city': ra[24], 'state': ra[25],
                       'height_structure_m': pd.to_numeric(ra[28], errors='coerce'),
                       'overall_height_agl_m': pd.to_numeric(ra[30], errors='coerce'),
                       'structure_type': ra[32]}).drop_duplicates('registration', keep='last')
    co = read('CO.dat')
    co = pd.DataFrame({'registration': co[3], 'coordinate_type': co[5],
                       'latitude': _dms(co[6], co[7], co[8], co[9]),
                       'longitude': _dms(co[11], co[12], co[13], co[14])})
    co = co.sort_values('coordinate_type', key=lambda s: s.ne('T')).drop_duplicates('registration')
    asr = co.merge(ra, on='registration', how='inner').dropna(subset=['latitude', 'longitude'])
    asr.to_parquet(table)
    log(f'FCC ASR: parsed {len(asr):,} structures into {table.name}')
    return asr


def nearest_structures(asr, lat, lon, max_m=100):
    """Registrations within max_m, constructed first, then nearest."""
    p1, p2 = np.radians(lat), np.radians(asr['latitude'].to_numpy())
    dl = np.radians(asr['longitude'].to_numpy() - lon)
    h = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    d = 2 * 6371008.8 * np.arcsin(np.sqrt(h))
    near = asr[d <= max_m].assign(distance_m=d[d <= max_m].round(1))
    near = near.assign(status=near['status_code'].map(STATUS).fillna(near['status_code']))
    return near.sort_values(['status_code', 'distance_m'], key=lambda s: s.ne('C') if s.name == 'status_code' else s)


def tower_height(asr, lat, lon, max_m=100):
    """(height_m, match_dict) using the best constructed match, or (None, None)."""
    near = nearest_structures(asr, lat, lon, max_m)
    near = near[near['status_code'].eq('C') & near['overall_height_agl_m'].notna()]
    if near.empty:
        return None, None
    m = near.iloc[0].to_dict()
    return float(m['overall_height_agl_m']), m
