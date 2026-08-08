"""Tests for routing.optimize_route()/server.optimize_route() — issue #177.

Uses the same offline transportation fixture (20x20 street grid, 100m
spacing) as test_routing.py/test_route.py; see
scripts/build_routing_fixture.py and tests/_routing_fixture.py. The autouse
fixture in tests/conftest.py points routing.py at the committed fixture for
every test in this file.

Two properties of that fixture do most of the work here:

- Four stops in a straight line along one grid column have exactly one
  optimal open path (walk the line end to end), so a deliberately shuffled
  input has a *provably unique* expected answer, not one of several tied
  ones.
- Its "river" is crossed only by a footway bridge, which drive mode
  excludes — so a pair of stops on opposite banks is genuinely unroutable
  by car, exercising the estimated-leg fallback against real fixture data
  rather than a mock.
"""

import itertools
import math
import random

import pytest

from placeroot import routing, server, tool_profiles

from ._routing_fixture import build_routing_fixture as fx

# Four collinear grid nodes, 300m apart along column i=2. Deliberately
# shuffled: the input order is nothing like the optimal visiting order.
LINE_NODES = [(2, 8), (2, 2), (2, 11), (2, 5)]
LINE_STOPS = [fx.node_latlon(*node) for node in LINE_NODES]
# Starting at input index 1 — grid node (2, 2), the southern end of the line
# — the only optimal open path walks north through (2,5), (2,8), (2,11):
# input indices 1 -> 3 -> 0 -> 2. Any other order doubles back over at
# least one 300m stretch, so this optimum is unique, not one of a tied set.
LINE_START_INDEX = 1
LINE_EXPECTED_ORDER = [1, 3, 0, 2]

# A 2x2 square of grid nodes, 300m on a side, shuffled. The optimal tour is
# the perimeter (1200m); any tour that crosses the diagonal is longer.
SQUARE_NODES = [(2, 2), (5, 5), (2, 5), (5, 2)]
SQUARE_STOPS = [fx.node_latlon(*node) for node in SQUARE_NODES]

# Opposite banks of the fixture's river (only footway bridge crosses it, and
# drive mode excludes footways), plus a third stop so the tour has a real
# ordering decision to make.
WEST_STOP = fx.node_latlon(5, 10)
EAST_STOP = fx.node_latlon(14, 10)
WEST_STOP_2 = fx.node_latlon(5, 12)


def _as_dicts(stops):
    return [{"lat": lat, "lon": lon} for lat, lon in stops]


# --- ordering ------------------------------------------------------------


def test_shuffled_collinear_input_returns_the_unique_optimal_open_path():
    result = routing.optimize_route(
        LINE_STOPS, mode="walk", roundtrip=False, start_index=LINE_START_INDEX
    )
    assert result["order"] == LINE_EXPECTED_ORDER
    # Three 300m legs walked end to end, nothing doubled back.
    assert result["total_distance_m"] == pytest.approx(900.0, rel=0.02)
    assert [leg["from_idx"] for leg in result["legs"]] == LINE_EXPECTED_ORDER[:-1]
    assert [leg["to_idx"] for leg in result["legs"]] == LINE_EXPECTED_ORDER[1:]


def test_roundtrip_and_open_path_differ_in_legs_and_totals():
    open_path = routing.optimize_route(
        LINE_STOPS, mode="walk", roundtrip=False, start_index=LINE_START_INDEX
    )
    cycle = routing.optimize_route(
        LINE_STOPS, mode="walk", roundtrip=True, start_index=LINE_START_INDEX
    )

    # The open path stops at the far end; the cycle pays its way back.
    assert len(open_path["legs"]) == len(LINE_STOPS) - 1
    assert len(cycle["legs"]) == len(LINE_STOPS)
    assert cycle["total_distance_m"] > open_path["total_distance_m"]
    assert cycle["total_distance_m"] == pytest.approx(1800.0, rel=0.02)
    assert open_path["roundtrip"] is False
    assert cycle["roundtrip"] is True

    # The cycle closes: last leg lands back on the start, which is never
    # repeated inside "order".
    assert cycle["order"][0] == LINE_START_INDEX
    assert cycle["legs"][-1]["to_idx"] == LINE_START_INDEX
    assert cycle["order"].count(LINE_START_INDEX) == 1


