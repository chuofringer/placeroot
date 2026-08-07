"""Tests for routing.route()/server.route() — issue #160.

Uses the same offline transportation fixture (20x20 street grid, 100m
spacing) as test_routing.py's isochrone tests; see
scripts/build_routing_fixture.py and tests/_routing_fixture.py. The
autouse fixture in tests/conftest.py points routing.py at the committed
fixture for every test in this file, same as test_routing.py.
"""

import math

import pytest

from placeroot import routing, server

from ._routing_fixture import build_routing_fixture as fx

# Two grid nodes connected the "long way" (vertical run, same column) —
# same pair test_routing.py's test_dijkstra_matches_manhattan_grid_distance
# uses, so they're known-connected on this fixture for every mode (plain
# cardinal residential edges, no motorway/footway/diagonal involved).
FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)


def _path_length_m() -> float:
    total = 0.0
    nodes = [(2, 2), (2, 3), (2, 4), (2, 5)]
    for a, b in zip(nodes, nodes[1:]):
        lat1, lon1 = fx.node_latlon(*a)
        lat2, lon2 = fx.node_latlon(*b)
        total += routing._haversine_m(lat1, lon1, lat2, lon2)
    return total


EXPECTED_GRID_PATH_M = _path_length_m()


def test_route_walk_distance_matches_duration_times_speed():
    result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert result["distance_m"] > 0
    assert result["duration_s"] > 0
    assert result["distance_m"] == pytest.approx(
        result["duration_s"] * routing.DEFAULT_SPEED_M_S, rel=0.01
    )
    # Also plausible against the known Manhattan-grid shortest path length.
    assert result["distance_m"] == pytest.approx(EXPECTED_GRID_PATH_M, rel=0.05)


def test_route_cycle_distance_matches_duration_times_speed():
    result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="cycle")
    assert result["distance_m"] > 0
    assert result["duration_s"] > 0
    assert result["distance_m"] == pytest.approx(
        result["duration_s"] * routing.CYCLE_SPEED_M_S, rel=0.01
    )


def test_route_drive_distance_and_duration_are_positive_and_plausible():
    result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive")
    assert result["distance_m"] > 0
    assert result["duration_s"] > 0
    straight_line_m = routing._haversine_m(FROM_LAT, FROM_LON, TO_LAT, TO_LON)
    # Road distance is never shorter than the straight line between the points.
    assert result["distance_m"] >= straight_line_m - 1e-6


def test_route_straight_line_beyond_cap_raises_route_too_long():
    with pytest.raises(routing.RouteTooLong):
        routing.route(0.0, 0.0, 10.0, 10.0, mode="walk")


def test_route_no_path_returns_structured_no_route_result():
    """Drive mode excludes the footway bridge — the only river crossing — so
    a near-bank and far-bank node are in permanently disconnected components
    for drive, regardless of extraction radius (same disconnection
    test_drive_class_filter_excludes_footway_bridge asserts on raw
    adjacency)."""
    j = 5  # away from BRIDGE_J, so this row has no crossing at all
    near_lat, near_lon = fx.node_latlon(fx.RIVER_GAP_I, j)
    far_lat, far_lon = fx.node_latlon(fx.RIVER_GAP_I + 1, j)

    result = routing.route(near_lat, near_lon, far_lat, far_lon, mode="drive")

    assert result["error"] == "no_route"
    assert "detail" in result


def test_route_unsupported_mode_raises():
    with pytest.raises(routing.UnsupportedMode):
        routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="teleport")


def test_route_non_numeric_coordinate_raises_value_error():
    with pytest.raises(ValueError):
        routing.route("not-a-number", FROM_LON, TO_LAT, TO_LON, mode="walk")


def test_route_non_finite_coordinate_raises_value_error():
    with pytest.raises(ValueError):
        routing.route(math.nan, FROM_LON, TO_LAT, TO_LON, mode="walk")
    with pytest.raises(ValueError):
        routing.route(math.inf, FROM_LON, TO_LAT, TO_LON, mode="walk")


def test_server_route_tool_happy_path():
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert "error" not in result
    assert result["distance_m"] > 0
    assert result["duration_s"] > 0
    assert result["mode"] == "walk"
    assert result["from"] == {"lat": FROM_LAT, "lon": FROM_LON}
    assert result["to"] == {"lat": TO_LAT, "lon": TO_LON}


def test_server_route_unsupported_mode():
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="teleport")
    assert result["error"] == "unsupported_mode"


def test_server_route_too_long():
    result = server.route(0.0, 0.0, 10.0, 10.0, mode="walk")
    assert result["error"] == "route_too_long"


def test_server_route_bad_request_for_non_numeric_coordinate():
    result = server.route("nope", FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert result["error"] == "bad_request"


def test_server_route_no_graph_nearby_far_from_the_grid():
    result = server.route(0.0, 0.0, 0.001, 0.001, mode="walk")
    assert result["error"] == "no_graph_nearby"
