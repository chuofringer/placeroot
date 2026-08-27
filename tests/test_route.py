"""Tests for routing.route()/server.route() — issue #160.

Uses the same offline transportation fixture (20x20 street grid, 100m
spacing) as test_routing.py's isochrone tests; see
scripts/build_routing_fixture.py and tests/_routing_fixture.py. The
autouse fixture in tests/conftest.py points routing.py at the committed
fixture for every test in this file, same as test_routing.py.
"""

import asyncio
import inspect
import math

import pytest

from placeroot import budget, errors, geocode, routing, server, simplify

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
    # Roadmap §4, next tier: no_route names the next move, tuned per mode —
    # here drive is blocked by the footway-only bridge, so the hint points
    # at walk/cycle rather than an invented capability.
    assert result["try"]
    assert len(result["try"]) < 200
    assert "walk" in result["try"] or "cycle" in result["try"]


def test_route_retry_radius_used_after_empty_first_graph():
    """Bug 3: the first radius's empty graph (node_count() == 0) must not
    raise NoGraphNearby outright — it should move on and try the widened
    retry radius, which here succeeds."""
    calls: list[float] = []

    def fake_build_graph(center_lat, center_lon, radius_m, mode, speed_m_s=None, **kwargs):
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

    def fake_build_graph(center_lat, center_lon, radius_m, mode, speed_m_s=None, **kwargs):
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

    def fake_build_graph(center_lat, center_lon, radius_m, mode, speed_m_s=None, **kwargs):
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
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", confirm=True)
    assert "error" not in result
    assert result["distance_m"] > 0
    assert result["duration_s"] > 0
    assert result["mode"] == "walk"
    assert result["from"] == {"lat": FROM_LAT, "lon": FROM_LON}
    assert result["to"] == {"lat": TO_LAT, "lon": TO_LON}


def test_server_route_unsupported_mode():
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="teleport")
    assert result["error"] == "unsupported_mode"
    assert result["detail"] == "unsupported mode 'teleport'; supported: ['cycle', 'drive', 'walk']"


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
    result = server.route(0.0, 0.0, 0.001, 0.001, mode="walk", confirm=True)
    assert result["error"] == "no_graph_nearby"


# --- include_path: optional simplified route geometry (#161) -----------------

# Opposite corners of the 20x20 grid: a long Manhattan path with many turns,
# so simplification has something real to chew on (a straight run would be
# collapsed to its two endpoints by epsilon=0 alone and prove nothing).
CORNER_A_LAT, CORNER_A_LON = fx.node_latlon(0, 0)
CORNER_B_LAT, CORNER_B_LON = fx.node_latlon(19, 19)


def _snapped_lonlat(lat: float, lon: float, other_lat: float, other_lon: float, mode: str):
    """The [lon, lat] of the graph node route() would snap (lat, lon) to.

    Rebuilt through the same extraction the router uses (same midpoint,
    same base radius) so the expectation is the router's own snapping
    result, not a hand-picked grid node that merely looks right.
    """
    center_lat, center_lon = routing._midpoint(lat, lon, other_lat, other_lon)
    straight_m = routing._haversine_m(lat, lon, other_lat, other_lon)
    radius_m = max(
        straight_m / 2.0 * routing.RADIUS_BUFFER + routing.SNAP_RADIUS_M,
        routing.ROUTE_MIN_RADIUS_M,
    )
    graph = routing._get_or_build_graph(center_lat, center_lon, radius_m, mode, speed_m_s=None)
    node_id = routing.snap_to_graph(graph, lat, lon)
    node_lat, node_lon = graph.coords[node_id]
    return [
        round(node_lon, routing.PATH_COORD_PRECISION),
        round(node_lat, routing.PATH_COORD_PRECISION),
    ]


def test_route_omits_path_by_default():
    """Compact-first: the polyline is the largest thing route() can return,
    so it must be absent unless asked for — not present-but-empty."""
    result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert "path" not in result
    assert "path_max_deviation_m" not in result
    assert "path_omitted" not in result


def test_route_include_path_returns_a_geojson_linestring():
    result = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    path = result["path"]
    assert path["type"] == "LineString"
    coords = path["coordinates"]
    assert len(coords) >= 2
    assert all(len(c) == 2 for c in coords)
    # [lon, lat] order, both inside the fixture grid's extent.
    for lon, lat in coords:
        assert -180.0 <= lon <= 180.0
        assert -90.0 <= lat <= 90.0
    assert "path_max_deviation_m" in result
    assert "path_omitted" not in result