def test_roundtrip_over_a_square_takes_the_perimeter_not_a_diagonal():
    result = routing.optimize_route(SQUARE_STOPS, mode="walk", roundtrip=True)
    # 4 x 300m perimeter; a tour using a diagonal would be ~1400m+.
    assert result["total_distance_m"] == pytest.approx(1200.0, rel=0.02)
    order = result["order"]
    assert sorted(order) == [0, 1, 2, 3]
    # Every consecutive pair (including the closing one) is a side of the
    # square, i.e. exactly 300m — never the ~424m diagonal.
    for leg in result["legs"]:
        assert leg["distance_m"] == pytest.approx(300.0, rel=0.02)


def test_order_always_starts_at_start_index():
    for start_index in range(len(LINE_STOPS)):
        result = routing.optimize_route(
            LINE_STOPS, mode="walk", roundtrip=False, start_index=start_index
        )
        assert result["order"][0] == start_index
        assert sorted(result["order"]) == list(range(len(LINE_STOPS)))


def test_result_carries_no_geometry():
    result = routing.optimize_route(LINE_STOPS, mode="walk")
    assert set(result) <= {
        "order", "legs", "total_distance_m", "total_duration_s", "mode", "roundtrip",
        "truncated", "note", "estimated",
    }
    for leg in result["legs"]:
        assert set(leg) <= {"from_idx", "to_idx", "distance_m", "duration_s", "estimated"}


def test_totals_are_the_sum_of_the_legs():
    result = routing.optimize_route(SQUARE_STOPS, mode="drive", roundtrip=True)
    assert result["total_distance_m"] == pytest.approx(
        sum(leg["distance_m"] for leg in result["legs"]), rel=1e-6
    )
    assert result["total_duration_s"] == pytest.approx(
        sum(leg["duration_s"] for leg in result["legs"]), rel=1e-6
    )


# --- one graph extraction, not one per pair ------------------------------


def test_one_graph_extraction_covers_every_stop(monkeypatch):
    """The design's whole point: an n-stop tour must not cost O(n^2)
    upstream scans. Four stops = 12 ordered pairs; this must still be a
    single extraction."""
    routing.clear_graph_cache()
    calls = []
    real_build_graph = routing.build_graph

    def counting_build_graph(lat, lon, radius_m, **kwargs):
        calls.append((lat, lon, radius_m))
        return real_build_graph(lat, lon, radius_m, **kwargs)

    monkeypatch.setattr(routing, "build_graph", counting_build_graph)
    routing.optimize_route(SQUARE_STOPS, mode="walk", roundtrip=True)
    assert len(calls) == 1


def test_extraction_circle_contains_every_stop():
    center_lat, center_lon, radius_m = routing._stops_extraction_geometry(
        SQUARE_STOPS, "walk"
    )
    for lat, lon in SQUARE_STOPS:
        assert routing._haversine_m(center_lat, center_lon, lat, lon) <= radius_m


# --- exactness and asymmetry (solver-level) ------------------------------


def _brute_force(cost, start_index, roundtrip):
    n = len(cost)
    others = [i for i in range(n) if i != start_index]
    best = None
    for perm in itertools.permutations(others):
        order = (start_index, *perm)
        total = sum(cost[a][b] for a, b in zip(order, order[1:]))
        if roundtrip:
            total += cost[order[-1]][start_index]
        if best is None or total < best[0] - 1e-9:
            best = (total, order)
    return best


@pytest.mark.parametrize("n", [2, 3, 5, 7])
@pytest.mark.parametrize("roundtrip", [True, False])
def test_solve_tsp_matches_brute_force_on_random_asymmetric_matrices(n, roundtrip):
    """Held-Karp must be *exact*, and must not assume cost[i][j] ==
    cost[j][i] — drive one-ways make the real matrix directed."""
    rng = random.Random(f"{n}-{roundtrip}")
    for _ in range(20):
        cost = [[0.0 if i == j else rng.uniform(1.0, 100.0) for j in range(n)]
                for i in range(n)]
        start_index = rng.randrange(n)
        order = routing.solve_tsp(cost, start_index=start_index, roundtrip=roundtrip)
        total = sum(cost[a][b] for a, b in zip(order, order[1:]))
        if roundtrip:
            total += cost[order[-1]][start_index]
        expected_total, _ = _brute_force(cost, start_index, roundtrip)
        assert total == pytest.approx(expected_total, rel=1e-9)
        assert order[0] == start_index
        assert sorted(order) == list(range(n))


