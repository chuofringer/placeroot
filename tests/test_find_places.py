from placeroot import overture, server

from ._geo import haversine_m
from .conftest import CENTER_LAT, CENTER_LON, raw_rows


def test_operating_status_is_relabelled():
    """operating_status is surfaced as business-lifecycle language, never the
    raw Overture "open" (which reads as "open right now" — we have no hours)."""
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    surfaced = {r["operating_status"] for r in results}
    assert "open" not in surfaced
    assert surfaced <= {"in business", "permanently closed", "temporarily closed", None}


def test_label_operating_status_maps_and_passes_through():
    assert overture._label_operating_status("open") == "in business"
    assert overture._label_operating_status("closed") == "permanently closed"
    assert overture._label_operating_status("closed_temporarily") == "temporarily closed"
    assert overture._label_operating_status(None) is None
    # Unrecognised values pass through unchanged — never misrepresent the source.
    assert overture._label_operating_status("some_future_value") == "some_future_value"


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


def test_ids_present_and_stable_across_identical_queries():
    """Regression for #25: every result carries a GERS id, and it's stable
    across two identical queries — an agent can hold onto it across turns."""
    first = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    second = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    assert all(r["id"] for r in first)
    assert [r["id"] for r in first] == [r["id"] for r in second]


def test_ids_match_ground_truth():
    expected = {row["id"] for row in raw_rows()}
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert all(r["id"] in expected for r in results)


def test_bogus_category_gets_hint_note():
    """Regression for #117: a wrong/invalid Overture category slug should
    not look identical to "this area genuinely has none of that category" —
    the server tool adds a non-fatal note pointing at search_categories."""
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="definitely_not_a_category_xyz", limit=10
    )
    assert result["results"] == []
    assert "note" in result
    assert "search_categories" in result["note"]


def test_valid_category_with_matches_has_no_note():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="coffee_shop", limit=25
    )
    assert result["results"]
    assert "note" not in result


def test_no_category_with_matches_has_no_note():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert result["results"]
    assert "note" not in result


def test_no_category_empty_area_has_no_note():
    """The hint is scoped to the category-filter case only — an empty area
    with no category filter at all is a plain empty result, not a hint."""
    result = server.find_places(0.0, 0.0, radius_m=1000, limit=10)
    assert result["results"] == []
    assert "note" not in result