def test_route_path_starts_at_a_and_ends_at_b_snapped_nodes():
    """Acceptance criterion: endpoints correct — the line starts at the
    origin's snapped node and ends at the destination's, exactly."""
    result = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    coords = result["path"]["coordinates"]
    assert coords[0] == _snapped_lonlat(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, "walk"
    )
    assert coords[-1] == _snapped_lonlat(
        CORNER_B_LAT, CORNER_B_LON, CORNER_A_LAT, CORNER_A_LON, "walk"
    )


def test_route_path_is_ordered_a_to_b_not_reversed():
    """Reversing the query reverses the geometry: the predecessor chain is
    walked source -> target, so B->A must not come back in A->B order."""
    forward = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    backward = routing.route(
        CORNER_B_LAT, CORNER_B_LON, CORNER_A_LAT, CORNER_A_LON, mode="walk", include_path=True
    )
    assert forward["path"]["coordinates"][0] == backward["path"]["coordinates"][-1]
    assert forward["path"]["coordinates"][-1] == backward["path"]["coordinates"][0]
    assert forward["path"]["coordinates"] != backward["path"]["coordinates"]


def test_route_path_traces_the_route_not_the_straight_line():
    """The polyline should follow the street path, so summing its segment
    lengths lands near the reported distance_m — and well above the
    straight-line distance the two corners are apart."""
    result = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    coords = result["path"]["coordinates"]
    traced_m = sum(
        routing._haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:])
    )
    straight_m = routing._haversine_m(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON
    )
    assert traced_m > straight_m * 1.2
    assert traced_m == pytest.approx(result["distance_m"], rel=0.05)


def test_route_path_deviation_is_zero_when_nothing_is_lost():
    """At the default budget this route fits unsimplified (epsilon=0 only
    drops exactly-colinear points), so the reported deviation is 0.0 —
    the deviation is a real measurement, not a constant."""
    result = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    assert result["path_max_deviation_m"] == 0.0


def test_route_path_is_simplified_and_reports_deviation_under_a_tight_budget(monkeypatch):
    """Acceptance criterion: token-capped with a reported deviation. A
    budget too small for the full path yields fewer points, a non-zero
    deviation, and a response that actually fits."""
    full = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "150")
    tight = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )

    assert "path" in tight
    tight_coords = tight["path"]["coordinates"]
    assert len(tight_coords) < len(full["path"]["coordinates"])
    assert tight["path_max_deviation_m"] > 0
    assert budget.estimate_tokens(tight) <= 150
    # Simplifying must not move the endpoints.
    assert tight_coords[0] == full["path"]["coordinates"][0]
    assert tight_coords[-1] == full["path"]["coordinates"][-1]


def test_route_path_deviation_shrinks_as_the_budget_grows(monkeypatch):
    """More budget buys a closer line — the deviation is tied to how much
    was actually dropped, not to an arbitrary tolerance."""
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "110")
    coarse = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "150")
    finer = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    assert coarse["path_max_deviation_m"] > finer["path_max_deviation_m"] > 0
    assert len(coarse["path"]["coordinates"]) < len(finer["path"]["coordinates"])


def test_route_drops_the_path_honestly_when_the_budget_cannot_hold_it(monkeypatch):
    """Degrade honestly: a budget with no room left for even a two-point
    line omits the path and says so, rather than returning a line that
    stops short of the destination."""
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "80")
    result = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    assert "path" not in result
    assert result["path_omitted"] is True
    assert "path_note" in result
    # The distance/duration answer itself survives untouched.
    assert result["distance_m"] > 0
    assert result["duration_s"] > 0


def test_route_path_omitted_rather_than_truncated_when_simplify_cannot_fit(monkeypatch):
    """Even with budget headroom for the envelope, if the simplified line
    still overflows (simplify_geometry returns a best effort rather than
    failing) the path is dropped, never handed back over budget."""
    monkeypatch.setattr(routing.simplify, "simplify_geometry", lambda geojson, max_tokens: {
        "geometry": geojson, "max_deviation_m": 0.0,
        "original_points": len(geojson["coordinates"]),
        "kept_points": len(geojson["coordinates"]),
    })
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "120")
    result = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    assert "path" not in result
    assert result["path_omitted"] is True