def test_solve_tsp_uses_the_asymmetric_cell_it_is_given():
    """A matrix where going 0->1->2 is cheap but 0->2->1 is not, purely
    because of the directed cells — a symmetric-matrix solver would pick
    either."""
    inf = 1000.0
    cost = [
        [0.0, 1.0, inf],
        [inf, 0.0, 1.0],
        [1.0, inf, 0.0],
    ]
    assert routing.solve_tsp(cost, start_index=0, roundtrip=True) == [0, 1, 2]


def test_solve_tsp_breaks_mirror_ties_deterministically():
    """A symmetric square: the perimeter tour and its mirror cost the same,
    so the lexicographically smallest one must come back every time."""
    cost = [
        [0.0, 1.0, 2.0, 1.0],
        [1.0, 0.0, 1.0, 2.0],
        [2.0, 1.0, 0.0, 1.0],
        [1.0, 2.0, 1.0, 0.0],
    ]
    assert routing.solve_tsp(cost, start_index=0, roundtrip=True) == [0, 1, 2, 3]


def test_cost_matrix_is_directed_for_a_one_way_graph():
    """A hand-built 3-node graph with one directed edge: the routed matrix
    must come back asymmetric rather than silently mirrored."""
    graph = routing.Graph()
    coords = {"a": (40.0, -74.0), "b": (40.0, -73.999), "c": (40.001, -73.999)}
    for node_id, (lat, lon) in coords.items():
        graph.add_node(node_id, lat, lon)
    graph.add_edge("a", "b", 100.0, 100.0, directed=True)  # one-way a -> b
    graph.add_edge("b", "c", 100.0, 100.0)
    graph.add_edge("c", "a", 100.0, 100.0)

    points = [coords["a"], coords["b"], coords["c"]]
    time_m, dist_m, estimated = routing._cost_matrices(
        graph, ["a", "b", "c"], points, "walk"
    )
    assert not estimated
    assert dist_m[0][1] == pytest.approx(100.0)  # straight down the one-way
    assert dist_m[1][0] == pytest.approx(200.0)  # b -> c -> a, the long way round
    assert time_m[0][1] < time_m[1][0]


# --- unroutable pairs ----------------------------------------------------


def test_unroutable_pair_is_estimated_and_flagged_not_a_crash():
    """Drive mode can't use the fixture's footway bridge, so the two banks
    of its river are genuinely disconnected by car."""
    result = routing.optimize_route(
        [WEST_STOP, EAST_STOP, WEST_STOP_2], mode="drive", roundtrip=True
    )
    assert "error" not in result
    assert result["estimated"] is True
    assert "estimated" in result["note"]

    estimated_legs = [leg for leg in result["legs"] if leg.get("estimated")]
    assert estimated_legs, "crossing the river by car should have been estimated"
    for leg in estimated_legs:
        # One end of every estimated leg is the far bank (input index 1).
        assert 1 in (leg["from_idx"], leg["to_idx"])
        assert leg["distance_m"] > 0
        assert leg["duration_s"] > 0
    # Legs that stay on one bank are real routes, not estimates.
    same_bank = [
        leg for leg in result["legs"]
        if {leg["from_idx"], leg["to_idx"]} == {0, 2}
    ]
    assert same_bank and all("estimated" not in leg for leg in same_bank)


def test_walk_mode_crosses_the_bridge_so_nothing_is_estimated():
    """Same three stops on foot: the footway bridge is walkable, so the
    same input that estimates by car routes fully on foot."""
    result = routing.optimize_route(
        [WEST_STOP, EAST_STOP, WEST_STOP_2], mode="walk", roundtrip=True
    )
    assert "estimated" not in result
    assert all("estimated" not in leg for leg in result["legs"])


def test_estimated_cell_is_straight_line_times_the_detour_factor():
    a, b = WEST_STOP, EAST_STOP
    seconds, distance_m = routing._estimated_cell(a, b, "walk")
    straight_m = routing._haversine_m(*a, *b)
    assert distance_m == pytest.approx(straight_m * routing.UNROUTABLE_DETOUR_FACTOR)
    assert seconds == pytest.approx(distance_m / routing.DEFAULT_SPEED_M_S)


