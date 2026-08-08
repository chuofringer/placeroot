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


def test_route_max_straight_line_derived_cap_magnitudes():
    """ROUTE_MAX_STRAIGHT_LINE_M is derived from each mode's max_radius_m
    (2 * (max_radius_m - SNAP_RADIUS_M) / RADIUS_BUFFER), not an
    independently-chosen constant — assert the actual derived numbers so a
    future change to MODE_CONFIG's radii (or RADIUS_BUFFER/SNAP_RADIUS_M)
    that silently drifts the two apart gets caught here."""
    assert routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"] == pytest.approx(7520.0)
    assert routing.ROUTE_MAX_STRAIGHT_LINE_M["cycle"] == pytest.approx(23520.0)
    assert routing.ROUTE_MAX_STRAIGHT_LINE_M["drive"] == pytest.approx(95520.0)


def test_route_straight_line_just_beyond_derived_cap_raises_route_too_long():
    """A pair just past walk's *derived* cap (~7.52km) must raise
    RouteTooLong reporting that same derived cap as max_distance_m — before
    the fix, walk's advertised 25km cap never actually fired here because
    the isochrone extraction-radius check (base_radius_m > max_radius_m)
    bound first, at ~7.5km, and reported a mismatched number."""
    cap_m = routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"]
    delta_deg = (cap_m * 1.02) / 110_540.0  # ~2% beyond the cap, pure-latitude offset
    far_lat, far_lon = FROM_LAT + delta_deg, FROM_LON
    straight_line_m = routing._haversine_m(FROM_LAT, FROM_LON, far_lat, far_lon)
    assert straight_line_m > cap_m

    with pytest.raises(routing.RouteTooLong) as exc_info:
        routing.route(FROM_LAT, FROM_LON, far_lat, far_lon, mode="walk")
    assert exc_info.value.max_distance_m == pytest.approx(cap_m)


def test_route_straight_line_just_within_derived_cap_does_not_raise_route_too_long():
    """A pair just inside walk's derived cap must not raise RouteTooLong —
    it may still legitimately raise NoGraphNearby (this point sits outside
    the routing fixture's grid) but must get past the straight-line/radius
    gate first."""
    cap_m = routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"]
    delta_deg = (cap_m * 0.98) / 110_540.0  # ~2% inside the cap
    near_lat, near_lon = FROM_LAT + delta_deg, FROM_LON
    straight_line_m = routing._haversine_m(FROM_LAT, FROM_LON, near_lat, near_lon)
    assert straight_line_m < cap_m

    try:
        routing.route(FROM_LAT, FROM_LON, near_lat, near_lon, mode="walk")
    except routing.RouteTooLong:
        pytest.fail("RouteTooLong raised for a pair inside the derived cap")
    except (routing.NoGraphNearby, routing.UpstreamUnavailable):
        pass  # fine — the fixture doesn't have graph data out there


def test_midpoint_ordinary_pair_is_plain_average():
    lat, lon = routing._midpoint(0.0, 0.0, 0.0, 10.0)
    assert lat == pytest.approx(0.0)
    assert lon == pytest.approx(5.0)


def test_midpoint_antimeridian_pair_wraps_near_180():
    """A plain (lon1 + lon2) / 2 gives ~0.0 for this pair (wrong side of the
    globe — true midpoint is near +/-180). The antimeridian-aware midpoint
    must land near +/-180 instead."""
    lat, lon = routing._midpoint(0.0, 179.99, 0.0, -179.99)
    assert lat == pytest.approx(0.0)
    assert abs(lon) == pytest.approx(180.0, abs=0.02)
    naive_lon = (179.99 + -179.99) / 2.0
    assert abs(lon - naive_lon) > 100  # confirms the wrap actually did something


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


def test_route_retry_radius_used_after_empty_first_graph():
    """Bug 3: the first radius's empty graph (node_count() == 0) must not
    raise NoGraphNearby outright — it should move on and try the widened
    retry radius, which here succeeds."""
    calls: list[float] = []

    def fake_build_graph(center_lat, center_lon, radius_m, mode, speed_m_s=None):
        calls.append(radius_m)
        if len(calls) == 1:
            return routing.Graph()  # empty: node_count() == 0
        g = routing.Graph()
        g.add_node("src", FROM_LAT, FROM_LON)
        g.add_node("dst", TO_LAT, TO_LON)
        return g

    def fake_snap_to_graph(graph, lat, lon, *args, **kwargs):
        if graph.node_count() == 0:
            return None
        return "src" if (lat, lon) == (FROM_LAT, FROM_LON) else "dst"

    def fake_dijkstra_path_to_target(graph, source, target, speed):
        return (10.0, 5.0, [(source, 0.0), (target, 5.0)])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(routing, "build_graph", fake_build_graph)
        mp.setattr(routing, "snap_to_graph", fake_snap_to_graph)
        mp.setattr(routing, "_dijkstra_path_to_target", fake_dijkstra_path_to_target)
        result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")

    assert len(calls) == 2  # first radius attempted, then the retry
    assert result["distance_m"] == 5.0
    assert result["duration_s"] == 10.0


