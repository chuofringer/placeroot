from placeroot import overture

from ._geo import haversine_m
from .conftest import CENTER_LAT, CENTER_LON, raw_rows


def _within(radius_m):
    return [
        row for row in raw_rows()
        if haversine_m(CENTER_LAT, CENTER_LON, row["lat"], row["lon"]) <= radius_m
    ]


def test_total_places_matches_direct_count():
    """Regression for #2: total_places must equal a direct count(*), not
    just the sum of the top-25 categories."""
    radius_m = 1000
    result = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m)
    expected = _within(radius_m)
    assert result["total_places"] == len(expected)


def test_uncategorized_count():
    radius_m = 1000
    result = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m)
    expected_uncategorized = [row for row in _within(radius_m) if row["basic_category"] is None]
    assert result["uncategorized_count"] == len(expected_uncategorized)


def test_top_categories_plus_other_plus_uncategorized_equals_total():
    result = overture.summarize_area(CENTER_LAT, CENTER_LON, 1000)
    top_sum = sum(c["count"] for c in result["top_categories"])
    reconstructed = top_sum + result["other_categories_count"] + result["uncategorized_count"]
    assert reconstructed == result["total_places"]


def test_circle_vs_square_agrees_with_find_places():
    """Regression for #3: summarize_area and find_places must agree on
    membership — the corner test place must be excluded from both."""
    radius_m = 500
    result = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m)
    expected = len(_within(radius_m))
    assert result["total_places"] == expected


def test_empty_area():
    result = overture.summarize_area(0.0, 0.0, radius_m=1000)
    assert result["total_places"] == 0
    assert result["top_categories"] == []
    assert result["uncategorized_count"] == 0
    assert result["other_categories_count"] == 0
