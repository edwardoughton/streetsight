"""Where streetsight keeps downloads and runs.

Cache (Overture downloads): STREETSIGHT_CACHE, else the per-user cache folder
(e.g. ~/.cache/streetsight on Linux, %LOCALAPPDATA%\\streetsight\\Cache on Windows).
Runs: STREETSIGHT_RUNS, else ./streetsight_runs in the current folder.

STREETSIGHT_DATA=<folder> keeps both in one project folder instead: <folder>/cache and
<folder>/streetsight_runs (the layout used before version 0.3).
"""
import os
from pathlib import Path

import platformdirs


def _data_dir():
    return Path(os.environ['STREETSIGHT_DATA']) if os.environ.get('STREETSIGHT_DATA') else None


def default_cache_dir():
    if os.environ.get('STREETSIGHT_CACHE'):
        return Path(os.environ['STREETSIGHT_CACHE'])
    data = _data_dir()
    return data / 'cache' if data else Path(platformdirs.user_cache_dir('streetsight', appauthor=False))


def default_runs_dir():
    if os.environ.get('STREETSIGHT_RUNS'):
        return Path(os.environ['STREETSIGHT_RUNS'])
    data = _data_dir()
    return data / 'streetsight_runs' if data else Path('streetsight_runs')