# Two stops ~850m apart, and two hand-built graphs standing in for the base
# and retry extractions: at the base radius each stop has its own 5-node
# street fragment (big enough to be a snappable component) with nothing
# connecting them; the wider extraction also holds the connecting chain —
# the "road that bulges just outside the base circle" scenario.
RETRY_STOP_A = (40.0, -74.0)
RETRY_STOP_B = (40.0, -73.99)


def _cluster(graph, prefix, lat, lon):
    """A 5-node chained street fragment starting at (lat, lon)."""
    ids = [f"{prefix}{i}" for i in range(5)]
    for i, node_id in enumerate(ids):
        graph.add_node(node_id, lat, lon + i * 0.0001)
    for a, b in zip(ids, ids[1:]):
        graph.add_edge(a, b, 10.0, 10.0)
    return ids


def _disconnected_and_connected_graphs():
    """(graph without the connector, graph with it), same stops snappable."""
    small = routing.Graph()
    a_ids = _cluster(small, "a", *RETRY_STOP_A)
    b_ids = _cluster(small, "b", *RETRY_STOP_B)

    big = routing.Graph()
    _cluster(big, "a", *RETRY_STOP_A)
    _cluster(big, "b", *RETRY_STOP_B)
    big.add_edge(a_ids[-1], b_ids[0], 850.0, 850.0)
    return small, big


def _patched_extractions(monkeypatch, graphs):
    """Serve `graphs` from _get_or_build_graph in order; record the radii."""
    radii = []

    def fake_get_or_build_graph(lat, lon, radius_m, mode, **kwargs):
        radii.append(radius_m)
        return graphs[min(len(radii), len(graphs)) - 1]

    monkeypatch.setattr(routing, "_get_or_build_graph", fake_get_or_build_graph)
    return radii


def test_unreachable_pair_retries_at_the_wider_radius_before_estimating(monkeypatch):
    """Regression: the widen-and-retry loop must retry on *connectivity*, not
    just on snapping — exactly as _shortest_path does. Two stops whose only
    connecting road bulges just outside the base circle snap fine at the
    base radius, so without this the retry never fired and the pair came
    back as straight-line "estimated" legs that route() would have routed
    via its own 1.6x retry."""
    small, big = _disconnected_and_connected_graphs()
    radii = _patched_extractions(monkeypatch, [small, big])

    result = routing.optimize_route([RETRY_STOP_A, RETRY_STOP_B], mode="walk")
    assert "estimated" not in result
    assert all("estimated" not in leg for leg in result["legs"])
    assert len(radii) == 2
    assert radii[1] == pytest.approx(radii[0] * routing.ROUTE_RADIUS_RETRY_FACTOR)


def test_snap_failure_at_the_retry_radius_falls_back_to_the_estimated_matrix(monkeypatch):
    """The wider extraction can lose a stop's fragment to the segment cap;
    that must fall back to the base radius's estimated answer, never raise
    NoGraphNearby for a stop that already snapped."""
    small, _ = _disconnected_and_connected_graphs()
    empty_near_b = routing.Graph()
    _cluster(empty_near_b, "a", *RETRY_STOP_A)  # stop B has nothing to snap to
    radii = _patched_extractions(monkeypatch, [small, empty_near_b])

    result = routing.optimize_route([RETRY_STOP_A, RETRY_STOP_B], mode="walk")
    assert len(radii) == 2
    assert result["estimated"] is True
    assert any(leg.get("estimated") for leg in result["legs"])


def test_still_unreachable_at_the_retry_radius_is_estimated_not_an_error(monkeypatch):
    """A genuinely disconnected pair stays an estimate after both radii."""
    small, _ = _disconnected_and_connected_graphs()
    radii = _patched_extractions(monkeypatch, [small, small])

    result = routing.optimize_route([RETRY_STOP_A, RETRY_STOP_B], mode="walk")
    assert len(radii) == 2
    assert result["estimated"] is True


# --- validation and caps -------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 11])
def test_stop_count_outside_the_range_is_a_bad_request(count):
    result = server.optimize_route(_as_dicts([fx.node_latlon(2, 2)] * count))
    assert result["error"] == "bad_request"
    assert "between 2 and 10" in result["detail"]


def test_routing_layer_rejects_the_stop_count_too():
    with pytest.raises(ValueError, match="between 2 and 10"):
        routing.optimize_route([fx.node_latlon(2, 2)])