@pytest.mark.parametrize("mode", ["walk", "cycle", "drive"])
def test_route_include_path_works_for_every_mode(mode):
    result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode=mode, include_path=True)
    coords = result["path"]["coordinates"]
    assert result["mode"] == mode
    assert len(coords) >= 2
    assert coords[0] == _snapped_lonlat(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode)
    assert coords[-1] == _snapped_lonlat(TO_LAT, TO_LON, FROM_LAT, FROM_LON, mode)


def test_route_include_path_leaves_no_route_result_unchanged():
    """The structured no_route answer has no path to report and must not
    grow one (or a path_omitted flag) just because include_path was set."""
    j = 5
    near_lat, near_lon = fx.node_latlon(fx.RIVER_GAP_I, j)
    far_lat, far_lon = fx.node_latlon(fx.RIVER_GAP_I + 1, j)

    plain = routing.route(near_lat, near_lon, far_lat, far_lon, mode="drive")
    with_path = routing.route(
        near_lat, near_lon, far_lat, far_lon, mode="drive", include_path=True
    )
    assert with_path == plain
    assert with_path["error"] == "no_route"


def test_route_include_path_leaves_route_too_long_unchanged():
    with pytest.raises(routing.RouteTooLong):
        routing.route(0.0, 0.0, 10.0, 10.0, mode="walk", include_path=True)


def test_route_path_for_identical_endpoints_is_a_valid_zero_length_line():
    """Both endpoints snapping to the same node gives a one-node path; a
    one-position LineString isn't valid GeoJSON, so it's emitted as the
    degenerate two-position line."""
    result = routing.route(FROM_LAT, FROM_LON, FROM_LAT, FROM_LON, mode="walk", include_path=True)
    coords = result["path"]["coordinates"]
    assert result["distance_m"] == 0.0
    assert len(coords) == 2
    assert coords[0] == coords[1]


def test_route_path_survives_the_simplify_geometry_tool_round_trip():
    """The emitted geometry is ordinary GeoJSON the simplify_geometry tool
    accepts — no bespoke shape that only route() understands."""
    result = routing.route(
        CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, mode="walk", include_path=True
    )
    round_trip = server.simplify_geometry(result["path"], max_tokens=500)
    assert "error" not in round_trip
    assert round_trip["geometry"]["type"] == "LineString"


# --- the path must follow the road, not chord across it (#161 sweep) --------

SWITCHBACK_START, SWITCHBACK_END = fx.switchback_endpoints_latlon()


def _linestring_length_m(coords: list[list[float]]) -> float:
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        total += routing._haversine_m(lat1, lon1, lat2, lon2)
    return total


def test_route_path_follows_segment_shape_instead_of_chording_it():
    """Graph nodes are segment endpoints and interior connectors, so a line
    drawn node-to-node cuts straight across every curve in between. On the
    fixture's switchback spur that chord is ~430 m against ~850 m of actual
    road: pre-fix the emitted line had 2 coordinates and was half the length
    of the distance_m printed beside it, while path_max_deviation_m claimed
    0.0. The emitted geometry must account for the distance it reports."""
    result = routing.route(
        *SWITCHBACK_START, *SWITCHBACK_END, mode="walk", include_path=True
    )
    coords = result["path"]["coordinates"]
    line_m = _linestring_length_m(coords)

    # The detour is real: a straight line between the endpoints is far
    # shorter than the route, so a chorded path could not fake this.
    chord_m = routing._haversine_m(*SWITCHBACK_START, *SWITCHBACK_END)
    assert chord_m < result["distance_m"] * 0.6

    assert len(coords) > 2
    assert line_m == pytest.approx(result["distance_m"], rel=0.02)


def test_route_path_deviation_is_zero_only_when_the_shape_is_fully_kept():
    """path_max_deviation_m is measured against the road-following line, so
    an unsimplified curvy route reports 0.0 honestly — and a budget that
    forces vertices out reports a deviation on the order of the detour it
    dropped, not 0.0."""
    full = routing.route(*SWITCHBACK_START, *SWITCHBACK_END, mode="walk", include_path=True)
    assert full["path_max_deviation_m"] == 0.0
    assert len(full["path"]["coordinates"]) == len(
        set(map(tuple, full["path"]["coordinates"]))
    )


