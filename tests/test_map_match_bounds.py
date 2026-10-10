"""map_match input validation and the stitch search bound: a trace point
with a NaN/inf/out-of-range coordinate is rejected at the entry (before any
graph build), and match_trace's per-leg Dijkstra stops exploring once the
cheapest open node already exceeds the outlier threshold, so an
unreachable leg no longer settles the whole graph. Hand-built graphs, no
fixture, no network.
"""

import math

import pytest

from placeroot import map_match, routing

# --- Entry validation --------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"lat": math.nan, "lon": -73.9},
        {"lat": 40.7, "lon": math.inf},
        {"lat": -math.inf, "lon": -73.9},
        {"lat": 91.0, "lon": -73.9},
        {"lat": 40.7, "lon": 180.5},
        {"lat": "forty", "lon": -73.9},
        {"lat": None, "lon": -73.9},
        {"lon": -73.9},
    ],
)
@pytest.mark.parametrize("entry", [map_match.snap_trace, map_match.match_trace])
def test_non_finite_or_invalid_coordinates_are_rejected_before_any_graph_build(
    monkeypatch, bad, entry
):
    def no_build(*args, **kwargs):
        raise AssertionError("graph must not be built for an invalid trace")

    monkeypatch.setattr(routing, "_get_or_build_graph", no_build)
    with pytest.raises(ValueError, match="point 1"):
        entry([{"lat": 40.7, "lon": -73.9}, bad], mode="walk")


def test_validated_latlon_accepts_boundary_values():
    assert map_match._validated_latlon(0, {"lat": 90, "lon": -180}) == (90.0, -180.0)
    assert map_match._validated_latlon(0, {"lat": "-90", "lon": "180"}) == (-90.0, 180.0)


# --- Bounded stitch search ---------------------------------------------------


class _CountingGraph(routing.Graph):
    """A Graph that counts how many adjacency lists the search reads, i.e.
    how many nodes it settled."""

    def __init__(self):
        super().__init__()
        self.settled = 0

    @property
    def adjacency(self):
        return self._counting_adjacency

    @adjacency.setter
    def adjacency(self, value):
        self._counting_adjacency = _CountingDict(value, self)


class _CountingDict(dict):
    def __init__(self, data, owner):
        super().__init__(data)
        self._owner = owner

    def __getitem__(self, key):
        self._owner.settled += 1
        return super().__getitem__(key)


def _chain(graph, prefix, n, spacing_m=10.0, lat=40.7, lon0=-73.9):
    """n+1 nodes in an east-west line, undirected edges of spacing_m."""
    deg = spacing_m / (routing._haversine_m(lat, lon0, lat, lon0 + 0.001) / 0.001)
    ids = []
    for i in range(n + 1):
        node = f"{prefix}{i}"
        graph.add_node(node, lat, lon0 + i * deg)
        ids.append(node)
    for a, b in zip(ids, ids[1:]):
        graph.add_edge(a, b, spacing_m, spacing_m)
    return ids


def test_max_cost_stops_the_search_and_reports_unreachable():
    graph = routing.Graph()
    ids = _chain(graph, "n", 20)
    assert routing._dijkstra_path_to_target(graph, ids[0], ids[20], 1.0) is not None
    # 200 m away; a bound under that must come back None, not a path.
    assert routing._dijkstra_path_to_target(graph, ids[0], ids[20], 1.0, max_cost=150.0) is None
    # ... and a bound at or above the true cost still finds it.
    found = routing._dijkstra_path_to_target(graph, ids[0], ids[20], 1.0, max_cost=200.0)
    assert found is not None and found[1] == pytest.approx(200.0)


def test_max_cost_is_in_heap_units_not_meters():
    graph = routing.Graph()
    ids = _chain(graph, "n", 10)  # 100 m
    # speed 2 m/s: 50 s of travel; a 60 s bound admits it, a 40 s one doesn't.
    assert routing._dijkstra_path_to_target(graph, ids[0], ids[10], 2.0, max_cost=60.0)
    assert routing._dijkstra_path_to_target(graph, ids[0], ids[10], 2.0, max_cost=40.0) is None