def test_out_of_range_coordinate_error_names_the_stop_index():
    stops = _as_dicts(LINE_STOPS)
    stops[2] = {"lat": 91.0, "lon": 0.0}
    result = server.optimize_route(stops)
    assert result["error"] == "bad_request"
    assert result["detail"].startswith("stops[2]: ")


def test_missing_coordinate_error_names_the_stop_index():
    stops = _as_dicts(LINE_STOPS)
    stops[1] = {"lat": 40.0}
    result = server.optimize_route(stops)
    assert result["error"] == "bad_request"
    assert result["detail"].startswith("stops[1]: ")


def test_non_numeric_coordinate_error_names_the_stop_index():
    stops = _as_dicts(LINE_STOPS)
    stops[3] = {"lat": "north", "lon": 0.0}
    result = server.optimize_route(stops)
    assert result["error"] == "bad_request"
    assert result["detail"].startswith("stops[3]: ")


@pytest.mark.parametrize("start_index", [-1, 4, 99])
def test_start_index_out_of_range_is_a_bad_request(start_index):
    result = server.optimize_route(_as_dicts(LINE_STOPS), start_index=start_index)
    assert result["error"] == "bad_request"
    assert "start_index" in result["detail"]


def test_unsupported_mode_is_structured():
    result = server.optimize_route(_as_dicts(LINE_STOPS), mode="teleport")
    assert result["error"] == "unsupported_mode"
    assert "walk" in result["supported"]


def test_stops_beyond_the_mode_cap_raise_route_too_long():
    far = [(0.0, 0.0), (0.05, 0.0), (10.0, 10.0)]
    with pytest.raises(routing.RouteTooLong):
        routing.optimize_route(far, mode="walk")


def test_route_too_long_reports_the_derived_per_mode_cap():
    """The cap is route()'s own derived one, reused unchanged — not a
    second, independently-chosen number."""
    far = [(0.0, 0.0), (0.0, 0.2)]  # ~22km apart, past walk's 7.5km cap
    result = server.optimize_route(_as_dicts(far), mode="walk")
    assert result["error"] == "route_too_long"
    assert result["max_distance_m"] == pytest.approx(
        routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"]
    )


# --- extraction geometry -------------------------------------------------
#
# The extraction circle has to contain every stop with SNAP_RADIUS_M to
# spare, or a stop that sits perfectly well on a street simply isn't in the
# extracted graph and the call dies with NoGraphNearby. These are pure
# geometry — no fixture data needed.


def _extraction_margins(stops, mode):
    """Slack, in meters, between each stop and the extraction circle's edge."""
    center_lat, center_lon, radius_m = routing._stops_extraction_geometry(stops, mode)
    return [
        radius_m - routing._haversine_m(center_lat, center_lon, lat, lon)
        for lat, lon in stops
    ]


def test_equilateral_stop_triple_stays_inside_the_extraction_circle():
    """Regression: three stops on an equilateral triangle, each pair 6.5km
    apart and so comfortably inside walk's 7.5km cap.

    Centering on the *diametral pair's midpoint* (the old behaviour) leaves
    the third vertex sqrt(3)/2 = 0.866 x span from that midpoint while the
    radius only covers 0.625 x span + 300m — the third stop fell ~1.2km
    outside the extracted graph and the call failed with
    NoGraphNearby: stops[2] even though that coordinate snaps fine on its
    own. (Jung's theorem's d/sqrt(3) bound is about the enclosing circle's
    circumcenter, not the diametral midpoint.)
    """
    stops = [(37.730000, -122.460000), (37.730000, -122.386173), (37.780567, -122.423086)]
    sides = [
        routing._haversine_m(*a, *b)
        for a, b in itertools.combinations(stops, 2)
    ]
    assert min(sides) == pytest.approx(max(sides), rel=1e-3)  # equilateral
    assert max(sides) < routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"]  # inside the cap

    margins = _extraction_margins(stops, "walk")
    assert min(margins) >= routing.SNAP_RADIUS_M


