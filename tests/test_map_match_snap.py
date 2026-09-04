"""snap_trace (#440): per-point trace snapping against the street graph.

Runs entirely against the committed offline routing fixture (20x20 street
grid, 100m spacing) — see scripts/build_routing_fixture.py and
tests/_routing_fixture.py; the autouse offline_data fixture in
tests/conftest.py points routing.py at it and clears the graph cache before
every test, so each test here builds its graph fresh from the fixture. No
network, no live Overture scan.
"""

import pytest

from placeroot import map_match, routing

from ._routing_fixture import build_routing_fixture as fx


def test_point_near_a_known_edge_snaps_onto_it():
    """A point 5m off the (2,2)-(3,2) grid edge's midpoint snaps back onto
    that edge, near its 0.5 fraction, with a small snap distance."""
    a_lat, a_lon = fx.node_latlon(2, 2)
    mid_lat, mid_lon = fx._offset(a_lat, a_lon, 50.0, 90)  # 50m east: edge midpoint
    query_lat, query_lon = fx._offset(mid_lat, mid_lon, 5.0, 0)  # 5m further off the line

    [snapped] = map_match.snap_trace([{"lat": query_lat, "lon": query_lon}], mode="walk")

    assert snapped.matched
    assert snapped.distance_m < 10.0
    expected_nodes = {fx.node_id(2, 2), fx.node_id(3, 2)}
    assert set(snapped.edge) == expected_nodes
    assert snapped.fraction == pytest.approx(0.5, abs=0.05)
    assert snapped.snapped_lat is not None and snapped.snapped_lon is not None


def test_point_far_from_everything_is_unmatched_not_dropped():
    """A trace point 500m from the nearest real edge is returned, flagged
    unmatched, rather than being silently dropped from the result."""
    near_lat, near_lon = fx.node_latlon(2, 2)
    near_lat, near_lon = fx._offset(near_lat, near_lon, 50.0, 90)
    far_lat, far_lon = fx._offset(near_lat, near_lon, 700.0, 180)  # ~500m past the grid's edge

    points = [
        {"lat": near_lat, "lon": near_lon},
        {"lat": far_lat, "lon": far_lon},
    ]
    near_result, far_result = map_match.snap_trace(points, mode="walk")

    assert near_result.matched
    assert not far_result.matched
    assert far_result.lat == far_lat
    assert far_result.lon == far_lon
    assert far_result.index == 1


def test_drive_mode_excludes_the_footway_bridge():
    """The fixture's river gap is only crossable via a class=footway bridge
    at row BRIDGE_J — walkable graphs use it, a drive graph must never snap
    a point there onto it."""
    a_lat, a_lon = fx.node_latlon(fx.RIVER_GAP_I, fx.BRIDGE_J)
    b_lat, b_lon = fx.node_latlon(fx.RIVER_GAP_I + 1, fx.BRIDGE_J)
    mid_lat, mid_lon = fx._offset(a_lat, a_lon, 50.0, 90)
    query_lat, query_lon = fx._offset(mid_lat, mid_lon, 5.0, 0)
    bridge_nodes = {
        fx.node_id(fx.RIVER_GAP_I, fx.BRIDGE_J),
        fx.node_id(fx.RIVER_GAP_I + 1, fx.BRIDGE_J),
    }

    [walked] = map_match.snap_trace([{"lat": query_lat, "lon": query_lon}], mode="walk")
    assert walked.matched
    assert set(walked.edge) == bridge_nodes

    [driven] = map_match.snap_trace([{"lat": query_lat, "lon": query_lon}], mode="drive")
    assert driven.edge is None or set(driven.edge) != bridge_nodes


def test_one_graph_build_per_call(monkeypatch):
    calls = []
    original = routing.build_graph

    def counting_build_graph(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", counting_build_graph)

    points = [{"lat": lat, "lon": lon} for lat, lon in (fx.node_latlon(i, 2) for i in range(5))]
    map_match.snap_trace(points, mode="walk")

    assert len(calls) == 1


def test_over_cap_points_raise_value_error():
    lat, lon = fx.node_latlon(2, 2)
    points = [{"lat": lat, "lon": lon}] * (map_match.MAX_TRACE_POINTS + 1)
    with pytest.raises(ValueError):
        map_match.snap_trace(points, mode="walk")


def test_empty_trace_returns_empty_list():
    assert map_match.snap_trace([], mode="walk") == []


def test_unsupported_mode_raises():
    lat, lon = fx.node_latlon(2, 2)
    with pytest.raises(routing.UnsupportedMode):
        map_match.snap_trace([{"lat": lat, "lon": lon}], mode="teleport")
