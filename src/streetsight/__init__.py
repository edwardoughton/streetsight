"""streetsight: download the street-level images most likely to show a tall structure.

    import streetsight
    result = streetsight.find_view(42.025504, -93.646039, 34)    # lat, lon, height in metres
    result.images                                                # downloaded views, best first
    summary = streetsight.find_views('sites.csv', n_images=3)
"""
from .batch import find_views
from .pipeline import ViewResult, find_view

__version__ = '0.3.0'
__all__ = ['find_view', 'find_views', 'ViewResult']