@pytest.mark.parametrize("mode", ["walk", "cycle", "drive"])
def test_every_stop_sits_inside_the_extraction_circle_for_random_sets(mode):
    """Property: whatever the stop set, containment holds by construction.

    Random sets, sized and shaped so they stay inside the mode's cap; any
    set the cap rejects is skipped rather than asserted about.
    """
    rng = random.Random(1177)
    radius_cap_m = routing.ROUTE_MAX_ENCLOSING_RADIUS_M[mode]
    checked = 0
    for _ in range(200):
        lat0 = rng.uniform(-60.0, 60.0)
        lon0 = rng.uniform(-180.0, 180.0)
        spread_m = rng.uniform(50.0, radius_cap_m)
        stops = []
        for _ in range(rng.randint(2, routing.OPTIMIZE_MAX_STOPS)):
            north_m = rng.uniform(-spread_m, spread_m)
            east_m = rng.uniform(-spread_m, spread_m)
            lat = lat0 + north_m / 111_320.0
            lon = lon0 + east_m / (111_320.0 * math.cos(math.radians(lat0)))
            stops.append((lat, ((lon + 180.0) % 360.0) - 180.0))
        try:
            margins = _extraction_margins(stops, mode)
        except routing.RouteTooLong:
            continue
        checked += 1
        assert min(margins) >= routing.SNAP_RADIUS_M - 1e-6
    assert checked > 50  # the sampling actually exercised the accepted region


def test_stops_straddling_the_antimeridian_are_contained_too():
    """The seam is where a naive bbox center lands on the far side of the
    globe; the enclosing-circle center unwraps longitudes first."""
    stops = [(0.5, 179.98), (0.5, -179.98), (0.52, 179.999)]
    margins = _extraction_margins(stops, "walk")
    assert min(margins) >= routing.SNAP_RADIUS_M


def test_two_stops_reproduce_route_s_own_extraction_geometry():
    """The n-stop rule generalizes route()'s, so for n=2 it must agree with
    it exactly — same center, same radius, same cap."""
    a, b = (40.7000, -73.9900), (40.7250, -73.9600)
    center_lat, center_lon, radius_m = routing._stops_extraction_geometry([a, b], "walk")
    span_m = routing._haversine_m(*a, *b)
    expected_center = routing._midpoint(*a, *b)
    assert center_lat == pytest.approx(expected_center[0], abs=1e-6)
    assert center_lon == pytest.approx(expected_center[1], abs=1e-6)
    assert radius_m == pytest.approx(
        span_m / 2.0 * routing.RADIUS_BUFFER + routing.SNAP_RADIUS_M, rel=1e-6
    )


def test_a_pair_just_inside_the_advertised_cap_is_accepted():
    """Advertised == enforced: the straight-line cap route() publishes has
    to remain reachable through optimize_route's n-stop check."""
    cap_m = routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"]
    lat = 40.7
    stops = [(lat, -73.99), (lat + (cap_m * 0.999) / 111_320.0, -73.99)]
    _, _, radius_m = routing._stops_extraction_geometry(stops, "walk")
    assert radius_m <= routing.MODE_CONFIG["walk"]["max_radius_m"]


def _isoceles_triple(base_m, apex_angle_deg, lat0=52.0, lon0=13.4):
    """Isoceles triple: `base_m` wide, apex angle `apex_angle_deg`, apex north.

    Laid out in local meters so the shape is exact; the caller re-measures
    with haversine.
    """
    half = base_m / 2.0
    height_m = half / math.tan(math.radians(apex_angle_deg / 2.0))
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    return [
        (lat0, lon0 - half / m_per_deg_lon),
        (lat0, lon0 + half / m_per_deg_lon),
        (lat0 + height_m / 111_320.0, lon0),
    ]


def test_a_triple_spanning_just_under_the_advertised_cap_is_accepted():
    """Regression (#177 sweep): acceptance is on SPAN, not enclosing radius.

    A 7.5km base with a 70-degree apex spans 7.5km — inside walk's 7520m
    straight-line cap, so route() would route every one of its three pairs
    happily. But its smallest enclosing circle has radius ~3990m, over the
    3760m *radius* cap, because for n >= 3 the enclosing radius runs to
    span / sqrt(3), not span / 2. Capping the enclosing radius (the first
    cut of the containment fix) rejected this set outright with a
    RouteTooLong quoting a 7980m "enclosing-circle diameter" — a number
    larger than any actual separation between two of the stops.

    So: accepted, contained with SNAP_RADIUS_M to spare, and the resulting
    extraction radius stays inside the widened n >= 3 bound even though it
    exceeds the pair-derived one.
    """
    stops = _isoceles_triple(7500.0, 70.0)
    span_m = max(
        routing._haversine_m(*a, *b) for a, b in itertools.combinations(stops, 2)
    )
    assert span_m < routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"]  # inside the cap

    center_lat, center_lon, radius_m = routing._stops_extraction_geometry(stops, "walk")
    enclosing_m = max(
        routing._haversine_m(center_lat, center_lon, lat, lon) for lat, lon in stops
    )
    assert enclosing_m > routing.ROUTE_MAX_ENCLOSING_RADIUS_M["walk"]  # the old reject
    assert min(_extraction_margins(stops, "walk")) >= routing.SNAP_RADIUS_M
    # Needs the widened n >= 3 extraction bound, and stays inside it.
    assert radius_m > routing.MODE_CONFIG["walk"]["max_radius_m"]
    assert radius_m <= routing.STOPS_MAX_EXTRACTION_RADIUS_M["walk"]


