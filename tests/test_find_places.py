from placeroot import overture

from ._geo import haversine_m
from .conftest import CENTER_LAT, CENTER_LON, raw_rows


def test_limit_fill():
    """Regression for #1: a dense area with more than `limit` matches
    returns exactly `limit` rows, not fewer."""
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    assert len(results) == 10


def test_circle_excludes_bbox_corner():
    """Regression for #3: a place inside the square prefilter but outside
    the true circle must not appear, and a place just inside the circle
    must appear."""
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=500, category="novelty_shop", limit=10
    )
    names = {r["name"] for r in results}
    assert "Corner Test Place" not in names
    assert "Edge Test Place" in names


def test_distances_are_within_radius():
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=500, limit=25)
    assert results
    for r in results:
        assert r["distance_m"] <= 500


def test_results_sorted_nearest_first():
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    distances = [r["distance_m"] for r in results]
    assert distances == sorted(distances)


def test_category_filter_matches_ground_truth():
    radius_m = 1000
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=radius_m, category="coffee_shop", limit=25
    )
    expected = [
        row for row in raw_rows()
        if row["basic_category"] == "coffee_shop"
        and haversine_m(CENTER_LAT, CENTER_LON, row["lat"], row["lon"]) <= radius_m
    ]
    assert results
    assert len(results) == len(expected)
    assert all(r["basic_category"] == "coffee_shop" for r in results)


def test_name_filter():
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, name="Roastery", limit=25)
    assert len(results) == 1
    assert results[0]["name"] == "Blue Bottle Roastery"


def test_empty_result_far_from_any_fixture_data():
    results = overture.find_places(0.0, 0.0, radius_m=1000, limit=10)
    assert results == []


def test_high_latitude_cluster_is_findable():
    results = overture.find_places(78.0, 15.0, radius_m=1000, limit=25)
    assert len(results) == 5
    assert all(r["distance_m"] <= 1000 for r in results)
