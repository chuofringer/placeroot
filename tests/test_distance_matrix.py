"""Tests for the distance_matrix tool: caps, validation, and haversine correctness.

Correctness is checked against tests/_geo.haversine_m — an independent
implementation from the one distance_matrix actually uses (placeroot.geo),
so this isn't just checking the code against itself.
"""

from placeroot import errors, geocode, server
from tests._geo import haversine_m as oracle_haversine_m
from tests.test_gers_lookup import PLACE_ID

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


# --- LocationRef (roadmap #4.1): mixed origins/destinations ---------------


def test_coordinate_only_has_no_resolved_key():
    result = server.distance_matrix(origins=ORIGINS, destinations=DESTINATIONS)
    assert "error" not in result
    assert "resolved" not in result


def test_gers_id_origin_adds_resolved_origins_only():
    """Real fixture GERS id (see tests/test_gers_lookup.py) — no monkeypatch."""
    result = server.distance_matrix(
        origins=[PLACE_ID], destinations=[{"lat": 30.2672, "lon": -97.7431}]
    )
    assert "error" not in result
    assert result["resolved"]["origins"][0]["index"] == 0
    assert result["resolved"]["origins"][0]["matched_by"] == "gers_id"
    assert result["resolved"]["origins"][0]["id"] == PLACE_ID
    assert "destinations" not in result["resolved"]


def test_mixed_named_origin_and_destination(monkeypatch):
    def fake_resolve(query):
        assert query == "Austin Office"
        return {
            "name": query, "lat": 30.2672, "lon": -97.7431,
            "id": "gers-austin", "type": "place",
        }

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.distance_matrix(
        origins=["Austin Office"], destinations=[{"lat": 29.7604, "lon": -95.3698}]
    )
    assert "error" not in result
    assert result["resolved"]["origins"] == [
        {
            "index": 0, "name": "Austin Office", "id": "gers-austin",
            "lat": 30.2672, "lon": -97.7431, "matched_by": "name",
        }
    ]
    assert "destinations" not in result["resolved"]


def test_ambiguous_destination_name_is_an_indexed_error(monkeypatch):
    def fake_resolve(query):
        raise errors.AmbiguousPlace(query, candidates=[{"name": "X"}])

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.distance_matrix(
        origins=[{"lat": 30.2672, "lon": -97.7431}], destinations=["Ambiguous"]
    )
    assert result["error"] == "ambiguous_place"
    assert result["index"] == 0
    assert result["detail"].startswith("destinations[0]: ")


def test_over_cap_checked_before_resolution(monkeypatch):
    """The 10-point cap fails before any name resolution is attempted."""
    def boom(query):
        raise AssertionError("resolve_named_place must not be called past the cap")

    monkeypatch.setattr(geocode, "resolve_named_place", boom)
    origins = ["Somewhere"] * 11
    result = server.distance_matrix(
        origins=origins, destinations=[{"lat": 0.0, "lon": 0.0}]
    )
    assert result["error"] == "bad_request"
    assert "elements" not in result
