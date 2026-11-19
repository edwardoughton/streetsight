"""streetsight: find the Street View panorama most likely to show a target, and verify it.

    import streetsight
    result = streetsight.find_view(41.288927, -88.217081, target='cell tower', site_id='IL_tower')
    summary = streetsight.find_views('sites.csv', n_candidates=5)
"""
from .batch import find_views
from .pipeline import FindViewResult, find_view

__version__ = '0.2.0'
__all__ = ['find_view', 'find_views', 'FindViewResult']
