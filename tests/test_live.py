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
        assert r["id"]  # GERS id (#25), real Overture data always carries one


@pytest.mark.live
def test_place_details_against_real_overture_data():
    hits = overture.find_places(47.6062, -122.3321, radius_m=500, limit=1)
    assert hits
    result = overture.place_details(id=hits[0]["id"])
    assert result is not None
    assert result["id"] == hits[0]["id"]


@pytest.mark.live
def test_within_distance_against_real_overture_data():
    result = overture.within_distance(47.6062, -122.3321, max_distance_m=500)
    assert isinstance(result["within"], bool)


@pytest.mark.live
def test_admin_lookup_against_real_overture_data():
    from placeroot import divisions

    result = divisions.admin_lookup(47.6062, -122.3321)
    assert isinstance(result["chain"], list)