def test_the_widened_extraction_bound_covers_the_whole_accepted_span_range():
    """The Jung-derived bound is the real ceiling: the worst case for n >= 3
    is an equilateral triple at exactly the span cap, and even that fits."""
    for mode in ("walk", "cycle", "drive"):
        cap_m = routing.ROUTE_MAX_STRAIGHT_LINE_M[mode]
        worst_enclosing_m = cap_m / math.sqrt(3.0)
        needed_m = worst_enclosing_m * routing.RADIUS_BUFFER + routing.SNAP_RADIUS_M
        assert needed_m <= routing.STOPS_MAX_EXTRACTION_RADIUS_M[mode]


def _geodesic_destination(lat, lon, bearing_deg, distance_m):
    """Great-circle destination point — exact spherical, not projected."""
    earth_r = 6_371_000.0
    bearing = math.radians(bearing_deg)
    d = distance_m / earth_r
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def test_a_near_cap_equilateral_triple_at_high_latitude_is_accepted():
    """Regression: the Jung-bound headroom must be latitude-aware.

    Three stops equally spaced on a geodesic circle at 82N, every pair
    ~95.4km apart — just under drive's straight-line cap, so route() would
    happily route each pair. The equirectangular projection inside
    _minimum_enclosing_center scales longitudes by cos(mid-lat) while the
    true scale at each stop is cos(lat), an error of ~ tan(lat) *
    lat_extent/2 (~1.7% here) — so the chosen center's re-measured haversine
    radius lands ~1.3% over the planar span/sqrt(3) bound, past the flat 1%
    JUNG_BOUND_HEADROOM. Under the old constant cap the safety net in
    _stops_extraction_geometry rejected this set route_too_long; the
    latitude-aware cap in _stops_radius_cap_m accepts it.
    """
    circumradius_m = 55_093.5
    stops = [
        _geodesic_destination(82.0, 20.0, 60.0 + 120.0 * k, circumradius_m)
        for k in range(3)
    ]
    span_m = max(
        routing._haversine_m(*a, *b) for a, b in itertools.combinations(stops, 2)
    )
    assert span_m < routing.ROUTE_MAX_STRAIGHT_LINE_M["drive"]  # inside the cap

    # The distortion really does overshoot the old constant cap: this is the
    # case the old code rejected, not a set that fit all along.
    _, _, radius_m = routing._stops_extraction_geometry(stops, "drive")
    assert radius_m > routing.STOPS_MAX_EXTRACTION_RADIUS_M["drive"]
    assert radius_m <= routing._stops_radius_cap_m(stops, "drive")
    assert min(_extraction_margins(stops, "drive")) >= routing.SNAP_RADIUS_M


def test_the_latitude_aware_cap_reduces_to_the_constant_at_low_latitude():
    """Near the equator the distortion term is negligible: the per-set cap
    is the published constant plus a vanishing sliver, and it is clamped at
    the sqrt(3) ceiling (center-on-a-stop coverage) toward the poles rather
    than growing without bound with tan(lat)."""
    equator = [(0.0, 0.0), (0.01, 0.01), (-0.01, 0.02)]
    assert routing._stops_radius_cap_m(equator, "walk") == pytest.approx(
        routing.STOPS_MAX_EXTRACTION_RADIUS_M["walk"], rel=1e-3
    )

    polar = [(89.99, 0.0), (89.2, 10.0), (89.5, -120.0)]
    span_cap_m = routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"]
    ceiling_m = span_cap_m * routing.RADIUS_BUFFER + routing.SNAP_RADIUS_M
    assert routing._stops_radius_cap_m(polar, "walk") == pytest.approx(ceiling_m)


