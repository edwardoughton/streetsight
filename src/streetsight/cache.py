"""Where streetsight keeps downloads and runs.

Data root: STREETSIGHT_DATA, else ./data (the project's git-ignored data folder).
Cache: STREETSIGHT_CACHE, else <data root>/cache. Runs: <data root>/streetsight_runs.
"""
import os
from pathlib import Path


def data_dir():
    return Path(os.environ.get('STREETSIGHT_DATA') or 'data')


def default_cache_dir():
    return Path(os.environ['STREETSIGHT_CACHE']) if os.environ.get('STREETSIGHT_CACHE') else data_dir() / 'cache'


def default_runs_dir():
    return data_dir() / 'streetsight_runs'