def test_route_path_deviation_accounts_for_build_time_shape_pruning(monkeypatch):
    """Shapes are pruned at build time (GRAPH_SHAPE_EPSILON_M), so a path
    whose token-fit simplification dropped nothing is still not exact.
    Reporting only the simplify deviation would print 0.0 for a line that
    is knowingly off the road — the reported number covers both."""
    monkeypatch.setattr(routing, "GRAPH_SHAPE_EPSILON_M", 120.0)
    routing._graph_cache.clear()
    result = routing.route(*SWITCHBACK_START, *SWITCHBACK_END, mode="walk", include_path=True)
    # 120 m of tolerance flattens the fixture's 100/150 m zigzag corners...
    assert len(result["path"]["coordinates"]) < 8
    # ...and that is reported, not hidden behind a full-fidelity 0.0.
    assert result["path_max_deviation_m"] > 0
    routing._graph_cache.clear()


def test_route_path_shape_is_direction_symmetric():
    """The stored shape belongs to the segment, not to a direction: routing
    B->A must return the same road, reversed."""
    forward = routing.route(*SWITCHBACK_START, *SWITCHBACK_END, mode="walk", include_path=True)
    backward = routing.route(*SWITCHBACK_END, *SWITCHBACK_START, mode="walk", include_path=True)
    assert backward["path"]["coordinates"] == list(
        reversed(forward["path"]["coordinates"])
    )


def test_route_path_omitted_note_is_priced_inside_the_budget(monkeypatch):
    """The ~35-token "why there's no path" note used to be appended after
    the fit check, so a budget too small for the path overran anyway
    (budget=60 produced a 78-token response). Whatever survives, the final
    response fits — and asking for a path never costs more than not asking
    for one at a budget too small for either."""
    for limit in (40, 50, 60, 70, 80, 100):
        monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", str(limit))
        kwargs = dict(mode="walk")
        plain = routing.route(CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON, **kwargs)
        result = routing.route(
            CORNER_A_LAT, CORNER_A_LON, CORNER_B_LAT, CORNER_B_LON,
            include_path=True, **kwargs,
        )
        # The distance/duration answer is irreducible, so at absurd budgets
        # the floor is what route() costs without a path at all; include_path
        # must never push past that or past the budget itself.
        floor = budget.estimate_tokens(plain)
        assert budget.estimate_tokens(result) <= max(limit, floor), limit
        assert result["distance_m"] > 0
        if "path_note" in result:
            assert result["path_omitted"] is True


def test_server_route_include_path_passthrough():
    result = server.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", include_path=True, confirm=True
    )
    assert "error" not in result
    assert result["path"]["type"] == "LineString"
    assert "path_max_deviation_m" in result

    default = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", confirm=True)
    assert "path" not in default


def test_server_route_schema_exposes_include_path_defaulting_to_false():
    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "route")
    schema = tool.model_dump(mode="json", by_alias=True, exclude_none=True)["inputSchema"]
    prop = schema["properties"]["include_path"]
    assert prop.get("type") == "boolean"
    assert prop.get("default") is False
    assert "include_path" not in schema.get("required", [])


def test_server_route_schema_exposes_confirm_defaulting_to_false():
    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "route")
    schema = tool.model_dump(mode="json", by_alias=True, exclude_none=True)["inputSchema"]
    prop = schema["properties"]["confirm"]
    assert prop.get("type") == "boolean"
    assert prop.get("default") is False
    assert "confirm" not in schema.get("required", [])
    desc = (tool.description or "").lower()
    assert "confirm=true after the user agreed" in desc
    # #419: route accepts names/ids itself now, so geocode may only appear as
    # the instruction *not* to pre-resolve — never as a step to take first.
    assert "do not call geocode()" in " ".join(desc.split())


# --- shape identity: the geometry emitted must be the edge dijkstra used ----


def _parallel_edge_graph():
    """A--B twice: an undirected 50-weight edge bulging north, and a one-way
    B->A 100-weight edge bulging south.

    Dijkstra travelling B->A takes the 50-weight undirected edge (it is
    traversable in both directions and cheaper), so B->A's geometry is the
    north bulge, read backwards.
    """
    graph = routing.Graph()
    graph.add_node("A", 37.800000, -122.400000)
    graph.add_node("B", 37.800000, -122.394000)
    north = [(-122.398000, 37.801000), (-122.396000, 37.801000)]
    south = [(-122.396000, 37.799000), (-122.398000, 37.799000)]
    graph.has_shapes = True
    graph.add_edge("A", "B", 50.0, 50.0, directed=False, shape=north)
    graph.add_edge("B", "A", 100.0, 100.0, directed=True, shape=south)
    return graph, north, south


