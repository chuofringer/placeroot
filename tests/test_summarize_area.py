from placeroot import geocode, overture, server

from ._geo import haversine_m
from .conftest import CENTER_LAT, CENTER_LON, raw_rows
from .test_gers_lookup import PLACE_ID


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


def test_uncategorized_zero_in_fully_categorized_area():
    """A non-empty area with no NULL-category places must report 0, not None."""
    result = overture.summarize_area(40.698570097831244, -73.90215789910143, 100)
    assert result["total_places"] > 0
    assert result["uncategorized_count"] == 0


# --- LocationRef (roadmap #4.1): `where`, mutually exclusive with lat/lon ---


def test_summarize_area_needs_lat_lon_or_where():
    result = server.summarize_area()
    assert result["error"] == "bad_request"


def test_summarize_area_rejects_both_latlon_and_where():
    result = server.summarize_area(
        lat=CENTER_LAT, lon=CENTER_LON, where={"lat": CENTER_LAT, "lon": CENTER_LON}
    )
    assert result["error"] == "bad_request"


def test_summarize_area_plain_latlon_has_no_resolved_key():
    result = server.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert "error" not in result
    assert "resolved" not in result


def test_summarize_area_where_coordinate_dict_has_no_resolved_key():
    result = server.summarize_area(where={"lat": CENTER_LAT, "lon": CENTER_LON}, radius_m=1000)
    assert "error" not in result
    assert "resolved" not in result


def test_summarize_area_where_gers_id_adds_resolved():
    """Real fixture GERS id (see tests/test_gers_lookup.py) — no monkeypatch."""
    result = server.summarize_area(where=PLACE_ID, radius_m=1000)
    assert "error" not in result
    assert result["resolved"]["matched_by"] == "gers_id"
    assert result["resolved"]["id"] == PLACE_ID


def test_summarize_area_where_name_adds_resolved(monkeypatch):
    def fake_resolve(query):
        assert query == "Cluster Place 000"
        return {
            "name": query, "lat": CENTER_LAT, "lon": CENTER_LON,
            "id": "gers-cluster", "type": "place",
        }

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.summarize_area(where="Cluster Place 000", radius_m=1000)
    assert "error" not in result
    assert result["resolved"] == {
        "name": "Cluster Place 000", "id": "gers-cluster",
        "lat": CENTER_LAT, "lon": CENTER_LON, "matched_by": "name",
    }


def test_summarize_area_where_bad_ref_is_bad_request():
    result = server.summarize_area(where={"lat": 91.0, "lon": 0.0})
    assert result["error"] == "bad_request"
