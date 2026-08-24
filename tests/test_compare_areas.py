"""Issue #12: 2-5 areas side by side."""

import pytest

from placeroot import errors, geocode, overture, server

from .conftest import CENTER_LAT, CENTER_LON


def test_two_areas_have_aligned_category_counts():
    result = overture.compare_areas([(CENTER_LAT, CENTER_LON), (78.0, 15.0)], radius_m=1000)
    assert len(result["areas"]) == 2
    keys_a = set(result["areas"][0]["category_counts"])
    keys_b = set(result["areas"][1]["category_counts"])
    assert keys_a == keys_b == set(result["categories"])
    assert len(result["categories"]) <= 10


def test_density_scales_with_place_count():
    result = overture.compare_areas([(CENTER_LAT, CENTER_LON), (78.0, 15.0)], radius_m=1000)
    dense, sparse = result["areas"]
    assert dense["total_places"] > sparse["total_places"]
    assert dense["density_per_km2"] > sparse["density_per_km2"]


def test_differentiators_flag_the_biggest_relative_gap():
    result = overture.compare_areas([(CENTER_LAT, CENTER_LON), (78.0, 15.0)], radius_m=1000)
    assert result["differentiators"]
    top = result["differentiators"][0]
    # bank is the only category present in both areas; everything else in
    # the dense NYC cluster is absent from the sparse Arctic cluster, so
    # several categories tie at relative_difference == 1.0.
    assert top["relative_difference"] == 1.0
    assert top["category"] != "bank"


def test_empty_areas_are_a_valid_comparison():
    result = overture.compare_areas([(0.0, 0.0), (0.1, 0.1)], radius_m=1000)
    assert all(a["total_places"] == 0 for a in result["areas"])
    assert result["categories"] == []
    assert result["differentiators"] == []


def test_rejects_fewer_than_two_areas():
    with pytest.raises(ValueError):
        overture.compare_areas([(CENTER_LAT, CENTER_LON)])


def test_rejects_more_than_five_areas():
    with pytest.raises(ValueError):
        overture.compare_areas([(CENTER_LAT, CENTER_LON)] * 6)


def test_server_happy_path():
    result = server.compare_areas(
        [{"lat": CENTER_LAT, "lon": CENTER_LON}, {"lat": 78.0, "lon": 15.0}], radius_m=1000
    )
    assert "error" not in result
    assert len(result["areas"]) == 2


def test_server_bad_request_on_wrong_area_count():
    result = server.compare_areas([{"lat": CENTER_LAT, "lon": CENTER_LON}])
    assert result["error"] == "bad_request"


def test_server_bad_request_on_malformed_area():
    result = server.compare_areas([{"lat": CENTER_LAT}, {"lat": 78.0, "lon": 15.0}])
    assert result["error"] == "bad_request"


# --- LocationRef (roadmap #4.1) -------------------------------------------


def test_coordinate_only_areas_have_no_resolved_key():
    result = server.compare_areas(
        [{"lat": CENTER_LAT, "lon": CENTER_LON}, {"lat": 78.0, "lon": 15.0}], radius_m=1000
    )
    assert "error" not in result
    assert "resolved" not in result


def test_mixed_areas_list_with_a_named_area(monkeypatch):
    def fake_resolve(query):
        assert query == "Arctic Base"
        return {"name": query, "lat": 78.0, "lon": 15.0, "id": "gers-arctic", "type": "place"}

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.compare_areas(
        [{"lat": CENTER_LAT, "lon": CENTER_LON}, "Arctic Base"], radius_m=1000
    )
    assert "error" not in result
    assert result["resolved"] == [
        {
            "index": 1, "name": "Arctic Base", "id": "gers-arctic",
            "lat": 78.0, "lon": 15.0, "matched_by": "name",
        }
    ]


def test_ambiguous_area_name_is_an_indexed_error(monkeypatch):
    def fake_resolve(query):
        raise errors.AmbiguousPlace(query, candidates=[{"name": "X"}])

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.compare_areas(["Ambiguous", {"lat": 78.0, "lon": 15.0}], radius_m=1000)
    assert result["error"] == "ambiguous_place"
    assert result["index"] == 0
    assert result["detail"].startswith("areas[0]: ")


def test_server_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    result = server.compare_areas(
        [{"lat": CENTER_LAT, "lon": CENTER_LON}, {"lat": 78.0, "lon": 15.0}]
    )
    assert result["error"] == "upstream_unavailable"


def test_server_applies_budget_to_differentiators(monkeypatch):
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "10")
    result = server.compare_areas(
        [{"lat": CENTER_LAT, "lon": CENTER_LON}, {"lat": 78.0, "lon": 15.0}]
    )
    assert result.get("truncated") is True