def test_shape_between_picks_the_edge_dijkstra_would_traverse():
    """Regression (#161 sweep): shape lookup used to fall back to the (b, a)
    key without comparing weights, so a *parallel* edge's geometry — one
    dijkstra never traversed — was emitted for the traversal.

    Here B->A resolves to the stored (B, A) entry (the one-way south bulge,
    weight 100) under the old rule, while dijkstra actually crosses the
    undirected north bulge at weight 50.
    """
    graph, north, south = _parallel_edge_graph()

    # The cheapest B->A edge really is the undirected one.
    assert min(w for nb, w, _ in graph.adjacency["B"] if nb == "A") == 50.0

    shape, _ = graph.shape_between("B", "A")
    assert shape == list(reversed(north))
    assert shape != south

    # ...and the forward direction is unchanged.
    assert graph.shape_between("A", "B")[0] == north


def test_emitted_path_follows_the_traversed_parallel_edge():
    """The same thing end to end through _path_linestring: the emitted
    LineString runs north of the chord (the edge dijkstra used), not south."""
    graph, north, _ = _parallel_edge_graph()
    geometry, _ = routing._path_linestring(graph, [("B", 0.0), ("A", 50.0)], 500)
    interior = geometry["coordinates"][1:-1]
    assert all(lat > 37.800000 for _lon, lat in interior)


def test_a_straight_edge_beats_a_heavier_shaped_parallel_edge():
    """Regression (#161 sweep): straight edges used to skip shape
    registration entirely, so the min-weight parallel-edge rule never saw
    them — a heavier curvy edge kept the slot against a lighter straight
    edge it could never have beaten, and the emitted line followed a road
    dijkstra never used (traced length exceeding distance_m, and the wrong
    edge's dropped_m reported as deviation).
    """
    graph = routing.Graph()
    graph.add_node("A", 37.800000, -122.400000)
    graph.add_node("B", 37.800000, -122.394000)
    graph.has_shapes = True
    south = [(-122.398000, 37.799000), (-122.396000, 37.799000)]
    graph.add_edge("A", "B", 50.0, 50.0, directed=False)  # straight chord
    graph.add_edge("A", "B", 100.0, 100.0, directed=False, shape=south, shape_dropped_m=3.0)

    # The cheapest A->B edge really is the straight one, so its (empty)
    # geometry and 0.0 dropped_m must answer — in both directions.
    assert min(w for nb, w, _ in graph.adjacency["A"] if nb == "B") == 50.0
    assert graph.shape_between("A", "B") == ([], 0.0)
    assert graph.shape_between("B", "A") == ([], 0.0)

    # End to end: the emitted LineString is the chord, not the south bulge.
    geometry, deviation_m = routing._path_linestring(graph, [("A", 0.0), ("B", 50.0)], 500)
    assert geometry["coordinates"] == [[-122.400000, 37.800000], [-122.394000, 37.800000]]
    assert deviation_m == 0.0

    # Registration order must not matter: heavier shaped edge first.
    graph2 = routing.Graph()
    graph2.add_node("A", 37.800000, -122.400000)
    graph2.add_node("B", 37.800000, -122.394000)
    graph2.has_shapes = True
    graph2.add_edge("A", "B", 100.0, 100.0, directed=False, shape=south, shape_dropped_m=3.0)
    graph2.add_edge("A", "B", 50.0, 50.0, directed=False)
    assert graph2.shape_between("A", "B") == ([], 0.0)
    assert graph2.shape_between("B", "A") == ([], 0.0)


def test_a_one_way_edges_shape_is_not_offered_for_the_forbidden_direction():
    """Shapes are keyed by direction of travel, so a one-way edge's geometry
    never answers for the direction it cannot be driven."""
    graph = routing.Graph()
    graph.add_node("A", 37.800000, -122.400000)
    graph.add_node("B", 37.800000, -122.394000)
    graph.has_shapes = True
    graph.add_edge("A", "B", 50.0, 50.0, directed=True, shape=[(-122.397000, 37.801000)])
    assert graph.shape_between("A", "B")[0] == [(-122.397000, 37.801000)]
    assert graph.shape_between("B", "A") == ([], 0.0)