def test_route_retry_radius_used_after_snap_failure():
    """Bug 3: a snap failure at the first radius must not raise
    NoGraphNearby outright either — same widen-and-continue behavior as an
    empty graph."""
    calls: list[float] = []

    def fake_build_graph(center_lat, center_lon, radius_m, mode, speed_m_s=None):
        calls.append(radius_m)
        g = routing.Graph()
        g.add_node("src", FROM_LAT, FROM_LON)
        g.add_node("dst", TO_LAT, TO_LON)
        return g

    def fake_snap_to_graph(graph, lat, lon, *args, **kwargs):
        if len(calls) == 1:
            return None  # first radius: this endpoint fails to snap
        return "src" if (lat, lon) == (FROM_LAT, FROM_LON) else "dst"

    def fake_dijkstra_path_to_target(graph, source, target, speed):
        return (10.0, 5.0, [(source, 0.0), (target, 5.0)])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(routing, "build_graph", fake_build_graph)
        mp.setattr(routing, "snap_to_graph", fake_snap_to_graph)
        mp.setattr(routing, "_dijkstra_path_to_target", fake_dijkstra_path_to_target)
        result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")

    assert len(calls) == 2
    assert result["distance_m"] == 5.0


def test_route_prefers_no_route_when_retry_radius_fails_after_earlier_snap_success():
    """Bug 4: once both endpoints have snapped successfully at some radius
    (snapped_both), a *later*, larger retry radius that fails to build a
    usable graph must not raise NoGraphNearby and clobber that — it should
    fall through to the structured no_route result, since both points are
    known to be on real street graphs and simply disconnected."""
    calls: list[float] = []

    def fake_build_graph(center_lat, center_lon, radius_m, mode, speed_m_s=None):
        calls.append(radius_m)
        if len(calls) == 1:
            g = routing.Graph()
            g.add_node("src", FROM_LAT, FROM_LON)
            g.add_node("dst", TO_LAT, TO_LON)
            return g
        return routing.Graph()  # retry radius: empty graph

    def fake_snap_to_graph(graph, lat, lon, *args, **kwargs):
        if graph.node_count() == 0:
            return None
        return "src" if (lat, lon) == (FROM_LAT, FROM_LON) else "dst"

    def fake_dijkstra_path_to_target(graph, source, target, speed):
        return None  # no path found on the successfully-snapped first graph

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(routing, "build_graph", fake_build_graph)
        mp.setattr(routing, "snap_to_graph", fake_snap_to_graph)
        mp.setattr(routing, "_dijkstra_path_to_target", fake_dijkstra_path_to_target)
        result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")

    assert len(calls) == 2
    assert result["error"] == "no_route"


def test_route_reuses_cached_graph_across_calls():
    """route() goes through _get_or_build_graph (#39), so a repeat route
    over the same area must not pay a second graph extraction."""
    calls: list[float] = []
    real_build_graph = routing.build_graph

    def counting_build_graph(*args, **kwargs):
        calls.append(args[2])
        return real_build_graph(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(routing, "build_graph", counting_build_graph)
        first = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
        second = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")

    assert first["distance_m"] == second["distance_m"]
    assert len(calls) == 1, "second identical route paid a fresh graph extraction"


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


def test_server_route_rejects_out_of_range_coordinate():
    # #163: an out-of-range endpoint (e.g. lat 91, or a swapped lat/lon
    # pair) must be a bad_request at the tool boundary, not a planet-wide
    # transportation scan ending in no_graph_nearby.
    result = server.route(from_lat=91.0, from_lon=0.0, to_lat=91.001, to_lon=0.0, mode="walk")
    assert result["error"] == "bad_request"
    result = server.route(
        from_lat=FROM_LAT, from_lon=FROM_LON, to_lat=14.60, to_lon=120.98 + 180.0, mode="walk"
    )
    assert result["error"] == "bad_request"


def test_server_route_no_graph_nearby_far_from_the_grid():
    result = server.route(0.0, 0.0, 0.001, 0.001, mode="walk")
    assert result["error"] == "no_graph_nearby"
