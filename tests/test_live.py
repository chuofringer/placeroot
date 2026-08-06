"""Opt-in test against real Overture S3 data. Excluded by default (see
pyproject.toml addopts); run explicitly with `uv run pytest -m live`.
"""

import pytest

from placeroot import overture


@pytest.mark.live
def test_find_places_against_real_overture_data():
    results = overture.find_places(47.6062, -122.3321, radius_m=500, limit=5)
    assert isinstance(results, list)
    assert len(results) <= 5
    for r in results:
        assert r["distance_m"] <= 500