# --- shapes are opt-in, and the cache never downgrades a path request ------


def test_isochrone_graphs_carry_no_edge_shapes():
    """Isochrone never reads shapes; building them anyway charged every
    cached isochrone graph ~31% extra memory, x GRAPH_CACHE_MAXSIZE."""
    routing.clear_graph_cache()
    routing.isochrone(*SWITCHBACK_START, minutes=5, mode="walk")
    entries = list(routing._graph_cache.values())
    assert entries
    assert all(not entry.graph.has_shapes for entry in entries)
    assert all(not entry.graph._edge_shapes for entry in entries)
    routing.clear_graph_cache()


def test_build_graph_without_shapes_reports_no_shape_and_no_deviation():
    graph = routing.build_graph(*SWITCHBACK_START, 1500.0, mode="walk")
    assert graph.has_shapes is False
    assert graph._edge_shapes == {}
    assert graph.shape_between(*list(graph.adjacency)[:2]) == ([], 0.0)


def test_a_path_request_is_never_served_a_shapeless_cached_graph():
    """The shared cache is the trap: isochrone warms an area with a
    shapeless graph, and a later route(include_path=True) over the same
    center must still get road-following geometry rather than silently
    inheriting the cheap graph and chording every curve.

    Same assertion as the switchback test, but with the cache pre-poisoned.
    """
    routing.clear_graph_cache()
    routing.isochrone(*SWITCHBACK_START, minutes=15, mode="walk")
    assert all(not e.graph.has_shapes for e in routing._graph_cache.values())

    result = routing.route(*SWITCHBACK_START, *SWITCHBACK_END, mode="walk", include_path=True)
    coords = result["path"]["coordinates"]
    assert len(coords) > 2
    assert _linestring_length_m(coords) == pytest.approx(result["distance_m"], rel=0.02)
    # The area was upgraded in place, not duplicated: some cached graph for
    # this area now carries shapes.
    assert any(e.graph.has_shapes for e in routing._graph_cache.values())
    routing.clear_graph_cache()


def test_a_shape_bearing_cached_graph_still_serves_shapeless_requests():
    """Subsumption runs one way only — the richer graph answers everything,
    so warming with a path request doesn't force a second extraction."""
    routing.clear_graph_cache()
    routing.route(*SWITCHBACK_START, *SWITCHBACK_END, mode="walk", include_path=True)
    before = len(routing._graph_cache)
    assert before
    routing.route(*SWITCHBACK_START, *SWITCHBACK_END, mode="walk")
    assert len(routing._graph_cache) == before
    routing.clear_graph_cache()


# --- deviation composes ----------------------------------------------------


def test_path_deviation_sums_pruning_and_simplification():
    """Regression (#161 sweep): the two displacements COMPOSE.

    Build-time pruning moves the retained vertices off the source roads;
    token-fit simplification then moves the emitted line off those
    already-pruned vertices. A vertex pruned 7.5m one way and simplified
    30m further the same way is 37.5m off the road, so reporting max() of
    the two understates what the caller is told is a maximum.
    """
    graph = routing.Graph()
    graph.add_node("A", 37.800000, -122.400000)
    graph.add_node("B", 37.800000, -122.390000)
    graph.has_shapes = True
    zigzag = [
        (-122.400000 + 0.000500 * i, 37.800000 + (0.000300 if i % 2 else -0.000300))
        for i in range(1, 20)
    ]
    dropped_m = 7.5
    graph.add_edge("A", "B", 900.0, 900.0, shape=zigzag, shape_dropped_m=dropped_m)

    path = [("A", 0.0), ("B", 900.0)]
    coords = [[-122.400000, 37.800000], *[[lon, lat] for lon, lat in zigzag]]
    coords.append([-122.390000, 37.800000])

    # Generous budget: nothing is simplified away, so the whole deviation is
    # the build-time pruning.
    _, full_dev = routing._path_linestring(graph, path, 4000)
    assert full_dev == pytest.approx(dropped_m)

    # Tight budget: simplification bites, and its deviation ADDS to pruning's.
    tight_tokens = 90
    _, tight_dev = routing._path_linestring(graph, path, tight_tokens)
    simplify_dev = simplify.simplify_geometry(
        {"type": "LineString", "coordinates": coords}, max_tokens=tight_tokens
    )["max_deviation_m"]
    assert simplify_dev > 0
    assert tight_dev == pytest.approx(simplify_dev + dropped_m)
    assert tight_dev > max(simplify_dev, dropped_m)  # the old, understating rule


