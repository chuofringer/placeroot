"""Issue #13: is X within N meters of Y."""

import pytest

from placeroot import geocode, overture, server

from .conftest import CENTER_LAT, CENTER_LON
from .test_gers_lookup import PLACE_ID


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


# --- LocationRef (roadmap #4.1): `where`, mutually exclusive with lat/lon ---


def test_needs_lat_lon_or_where():
    result = server.within_distance(max_distance_m=1000)
    assert result["error"] == "bad_request"


def test_rejects_both_latlon_and_where():
    result = server.within_distance(
        lat=CENTER_LAT, lon=CENTER_LON, where={"lat": CENTER_LAT, "lon": CENTER_LON},
        max_distance_m=1000,
    )
    assert result["error"] == "bad_request"


def test_plain_latlon_has_no_resolved_key():
    result = server.within_distance(CENTER_LAT, CENTER_LON, max_distance_m=1000)
    assert "error" not in result
    assert "resolved" not in result


def test_where_coordinate_dict_has_no_resolved_key():
    result = server.within_distance(
        where={"lat": CENTER_LAT, "lon": CENTER_LON}, max_distance_m=1000
    )
    assert "error" not in result
    assert "resolved" not in result


def test_where_gers_id_adds_resolved():
    """Real fixture GERS id (see tests/test_gers_lookup.py) — no monkeypatch."""
    result = server.within_distance(where=PLACE_ID, max_distance_m=1000)
    assert "error" not in result
    assert result["resolved"]["matched_by"] == "gers_id"
    assert result["resolved"]["id"] == PLACE_ID


def test_where_name_adds_resolved(monkeypatch):
    def fake_resolve(query):
        assert query == "Cluster Place 000"
        return {
            "name": query, "lat": CENTER_LAT, "lon": CENTER_LON,
            "id": "gers-cluster", "type": "place",
        }

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.within_distance(where="Cluster Place 000", max_distance_m=1000)
    assert "error" not in result
    assert result["resolved"] == {
        "name": "Cluster Place 000", "id": "gers-cluster",
        "lat": CENTER_LAT, "lon": CENTER_LON, "matched_by": "name",
    }


def test_where_bad_ref_is_bad_request():
    result = server.within_distance(where={"lat": 91.0, "lon": 0.0}, max_distance_m=1000)
    assert result["error"] == "bad_request"


# --- max_distance_m: required, keyword-only, must be a positive number ----
#
# Issue: making lat/lon optional (for `where`) forced max_distance_m to
# gain a Python default (`= 0`) to satisfy argument ordering — silently
# turning an omitted max_distance_m into a 0m search window and a
# confident-looking {"within": False} instead of a loud failure. Fixed by
# making max_distance_m keyword-only (no default, so it stays required in
# both Python and the published schema — see the `required` list in
# tools/list) plus an explicit runtime check, belt-and-braces.


def test_omitted_max_distance_m_is_a_missing_argument_error():
    """Keyword-only with no default: Python itself refuses the call before
    this tool's body ever runs — the schema (see `required` above) refuses
    it identically for an MCP caller."""
    with pytest.raises(TypeError):
        server.within_distance(CENTER_LAT, CENTER_LON)


def test_zero_max_distance_m_is_a_bad_request():
    result = server.within_distance(CENTER_LAT, CENTER_LON, max_distance_m=0)
    assert result["error"] == "bad_request"
    assert "max_distance_m" in result["detail"]


def test_negative_max_distance_m_is_a_bad_request():
    result = server.within_distance(CENTER_LAT, CENTER_LON, max_distance_m=-500)
    assert result["error"] == "bad_request"
    assert "max_distance_m" in result["detail"]


def test_non_numeric_max_distance_m_is_a_bad_request():
    result = server.within_distance(CENTER_LAT, CENTER_LON, max_distance_m="far")
    assert result["error"] == "bad_request"
    assert "max_distance_m" in result["detail"]


def test_non_finite_max_distance_m_is_a_bad_request():
    result = server.within_distance(CENTER_LAT, CENTER_LON, max_distance_m=float("inf"))
    assert result["error"] == "bad_request"
    assert "max_distance_m" in result["detail"]