def test_unreachable_stitch_leg_does_not_settle_the_whole_graph(monkeypatch):
    # Two islands: a short chain the trace sits on, and a huge disconnected
    # chain. Before the bound, the leg to the disconnected point explored
    # every node of the big island before giving up.
    graph = _CountingGraph()
    graph.has_shapes = True
    near = _chain(graph, "a", 2)  # a0 - a1 - a2, 20 m
    far_lat = 40.75
    far = _chain(graph, "b", 2000, lat=far_lat)  # 20 km of nodes, far away
    island_size = len(far)

    def snapped(index, node, matched=True):
        lat, lon = graph.coords[node]
        return map_match.SnappedPoint(
            index=index,
            lat=lat,
            lon=lon,
            matched=matched,
            edge=(node, node),
            fraction=0.0,
            snapped_lat=lat,
            snapped_lon=lon,
            distance_m=1.0,
        )

    # Point 1 anchors at b0 (unreachable from a0); point 2 is back on the
    # near island so a legitimate leg still stitches afterwards.
    trace = [snapped(0, near[0]), snapped(1, far[0]), snapped(2, near[2])]
    monkeypatch.setattr(map_match, "_snap_trace_with_graph", lambda points, mode: (graph, trace))
    monkeypatch.setattr(map_match, "_anchor_node", lambda sp: sp.edge[0])
    monkeypatch.setattr(map_match, "_edge_polyline", lambda g, a, b: [g.coords[a], g.coords[b]])

    points = [{"lat": 40.7, "lon": -73.9}] * 3
    graph.settled = 0  # building the graph touched adjacency too
    result = map_match.match_trace(points, mode="walk")

    assert result.unmatched_indices == [1]
    assert result.matched_length_m == pytest.approx(20.0)
    # The unreachable leg from a0 explores the 3-node near island only.
    assert graph.settled < island_size


def test_drive_graph_bound_is_converted_from_meters_at_the_floor_speed(monkeypatch):
    seen = {}
    real = routing._dijkstra_path_to_target

    def spy(graph, source, target, speed_m_s, max_cost=math.inf):
        seen["max_cost"] = max_cost
        return real(graph, source, target, speed_m_s, max_cost=max_cost)

    monkeypatch.setattr(routing, "_dijkstra_path_to_target", spy)
    graph = routing.Graph()
    graph.has_shapes = True
    graph.weight_is_time = True
    ids = _chain(graph, "d", 1, spacing_m=100.0)

    def snapped(index, node):
        lat, lon = graph.coords[node]
        return map_match.SnappedPoint(
            index=index,
            lat=lat,
            lon=lon,
            matched=True,
            edge=(node, node),
            fraction=0.0,
            snapped_lat=lat,
            snapped_lon=lon,
            distance_m=1.0,
        )

    monkeypatch.setattr(
        map_match,
        "_snap_trace_with_graph",
        lambda points, mode: (graph, [snapped(0, ids[0]), snapped(1, ids[1])]),
    )
    monkeypatch.setattr(map_match, "_anchor_node", lambda sp: sp.edge[0])
    monkeypatch.setattr(map_match, "_edge_polyline", lambda g, a, b: [g.coords[a], g.coords[b]])
    map_match.match_trace([{"lat": 40.7, "lon": -73.9}] * 2, mode="drive")

    straight_m = 100.0
    threshold_m = max(
        map_match.STITCH_OUTLIER_RATIO_K * straight_m,
        straight_m + map_match.STITCH_OUTLIER_MIN_SLACK_M,
    )
    assert seen["max_cost"] == pytest.approx(
        threshold_m / map_match.STITCH_BOUND_FLOOR_SPEED_M_S, rel=1e-3
    )