# --- from/to as LocationRefs (#419) ------------------------------------------
#
# route's two ends now take the same {"lat","lon"} dict | GERS id |
# free-text name refs from_to takes, resolved by the same machinery; the
# four scalars keep working untouched. Resolves are mocked here for the
# same reason test_from_to.py mocks them: the street graph is the committed
# fixture, and nothing in this file should need live geocoding.


def _hit(name, lat, lon, id_="gers-x"):
    return {"name": name, "lat": lat, "lon": lon, "id": id_, "type": "place"}


def _ab(query):
    if query == "A":
        return _hit(query, FROM_LAT, FROM_LON, id_="gers-a")
    return _hit(query, TO_LAT, TO_LON, id_="gers-b")


def test_route_resolves_name_refs_for_both_ends(monkeypatch):
    monkeypatch.setattr(geocode, "resolve_named_place", _ab)
    result = server.route(from_="A", to="B", mode="walk", confirm=True)
    assert "error" not in result
    assert result["distance_m"] > 0
    assert result["mode"] == "walk"
    assert result["from"]["name"] == "A"
    assert result["from"]["id"] == "gers-a"
    assert result["to"]["name"] == "B"
    assert result["to"]["lat"] == TO_LAT
    # The named endpoints reach the handoff, not bare coordinates.
    assert "A" in result["export"]["text"]


def test_route_refs_keep_routes_drive_default(monkeypatch):
    """from_to defaults to walk; route stays drive, refs or not."""
    monkeypatch.setattr(geocode, "resolve_named_place", _ab)
    result = server.route(from_="A", to="B", confirm=True)
    assert "error" not in result
    assert result["mode"] == "drive"


def test_route_resolves_gers_id_refs(monkeypatch):
    from_id = "a" * 32
    to_id = "b" * 32

    def fake_lookup(id_, near_lat=None, near_lon=None):
        lat, lon = (FROM_LAT, FROM_LON) if id_ == from_id else (TO_LAT, TO_LON)
        name = "Origin GERS" if id_ == from_id else "Dest GERS"
        return {
            "id": id_,
            "theme": "places",
            "type": "place",
            "name": name,
            "lat": lat,
            "lon": lon,
            "summary": {},
            "related": {},
        }

    monkeypatch.setattr(server.gers, "gers_lookup", fake_lookup)
    result = server.route(from_=from_id, to=to_id, mode="walk", confirm=True)
    assert "error" not in result
    assert result["from"]["id"] == from_id
    assert result["from"]["name"] == "Origin GERS"
    assert result["to"]["id"] == to_id


def test_route_accepts_coordinate_dict_refs():
    result = server.route(
        from_={"lat": FROM_LAT, "lon": FROM_LON},
        to={"lat": TO_LAT, "lon": TO_LON},
        mode="walk",
        confirm=True,
    )
    assert "error" not in result
    assert result["distance_m"] > 0
    # A bare coordinate endpoint carries nothing to echo — no payload growth.
    assert result["from"] == {"lat": FROM_LAT, "lon": FROM_LON}
    assert result["to"] == {"lat": TO_LAT, "lon": TO_LON}


def test_route_mixing_refs_and_scalars_is_bad_request():
    result = server.route(from_lat=FROM_LAT, from_lon=FROM_LON, to={"lat": TO_LAT, "lon": TO_LON})
    assert result["error"] == "bad_request"
    assert "not both" in result["detail"]
    assert "from_lat" in result["detail"] and "from and to" in result["detail"]


def test_route_with_neither_form_is_bad_request():
    result = server.route()
    assert result["error"] == "bad_request"
    assert "from_lat" in result["detail"] and "from and to" in result["detail"]
    # Half a scalar pair is still the scalar form, and still incomplete.
    partial = server.route(from_lat=FROM_LAT, from_lon=FROM_LON)
    assert partial["error"] == "bad_request"


def test_route_with_one_ref_side_names_the_missing_side():
    result = server.route(from_="A")
    assert result["error"] == "bad_request"
    assert result["field"] == "to"
    assert server.route(to="B")["field"] == "from"


