"""Opt-in test against real Overture S3 data. Excluded by default (see
pyproject.toml addopts); run explicitly with `uv run pytest -m live`.
"""

import pytest

from placeroot import overture, routing


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


@pytest.mark.live
def test_isochrone_against_real_overture_transportation_data():
    """Pike Place Market, Seattle: don't over-assert on shape, just that
    routing works end-to-end against a real transportation-theme extract.

    Note: nearest-node snapping can land on an isolated pedestrian-path
    fragment (a real gap in Overture's sidewalk connectivity, not a bug in
    this code) — pick a point known to sit on the connected street network
    rather than an arbitrary coordinate.
    """
    result = routing.isochrone(47.6097, -122.3422, minutes=10)
    assert result["stats"]["reachable_nodes"] > 1
    assert result["polygon"]["type"] == "Polygon"
    assert len(result["polygon"]["coordinates"][0]) >= 4
