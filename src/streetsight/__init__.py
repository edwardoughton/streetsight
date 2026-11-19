"""streetsight: find a street-level image (Mapillary, then Google Street View) that shows a target.

    import streetsight
    result = streetsight.find_view(41.288927, -88.217081, target_height_m=40, target='water tower')
    summary = streetsight.find_views('sites.csv', n_candidates=5)
"""
from .batch import find_views
from .pipeline import FindViewResult, find_view

__version__ = '0.3.0'
__all__ = ['find_view', 'find_views', 'FindViewResult']