def test_route_ambiguous_name_ref_returns_candidates(monkeypatch):
    def fake_resolve(query):
        if query == "A":
            raise errors.AmbiguousPlace(query, candidates=[{"name": "A1"}, {"name": "A2"}])
        return _ab(query)

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.route(from_="A", to="B", mode="walk", confirm=True)
    assert result["error"] == "ambiguous_place"
    assert result["field"] == "from"
    assert result["candidates"] == [{"name": "A1"}, {"name": "A2"}]


def test_route_far_apart_refs_are_too_far_without_building_a_graph(monkeypatch):
    def fake_resolve(query):
        if "Tokyo" in query:
            return _hit("Tokyo", 35.68, 139.69)
        return _hit("New York", 40.71, -74.01)

    def boom(*_a, **_k):
        raise AssertionError("must not build a continent graph")

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    monkeypatch.setattr(routing, "route", boom)
    result = server.route(from_="Tokyo", to="New York", mode="walk")
    assert result["error"] == "too_far"
    assert "route stays inside one city" in result["detail"]
    assert result["from"]["name"] == "Tokyo"
    assert result["max_distance_m"] == pytest.approx(routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"])


def test_route_bad_dict_ref_is_bad_request():
    result = server.route(from_={"lat": 91.0, "lon": 0.0}, to={"lat": TO_LAT, "lon": TO_LON})
    assert result["error"] == "bad_request"
    assert result["field"] == "from"


def test_route_legacy_scalar_call_is_unchanged(monkeypatch):
    """Regression: the four scalars route exactly as before — no resolver
    touched, no resolved blocks, positional and keyword calls identical."""

    def boom(*_a, **_k):
        raise AssertionError("a coordinate route must not resolve anything")

    monkeypatch.setattr(geocode, "resolve_named_place", boom)
    positional = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", confirm=True)
    keyword = server.route(
        from_lat=FROM_LAT,
        from_lon=FROM_LON,
        to_lat=TO_LAT,
        to_lon=TO_LON,
        mode="walk",
        confirm=True,
    )
    assert positional["from"] == {"lat": FROM_LAT, "lon": FROM_LON}
    assert positional["distance_m"] == keyword["distance_m"]
    assert positional["duration_s"] == keyword["duration_s"]
    assert "resolved" not in positional


def test_route_dispatch_maps_the_published_from_to_the_python_keyword(monkeypatch):
    monkeypatch.setattr(geocode, "resolve_named_place", _ab)
    result = server.placeroot_call(
        "route", {"from": "A", "to": "B", "mode": "walk", "confirm": True}
    )
    assert "error" not in result
    assert result["from"]["name"] == "A"


# --- published schema: every accepted parameter is visible (#328/#395/#419) ---


def test_server_route_schema_publishes_from_to_and_every_other_parameter():
    """The from/to alias patch must not delete parameters from the schema —
    the exact bug #328 and #395 each shipped once. Assert the published
    property set is the function's own signature, `from_` spelled `from`."""
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools["route"].input_schema["properties"]
    accepted = {
        "from" if name == "from_" else name for name in inspect.signature(server.route).parameters
    }
    assert set(props) == accepted
    assert "from_" not in props
    for name in ("from", "to", "from_lat", "from_lon", "to_lat", "to_lon"):
        assert name in props
    for name in ("include_path", "include_elevation", "prefer", "mode", "confirm"):
        assert name in props
    assert props["mode"]["enum"] == ["cycle", "drive", "walk"]
    assert props["prefer"]["enum"] == ["flat"]
    for name in ("from", "to"):
        types = {branch.get("type") for branch in props[name]["anyOf"]}
        assert {"string", "object"} <= types
    # Both forms are optional; the tool decides which pair it got.
    assert not set(tools["route"].input_schema.get("required") or [])


def test_route_published_validator_passes_every_argument_through():
    """Not just the schema: the patched arg model is what the SDK validates a
    tools/call against, so its dump must reach route() with every keyword."""
    tool = server.mcp._tool_manager.get_tool("route")
    kwargs = tool.fn_metadata.validate_arguments(
        {"from": "A", "to": "B", "mode": "walk", "include_path": True, "confirm": True}
    )
    assert set(kwargs) == set(inspect.signature(server.route).parameters)
    assert kwargs["from_"] == "A"
    assert kwargs["to"] == "B"
    assert kwargs["include_path"] is True
    assert kwargs["from_lat"] is None
