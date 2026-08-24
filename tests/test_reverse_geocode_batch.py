"""Tests for reverse_geocode_batch (#125): batch many points into one call.

Mirrors the geocode_batch test shape (per-row error union, whole-call
upstream abort, 20-point cap, empty-list short circuit) against
reverse_geocode's fixtures.
"""

from placeroot import geocode as geocoding
from placeroot import overture, server

from .conftest import CENTER_LAT, CENTER_LON


def test_happy_path_resolves_each_point_in_order():
    points = [
        {"lat": CENTER_LAT, "lon": CENTER_LON},
        {"lat": 0.0, "lon": 0.0},
    ]
    result = server.reverse_geocode_batch(points)
    assert "results" in result
    rows = result["results"]
    assert len(rows) == 2
    assert rows[0]["source"] == "address"
    assert rows[0]["admin_context"][-1] == "Brooklyn"
    assert rows[1]["source"] == "divisions_only"
    assert rows[1]["admin_context"] == []


def test_malformed_point_gets_a_per_row_error_without_failing_the_batch():
    points = [
        {"lat": CENTER_LAT, "lon": CENTER_LON},
        {"lon": CENTER_LON},  # missing lat
    ]
    result = server.reverse_geocode_batch(points)
    rows = result["results"]
    assert len(rows) == 2
    assert rows[0]["source"] == "address"
    assert rows[1]["error"] == "bad_request"
    assert rows[1]["detail"]
    assert rows[1]["lat"] is None
    assert rows[1]["lon"] == CENTER_LON


def test_over_cap_returns_bad_request():
    points = [{"lat": CENTER_LAT, "lon": CENTER_LON}] * 21
    result = server.reverse_geocode_batch(points)
    assert result["error"] == "bad_request"
    assert "results" not in result


def test_empty_list_returns_empty_results():
    result = server.reverse_geocode_batch([])
    assert result == {"results": []}


def test_upstream_unavailable_aborts_the_whole_batch(monkeypatch):
    def _boom(lat, lon):
        raise overture.UpstreamUnavailable("boom")

    monkeypatch.setattr(geocoding, "reverse_geocode", _boom)
    result = server.reverse_geocode_batch([{"lat": CENTER_LAT, "lon": CENTER_LON}])
    assert result["error"] == "upstream_unavailable"
    assert result["retry_advised"] is True
