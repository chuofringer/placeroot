"""Issue #13: is X within N meters of Y."""

from placeroot import overture, server

from .conftest import CENTER_LAT, CENTER_LON


def test_within_true_when_something_matches_inside_max_distance():
    result = overture.within_distance(CENTER_LAT, CENTER_LON, max_distance_m=1000)
    assert result["within"] is True
    assert result["nearest"] is not None
    assert result["nearest"]["id"]
    assert result["distance_m"] == result["nearest"]["distance_m"]


def test_within_false_when_nearest_match_is_farther_than_max_distance():
    # "Far Away Place" is ~5000m out. max_distance_m=3000 puts the search
    # window (2x = 6000m) wide enough to still find it, but short of it
    # counting as "within".
    result = overture.within_distance(
        CENTER_LAT, CENTER_LON, max_distance_m=3000, name="Far Away Place"
    )
    assert result["within"] is False
    assert result["nearest"] is not None
    assert result["distance_m"] > 3000


def test_nearest_none_when_nothing_within_search_window():
    """Search window is capped at max_distance_m * 2 — nothing matching
    that name exists at all, at any distance."""
    result = overture.within_distance(
        CENTER_LAT, CENTER_LON, max_distance_m=10, name="Nonexistent Place XYZ"
    )
    assert result == {"within": False, "nearest": None, "distance_m": None}


def test_category_filter_narrows_the_match():
    result = overture.within_distance(
        CENTER_LAT, CENTER_LON, max_distance_m=1000, category="coffee_shop"
    )
    assert result["nearest"]["basic_category"] == "coffee_shop"


def test_server_happy_path():
    result = server.within_distance(CENTER_LAT, CENTER_LON, max_distance_m=1000)
    assert "error" not in result
    assert result["within"] is True


def test_server_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    result = server.within_distance(CENTER_LAT, CENTER_LON, max_distance_m=1000)
    assert result["error"] == "upstream_unavailable"
