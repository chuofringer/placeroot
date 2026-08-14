"""Tests for the distance_matrix tool: caps, validation, and haversine correctness.

Correctness is checked against tests/_geo.haversine_m — an independent
implementation from the one distance_matrix actually uses (placeroot.geo),
so this isn't just checking the code against itself.
"""

from placeroot import server
from tests._geo import haversine_m as oracle_haversine_m

ORIGINS = [
    {"lat": 30.2672, "lon": -97.7431},  # Austin
    {"lat": 40.7128, "lon": -74.0060},  # NYC
]

DESTINATIONS = [
    {"lat": 29.7604, "lon": -95.3698},  # Houston
    {"lat": 34.0522, "lon": -118.2437},  # LA
    {"lat": 41.8781, "lon": -87.6298},  # Chicago
]


def test_distance_matrix_matches_independent_oracle_and_ordering():
    result = server.distance_matrix(origins=ORIGINS, destinations=DESTINATIONS)
    elements = result["elements"]
    assert len(elements) == 6

    expected_order = [(oi, di) for oi in range(2) for di in range(3)]
    assert [(e["origin_idx"], e["dest_idx"]) for e in elements] == expected_order

    for e in elements:
        o = ORIGINS[e["origin_idx"]]
        d = DESTINATIONS[e["dest_idx"]]
        expected = round(oracle_haversine_m(o["lat"], o["lon"], d["lat"], d["lon"]))
        assert abs(e["distance_m"] - expected) <= 1


def test_distance_matrix_self_distance_is_zero():
    point = {"lat": 30.2672, "lon": -97.7431}
    result = server.distance_matrix(origins=[point], destinations=[point])
    assert result["elements"] == [{"origin_idx": 0, "dest_idx": 0, "distance_m": 0}]


def test_distance_matrix_over_cap_origins():
    origins = [{"lat": 0.0, "lon": 0.0}] * 11
    result = server.distance_matrix(origins=origins, destinations=[{"lat": 0.0, "lon": 0.0}])
    assert result["error"] == "bad_request"
    assert "elements" not in result
    assert result["detail"] == "origins accepts at most 10 points, got 11"


def test_distance_matrix_over_cap_destinations():
    destinations = [{"lat": 0.0, "lon": 0.0}] * 11
    result = server.distance_matrix(origins=[{"lat": 0.0, "lon": 0.0}], destinations=destinations)
    assert result["error"] == "bad_request"
    assert "elements" not in result
    assert result["detail"] == "destinations accepts at most 10 points, got 11"


def test_distance_matrix_over_cap_both():
    origins = [{"lat": 0.0, "lon": 0.0}] * 12
    destinations = [{"lat": 0.0, "lon": 0.0}] * 15
    result = server.distance_matrix(origins=origins, destinations=destinations)
    assert result["error"] == "bad_request"
    assert "elements" not in result
    assert result["detail"] == (
        "origins and destinations each accept at most 10 points, "
        "got 12 origins and 15 destinations"
    )


def test_distance_matrix_malformed_point():
    result = server.distance_matrix(
        origins=[{"lon": -97.7431}], destinations=[{"lat": 0.0, "lon": 0.0}]
    )
    assert result["error"] == "bad_request"


def test_distance_matrix_empty_origins():
    result = server.distance_matrix(origins=[], destinations=[{"lat": 0.0, "lon": 0.0}])
    assert result == {"elements": []}


def test_distance_matrix_empty_destinations():
    result = server.distance_matrix(origins=[{"lat": 0.0, "lon": 0.0}], destinations=[])
    assert result == {"elements": []}
