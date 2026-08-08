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


# --- registration --------------------------------------------------------


def test_optimize_route_is_in_the_routing_profile():
    assert "optimize_route" in tool_profiles.PROFILES["routing"]
