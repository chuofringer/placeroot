"""Opt-in test against real Overture S3 data. Excluded by default (see
pyproject.toml addopts); run explicitly with `uv run pytest -m live`.
"""

import pytest

from placeroot import geocode, overture, routing


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


@pytest.mark.live
def test_resolve_place_against_real_overture_data():
    """#22 smoke test: a business name + city, with a rough location hint,
    should resolve to a place-kind candidate carrying a real GERS id.
    """
    results = geocode.resolve_place(
        "Manana coffee Austin", near_lat=30.2672, near_lon=-97.7431, limit=5
    )
    assert results
    assert any(r["kind"] == "place" and r["id"] for r in results)


@pytest.mark.live
def test_geocode_address_finds_an_exact_doorway():
    """#225: number + street + city -> the one point, live.

    "1600 Amphitheatre Parkway" is spelled "AMPHITHEATRE PKWY" in Overture's
    US address rows, so this only passes through the USPS suffix map.
    """
    result = geocode.geocode_address("1600 Amphitheatre Parkway, Mountain View")
    assert result["anchor"]["name"] == "Mountain View"
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["number"] == "1600"
    assert row["street"].upper().startswith("AMPHITHEATRE")
    assert abs(row["lat"] - 37.4224) < 0.01
    assert abs(row["lon"] + 122.0842) < 0.01


@pytest.mark.live
def test_geocode_address_dedupes_a_whole_street():
    """#225: MARKET ST is ~3,000 raw address points over ~900 distinct
    number|street pairs; the answer must be the distinct ones, capped."""
    result = geocode.geocode_address("Market Street, San Francisco", limit=5)
    assert result["anchor"]["name"] == "San Francisco"
    assert len(result["results"]) == 5
    pairs = [(r["number"], r["street"]) for r in result["results"]]
    assert len(set(pairs)) == 5
    assert result["truncated"] is True
    assert result["distinct_in_range"] > 500


@pytest.mark.live
def test_geocode_address_folds_ordinals_to_nyc_spelling():
    """Task #23's original repro: NYC writes Fifth Avenue as "5 AVENUE", so
    "350 5th Ave" only resolves through the ordinal fold."""
    result = geocode.geocode_address("350 5th Ave, New York")
    assert result["results"], result.get("note")
    assert any(r["street"].upper().startswith("5 AVE") for r in result["results"])
