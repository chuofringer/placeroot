"""Issue #12: 2-5 areas side by side."""

import pytest

from placeroot import overture, server

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