def test_a_stop_set_over_the_span_cap_is_still_rejected_honestly():
    """The cap is enforced, and the error quotes a distance that two of the
    stops really are apart — not a derived diameter nothing matches."""
    stops = _isoceles_triple(routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"] * 1.2, 70.0)
    with pytest.raises(routing.RouteTooLong) as excinfo:
        routing._stops_extraction_geometry(stops, "walk")
    span_m = max(
        routing._haversine_m(*a, *b) for a, b in itertools.combinations(stops, 2)
    )
    assert excinfo.value.distance_m == pytest.approx(span_m, rel=1e-9)


def test_isoceles_triple_on_the_fixture_grid_routes_end_to_end():
    """The same 70-degree shape at fixture scale, through the real code path."""
    stops = [fx.node_latlon(2, 2), fx.node_latlon(16, 2), fx.node_latlon(9, 12)]
    result = routing.optimize_route(stops, mode="walk", roundtrip=True)
    assert sorted(result["order"]) == [0, 1, 2]
    assert result["total_distance_m"] > 0


def test_equilateral_triple_on_the_fixture_grid_routes_end_to_end():
    """The same shape at fixture scale, through the real code path."""
    stops = [fx.node_latlon(2, 2), fx.node_latlon(14, 2), fx.node_latlon(8, 12)]
    result = routing.optimize_route(stops, mode="walk", roundtrip=True)
    assert sorted(result["order"]) == [0, 1, 2]
    assert result["total_distance_m"] > 0


def test_stop_with_no_street_nearby_names_its_index():
    stops = list(LINE_STOPS)
    # Middle of the ocean-ish: inside the extraction circle, nowhere near a
    # fixture segment.
    stops[2] = (fx.ORIGIN_LAT + 0.02, fx.ORIGIN_LON + 0.02)
    with pytest.raises(routing.NoGraphNearby) as excinfo:
        routing.optimize_route(stops, mode="walk")
    assert "stops[2]" in excinfo.value.detail


def test_no_graph_nearby_is_structured_at_the_tool_boundary():
    stops = list(LINE_STOPS)
    stops[2] = (fx.ORIGIN_LAT + 0.02, fx.ORIGIN_LON + 0.02)
    result = server.optimize_route(_as_dicts(stops), mode="walk")
    assert result["error"] == "no_graph_nearby"
    assert "stops[2]" in result["detail"]


# --- budget --------------------------------------------------------------


def test_response_fits_the_default_token_budget():
    stops = [fx.node_latlon(2, j) for j in range(0, 20, 2)]  # 10 stops, the max
    result = routing.optimize_route(stops, mode="walk", roundtrip=True)
    assert len(result["legs"]) == 10
    from placeroot import budget

    assert budget.estimate_tokens(result) <= budget.token_budget()
    assert "truncated" not in result


def test_a_tiny_budget_drops_the_legs_and_says_so(monkeypatch):
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "20")
    result = routing.optimize_route(SQUARE_STOPS, mode="walk", roundtrip=True)
    assert "legs" not in result
    assert result["truncated"] is True
    assert "token budget" in result["note"]
    # The answer itself survives.
    assert sorted(result["order"]) == [0, 1, 2, 3]
    assert result["total_distance_m"] > 0


def test_the_dropped_legs_note_is_paid_for_out_of_the_budget(monkeypatch):
    """Regression: the fit check has to price the response it actually
    returns.

    Dropping the legs adds a ~30-token explanatory note plus the
    "truncated" flag. Estimating the bare payload and appending those
    afterwards overran the very budget just checked — at
    PLACEROOT_TOKEN_BUDGET=110 the answer came back at ~130 tokens.
    """
    from placeroot import budget

    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "110")
    stops = [fx.node_latlon(2, j) for j in range(0, 20, 2)]  # 10 stops, the max
    result = routing.optimize_route(stops, mode="walk", roundtrip=True)
    assert "legs" not in result  # the budget did bind
    assert "token budget" in result["note"]
    assert budget.estimate_tokens(result) <= budget.token_budget()


# --- registration --------------------------------------------------------


def test_optimize_route_is_in_the_routing_profile():
    assert "optimize_route" in tool_profiles.PROFILES["routing"]
