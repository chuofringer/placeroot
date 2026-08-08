import pytest

from placeroot import routing, server

from ._routing_fixture import build_routing_fixture as fx
from .conftest import FIXTURE_PATH as PLACES_FIXTURE_PATH

ORIGIN_LAT = fx.ORIGIN_LAT
ORIGIN_LON = fx.ORIGIN_LON

# Radius (m) generous enough to pull the whole 20x20 grid into one query.
WHOLE_GRID_RADIUS_M = 3000

# Grid nodes, plus the issue #37 fixture additions that also fall inside
# WHOLE_GRID_RADIUS_M of ORIGIN: the interior-connector crossing (5 nodes:
# x_a0, x_a1, x_b0, x_b1, x_cross), the isolated 2-node fragment
# (frag_0, frag_1), and the issue #38 one-way pair (ow_a, ow_b). See
# build_routing_fixture.py.
# ... and the switchback spur's far end (sw_end), 1 more node.
EXPECTED_NODE_COUNT = fx.GRID_N * fx.GRID_N + 5 + 2 + 2 + 1
# All grid edges (horizontal + vertical + diagonal) minus the one motorway
# shortcut (excluded by the walkable filter), plus the crossing's 4 split
# edges, the isolated fragment's 1 edge, the one-way pair's 1 edge (same
# #37/#38 additions as above), and the switchback spur's 1 edge.
EXPECTED_EDGE_COUNT = len(fx.build_edges()) - 1 + 4 + 1 + 1 + 1


def test_bbox_filter_sql_unchanged_for_non_crossing_box():
    """Issue #42: routing.py carries its own duplicate of the antimeridian
    fix (see its _bbox_around/_bbox_filter_sql docstrings — unifying with
    overture.py's copy is #40's job, not this one's). The non-crossing case
    must stay byte-identical to the filter build_graph used before."""
    filter_sql, params = routing._bbox_filter_sql(-74.0, 40.0, -73.0, 41.0)
    assert filter_sql == (
        "bbox.xmax >= $xmin AND bbox.xmin <= $xmax"
        " AND bbox.ymax >= $ymin AND bbox.ymin <= $ymax"
    )
    assert params == {"xmin": -74.0, "ymin": 40.0, "xmax": -73.0, "ymax": 41.0}


def test_bbox_filter_sql_wraps_at_antimeridian():
    filter_sql, params = routing._bbox_filter_sql(179.9, 9.0, 180.4, 11.0)
    assert "OR" in filter_sql
    assert params["xmin"] == pytest.approx(179.9)
    assert params["xmax"] == pytest.approx(180.4 - 360.0)


def test_bbox_around_clamps_latitude_to_poles():
    xmin, ymin, xmax, ymax = routing._bbox_around(89.9, 15.0, 50_000)
    assert ymax == 90.0


def _path_length(*nodes: tuple[int, int]) -> float:
    """Ground-truth length (m) of a path through named grid nodes, via the
    same haversine + coordinate helpers the fixture and routing module use."""
    total = 0.0
    for a, b in zip(nodes, nodes[1:]):
        lat1, lon1 = fx.node_latlon(*a)
        lat2, lon2 = fx.node_latlon(*b)
        total += routing._haversine_m(lat1, lon1, lat2, lon2)
    return total


def test_graph_builds_with_expected_node_and_edge_counts():
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M)
    assert graph.node_count() == EXPECTED_NODE_COUNT
    assert graph.edge_count() == EXPECTED_EDGE_COUNT


def test_walkable_filter_excludes_motorway_shortcut():
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M)
    a, b = fx.node_id(*fx.SHORTCUT["from"]), fx.node_id(*fx.SHORTCUT["to"])
    neighbors_of_a = {n for n, _, _ in graph.adjacency[a]}
    assert b not in neighbors_of_a


def test_dijkstra_matches_manhattan_grid_distance():
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M)
    source = fx.node_id(2, 2)
    dest = fx.node_id(2, 5)
    expected_m = _path_length((2, 2), (2, 3), (2, 4), (2, 5))

    dist = routing.dijkstra(graph, source, max_seconds=expected_m + 1, speed_m_s=1.0)

    assert dest in dist
    assert dist[dest] == pytest.approx(expected_m, rel=1e-6)


def test_dijkstra_takes_the_shortcut_diagonal_where_available():
    """(0,0)-(1,1) is a diagonal edge — shorter than the two-cardinal-edge route."""
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M)
    source = fx.node_id(0, 0)
    dest = fx.node_id(1, 1)
    diagonal_m = _path_length((0, 0), (1, 1))
    cardinal_m = _path_length((0, 0), (1, 0), (1, 1))
    assert diagonal_m < cardinal_m

    dist = routing.dijkstra(graph, source, max_seconds=cardinal_m, speed_m_s=1.0)
    assert dist[dest] == pytest.approx(diagonal_m, rel=1e-6)


def test_isochrone_excludes_across_river_nodes_except_via_bridge():
    """(9,5) and (10,5) are 100m apart geometrically but on opposite riverbanks;
    the only crossing is the bridge at row BRIDGE_J. Reaching (10,5) must cost
    the detour distance, not the 100m direct distance."""
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M)
    source = fx.node_id(9, 5)
    dest_across = fx.node_id(10, 5)

    detour_m = _path_length(
        (9, 5), (9, fx.BRIDGE_J), (10, fx.BRIDGE_J), (10, 5)
    )
    direct_m = _path_length((9, 5), (10, 5))
    assert detour_m > direct_m * 5  # sanity: the detour is much longer than "next door"

    # A budget that covers the direct 100m hop but not the real detour must
    # NOT reach across the river.
    short_dist = routing.dijkstra(graph, source, max_seconds=direct_m * 2, speed_m_s=1.0)
    assert dest_across not in short_dist

    # A budget covering the real detour must reach it, at the detour distance.
    long_dist = routing.dijkstra(graph, source, max_seconds=detour_m + 1, speed_m_s=1.0)
    assert dest_across in long_dist
    assert long_dist[dest_across] == pytest.approx(detour_m, rel=1e-6)


def test_isochrone_polygon_is_valid_and_within_point_budget():
    lat, lon = fx.node_latlon(10, 10)
    result = routing.isochrone(lat, lon, minutes=20)
    polygon = result["polygon"]
    assert polygon["type"] == "Polygon"
    ring = polygon["coordinates"][0]
    assert len(ring) >= 4
    assert ring[0] == ring[-1]  # closed ring
    assert result["stats"]["reachable_nodes"] > 0
    assert result["stats"]["area_km2"] >= 0
    # 20 minutes of walking over the grid reaches well more than
    # CONCAVE_MIN_NODES nodes, so the concave boundary trace applies (#36).
    assert result["polygon_method"] == "concave_boundary"


def test_isochrone_stats_scale_with_minutes():
    lat, lon = fx.node_latlon(10, 10)
    small = routing.isochrone(lat, lon, minutes=2)
    big = routing.isochrone(lat, lon, minutes=15)
    assert big["stats"]["reachable_nodes"] >= small["stats"]["reachable_nodes"]
    assert big["stats"]["max_radius_m"] >= small["stats"]["max_radius_m"]


def test_isochrone_raises_no_graph_nearby_far_from_the_grid():
    with pytest.raises(routing.NoGraphNearby):
        routing.isochrone(0.0, 0.0, minutes=10, radius_m=500)


def test_build_graph_raises_radius_too_large():
    with pytest.raises(routing.RadiusTooLarge):
        routing.build_graph(ORIGIN_LAT, ORIGIN_LON, routing.WALK_MAX_RADIUS_M + 1)


def test_build_graph_raises_schema_degraded_on_wrong_dataset():
    # places.parquet has no geometry column at all.
    routing.set_data_path(str(PLACES_FIXTURE_PATH))
    try:
        with pytest.raises(routing.SchemaDegraded):
            routing.build_graph(ORIGIN_LAT, ORIGIN_LON, 1000)
    finally:
        routing.set_data_path(None)


def test_server_isochrone_tool_happy_path():
    lat, lon = fx.node_latlon(10, 10)
    result = server.isochrone(lat, lon, minutes=15)
    assert "polygon" in result
    assert result["stats"]["reachable_nodes"] > 0


def test_server_isochrone_unsupported_mode():
    result = server.isochrone(ORIGIN_LAT, ORIGIN_LON, minutes=15, mode="teleport")
    assert result == {"error": "unsupported_mode", "supported": ["cycle", "drive", "walk"]}


def test_server_isochrone_drive_mode_now_works():
    lat, lon = fx.node_latlon(10, 10)
    result = server.isochrone(lat, lon, minutes=15, mode="drive")
    assert result["mode"] == "drive"
    assert result["stats"]["reachable_nodes"] > 0


def test_server_isochrone_cycle_mode_now_works():
    lat, lon = fx.node_latlon(10, 10)
    result = server.isochrone(lat, lon, minutes=15, mode="cycle")
    assert result["mode"] == "cycle"
    assert result["stats"]["reachable_nodes"] > 0


def test_server_isochrone_radius_too_large_error():
    result = server.isochrone(ORIGIN_LAT, ORIGIN_LON, minutes=15, radius_m=999_999)
    assert result["error"] == "radius_too_large"
    assert result["max_radius_m"] == routing.WALK_MAX_RADIUS_M


def test_server_isochrone_no_graph_nearby_error():
    result = server.isochrone(0.0, 0.0, minutes=10, radius_m=500)
    assert result["error"] == "no_graph_nearby"


# --- Issue #133: negative minutes/radius_m must be rejected up front, not ---
# --- silently turned into an inverted bbox that yields a misleading       ---
# --- "no graph nearby" error. --------------------------------------------


def test_server_isochrone_negative_minutes_is_bad_request():
    lat, lon = fx.node_latlon(10, 10)
    result = server.isochrone(lat, lon, minutes=-5)
    assert result["error"] == "bad_request"
    # Not the misleading "no walkable segments found within -Nm" message.
    assert "no_graph_nearby" not in str(result)
    assert "must be greater than 0" in result["detail"]


def test_server_isochrone_negative_radius_m_is_bad_request():
    lat, lon = fx.node_latlon(10, 10)
    result = server.isochrone(lat, lon, minutes=5, radius_m=-100)
    assert result["error"] == "bad_request"


def test_server_isochrone_zero_minutes_is_bad_request():
    lat, lon = fx.node_latlon(10, 10)
    result = server.isochrone(lat, lon, minutes=0)
    assert result["error"] == "bad_request"


def test_isochrone_negative_minutes_raises_value_error():
    lat, lon = fx.node_latlon(10, 10)
    with pytest.raises(ValueError):
        routing.isochrone(lat, lon, minutes=-5)


def test_isochrone_negative_radius_m_raises_value_error():
    lat, lon = fx.node_latlon(10, 10)
    with pytest.raises(ValueError):
        routing.isochrone(lat, lon, minutes=5, radius_m=-100)


def test_server_isochrone_valid_minutes_still_works():
    """No regression: a normal, valid call must still return a polygon/stats,
    not get caught by the new bad_request guard."""
    lat, lon = fx.node_latlon(10, 10)
    result = server.isochrone(lat, lon, minutes=5)
    assert "polygon" in result
    assert result["stats"]["reachable_nodes"] > 0


# --- Issue #37: interior-connector splitting + component-aware snapping ---


def test_interior_connector_links_two_segments_at_a_shared_crossing():
    """x_a0-x_a1 and x_b0-x_b1 only share the *interior* connector x_cross
    (at == 0.5 on both) — neither segment terminates there. The crossing
    must still be traversable, and each split edge's length must be half
    of its parent segment's real length (arc-length fraction, not a
    straight-line reinterpolation)."""
    clat, clon = fx.cross_center_latlon()
    graph = routing.build_graph(clat, clon, 100)

    assert fx.CROSS_CONNECTOR_ID in graph.adjacency
    neighbor_ids = {n for n, _, _ in graph.adjacency[fx.CROSS_CONNECTOR_ID]}
    assert neighbor_ids == {"x_a0", "x_a1", "x_b0", "x_b1"}
    for _, _weight, length_m in graph.adjacency[fx.CROSS_CONNECTOR_ID]:
        assert length_m == pytest.approx(fx.CROSS_HALF_LEN_M, rel=1e-3)

    dist = routing.dijkstra(graph, "x_a0", max_seconds=1000, speed_m_s=1.0)
    assert "x_b1" in dist
    assert dist["x_b1"] == pytest.approx(2 * fx.CROSS_HALF_LEN_M, rel=1e-3)


def test_snap_to_graph_skips_isolated_fragment_for_larger_component():
    """frag_0/frag_1 form an isolated 2-node fragment placed nearer to the
    query point than real grid node (5, 5). Snapping must prefer the grid
    node's much larger component over the geometrically-closer fragment."""
    qlat, qlon = fx.isolated_query_latlon()
    graph = routing.build_graph(qlat, qlon, 150)

    # Sanity: the fragment really is closer than the grid node, and both
    # are present in this graph.
    frag_d = routing._haversine_m(qlat, qlon, *graph.coords["frag_0"])
    grid_node_id = fx.node_id(*fx.ISOLATED_ANCHOR_NODE)
    grid_d = routing._haversine_m(qlat, qlon, *graph.coords[grid_node_id])
    assert frag_d < grid_d

    components = graph.connected_components()
    frag_component = next(c for c in components if "frag_0" in c)
    grid_component = next(c for c in components if grid_node_id in c)
    assert len(frag_component) < routing.MIN_USABLE_COMPONENT_NODES
    assert len(grid_component) >= routing.MIN_USABLE_COMPONENT_NODES

    source = routing.snap_to_graph(graph, qlat, qlon)
    assert source == grid_node_id


def test_isochrone_snaps_past_isolated_fragment():
    qlat, qlon = fx.isolated_query_latlon()
    result = routing.isochrone(qlat, qlon, minutes=5, radius_m=150)
    # If snapping had picked the 2-node fragment instead, only 1-2 nodes
    # (frag_1, maybe frag_0) would ever be reachable.
    assert result["stats"]["reachable_nodes"] > routing.MIN_USABLE_COMPONENT_NODES


# --- Issue #36: concave (grid-boundary-trace) isochrone polygons -----------


def test_build_polygon_falls_back_to_convex_hull_below_threshold():
    reached_coords = [(40.0 + i * 0.0001, -73.0) for i in range(5)]  # < CONCAVE_MIN_NODES
    ring, method, truncated = routing._build_polygon(reached_coords, 40.0, -73.0, 50.0)
    assert method == "convex_hull"
    assert len(ring) >= 2


def test_build_polygon_concave_ring_is_valid_and_within_reached_bbox():
    """An L-shaped blob of reached nodes, well above CONCAVE_MIN_NODES, so the
    boundary trace actually runs. Guarantees: closed-able ring (>=3 open
    points), and every vertex sits inside the reached nodes' own bbox — the
    "never larger than the old convex hull's bbox" guarantee, since a convex
    hull's bbox is exactly the reached points' bbox."""
    reached_coords = [
        (40.0000, -73.0000), (40.0010, -73.0000), (40.0020, -73.0000),
        (40.0000, -73.0010), (40.0000, -73.0020),
        (40.0010, -73.0010), (40.0020, -73.0020), (40.0030, -73.0030),
    ]
    ring, method, truncated = routing._build_polygon(reached_coords, 40.0, -73.0, 300.0)
    assert len(ring) >= 3
    lonlat_points = [(lon_, lat_) for lat_, lon_ in reached_coords]
    hxmin, hymin, hxmax, hymax = routing._bbox_of(lonlat_points)
    for lon_, lat_ in ring:
        assert hxmin - 1e-9 <= lon_ <= hxmax + 1e-9
        assert hymin - 1e-9 <= lat_ <= hymax + 1e-9


def test_isochrone_concave_polygon_excludes_unreachable_far_bank():
    """A short-minutes isochrone from the near riverbank must not cover the
    far bank (only reachable via the single bridge, a long detour around
    BRIDGE_J) — the visible win of a concave boundary trace over a convex
    hull, which would happily draw a hull spanning the water."""
    lat, lon = fx.node_latlon(9, 5)
    result = routing.isochrone(lat, lon, minutes=3, radius_m=400)
    assert result["stats"]["reachable_nodes"] >= routing.CONCAVE_MIN_NODES
    assert result["polygon_method"] == "concave_boundary"
    ring = result["polygon"]["coordinates"][0]

    far_bank_cells = [(10, j) for j in range(2, 9)]
    for i, j in far_bank_cells:
        flat, flon = fx.node_latlon(i, j)
        assert not routing.point_in_ring(flon, flat, ring), (
            f"far bank cell ({i},{j}) is inside the isochrone polygon"
        )


def test_point_in_ring_basic_square():
    ring = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
    assert routing.point_in_ring(0.5, 0.5, ring)
    assert not routing.point_in_ring(1.5, 0.5, ring)


# --- Issue #38: cycling/driving cost models + one-way -----------------------


def test_cycle_class_filter_excludes_motorway_shortcut():
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M, mode="cycle")
    a, b = fx.node_id(*fx.SHORTCUT["from"]), fx.node_id(*fx.SHORTCUT["to"])
    neighbors_of_a = {n for n, _, _ in graph.adjacency[a]}
    assert b not in neighbors_of_a


def test_drive_class_filter_excludes_footway_bridge():
    """The only river crossing is a footway bridge — excluded for driving, so
    the two riverbanks must end up in separate connected components."""
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M, mode="drive")
    near_bank = fx.node_id(fx.RIVER_GAP_I, fx.BRIDGE_J)
    far_bank = fx.node_id(fx.RIVER_GAP_I + 1, fx.BRIDGE_J)
    components = graph.connected_components()
    near_component = next(c for c in components if near_bank in c)
    assert far_bank not in near_component


def test_drive_speed_limit_overrides_class_default_on_shortcut():
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M, mode="drive")
    assert graph.weight_is_time
    a, b = fx.node_id(*fx.SHORTCUT["from"]), fx.node_id(*fx.SHORTCUT["to"])
    weight = next(w for n, w, _length in graph.adjacency[a] if n == b)

    shortcut_length_m = _path_length(fx.SHORTCUT["from"], fx.SHORTCUT["to"])
    expected_seconds = shortcut_length_m / fx.SHORTCUT_SPEED_LIMIT_M_S
    assert weight == pytest.approx(expected_seconds, rel=1e-6)

    class_default_seconds = shortcut_length_m / routing.DRIVE_CLASS_SPEEDS_M_S["motorway"]
    assert weight > class_default_seconds  # posted limit is slower than the class default


def test_oneway_segment_is_directed_for_drive_mode():
    clat, clon = fx.oneway_center_latlon()
    graph = routing.build_graph(clat, clon, 100, mode="drive")
    a_neighbors = {n for n, _, _ in graph.adjacency[fx.ONEWAY_A_ID]}
    b_neighbors = {n for n, _, _ in graph.adjacency[fx.ONEWAY_B_ID]}
    assert fx.ONEWAY_B_ID in a_neighbors  # A -> B reachable
    assert fx.ONEWAY_A_ID not in b_neighbors  # B -> A not reachable


def test_oneway_segment_is_directed_for_cycle_mode():
    clat, clon = fx.oneway_center_latlon()
    graph = routing.build_graph(clat, clon, 100, mode="cycle")
    a_neighbors = {n for n, _, _ in graph.adjacency[fx.ONEWAY_A_ID]}
    b_neighbors = {n for n, _, _ in graph.adjacency[fx.ONEWAY_B_ID]}
    assert fx.ONEWAY_B_ID in a_neighbors
    assert fx.ONEWAY_A_ID not in b_neighbors


def test_oneway_segment_is_undirected_for_walk_mode():
    clat, clon = fx.oneway_center_latlon()
    graph = routing.build_graph(clat, clon, 100, mode="walk")
    a_neighbors = {n for n, _, _ in graph.adjacency[fx.ONEWAY_A_ID]}
    b_neighbors = {n for n, _, _ in graph.adjacency[fx.ONEWAY_B_ID]}
    assert fx.ONEWAY_B_ID in a_neighbors
    assert fx.ONEWAY_A_ID in b_neighbors


def test_oneway_isochrone_asymmetry_drive_mode():
    """A -> B reachable, B -> A not, for drive mode — reachability (via
    dijkstra) is the issue's stated asymmetry, distinct from the raw
    adjacency check in test_oneway_segment_is_directed_for_drive_mode."""
    clat, clon = fx.oneway_center_latlon()
    graph = routing.build_graph(clat, clon, 100, mode="drive")
    forward = routing.dijkstra(graph, fx.ONEWAY_A_ID, max_seconds=60, speed_m_s=1.0)
    backward = routing.dijkstra(graph, fx.ONEWAY_B_ID, max_seconds=60, speed_m_s=1.0)
    assert fx.ONEWAY_B_ID in forward
    assert fx.ONEWAY_A_ID not in backward


def test_build_graph_radius_caps_per_mode():
    with pytest.raises(routing.RadiusTooLarge):
        routing.build_graph(ORIGIN_LAT, ORIGIN_LON, routing.CYCLE_MAX_RADIUS_M + 1, mode="cycle")
    with pytest.raises(routing.RadiusTooLarge):
        routing.build_graph(ORIGIN_LAT, ORIGIN_LON, routing.DRIVE_MAX_RADIUS_M + 1, mode="drive")
    # Walking's cap is unchanged and much tighter than cycling/driving's.
    assert routing.WALK_MAX_RADIUS_M < routing.CYCLE_MAX_RADIUS_M < routing.DRIVE_MAX_RADIUS_M


def test_isochrone_raises_unsupported_mode_for_unknown_string():
    with pytest.raises(routing.UnsupportedMode):
        routing.isochrone(ORIGIN_LAT, ORIGIN_LON, minutes=5, mode="teleport")


def test_drive_isochrone_reaches_farther_than_walk_same_minutes():
    lat, lon = fx.node_latlon(10, 10)
    walk = routing.isochrone(lat, lon, minutes=5, mode="walk")
    drive = routing.isochrone(lat, lon, minutes=5, mode="drive")
    assert drive["stats"]["max_radius_m"] > walk["stats"]["max_radius_m"]
    assert drive["stats"]["reachable_nodes"] >= walk["stats"]["reachable_nodes"]


# --- Issue #39: in-memory graph cache ---------------------------------------


def test_bbox_contains_helper():
    outer = (-74.0, 40.0, -73.9, 40.1)
    inner = (-73.98, 40.02, -73.96, 40.08)
    assert routing._bbox_contains(outer, inner)
    assert not routing._bbox_contains(inner, outer)


def test_graph_cache_reuses_graph_for_identical_repeat_query(monkeypatch):
    lat, lon = fx.node_latlon(10, 10)
    calls = []
    original_build_graph = routing.build_graph

    def counting_build_graph(*args, **kwargs):
        calls.append((args, kwargs))
        return original_build_graph(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", counting_build_graph)

    routing.isochrone(lat, lon, minutes=5, mode="walk")
    routing.isochrone(lat, lon, minutes=5, mode="walk")
    assert len(calls) == 1


def test_graph_cache_reuses_graph_for_smaller_nested_query(monkeypatch):
    """The margin-padded extraction from a bigger query should already cover
    a later, smaller query at the same origin without re-extracting."""
    lat, lon = fx.node_latlon(10, 10)
    calls = []
    original_build_graph = routing.build_graph

    def counting_build_graph(*args, **kwargs):
        calls.append((args, kwargs))
        return original_build_graph(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", counting_build_graph)

    routing.isochrone(lat, lon, minutes=15, mode="walk")
    routing.isochrone(lat, lon, minutes=2, mode="walk")
    assert len(calls) == 1


def test_graph_cache_misses_on_different_mode(monkeypatch):
    lat, lon = fx.node_latlon(10, 10)
    calls = []
    original_build_graph = routing.build_graph

    def counting_build_graph(*args, **kwargs):
        calls.append((args, kwargs))
        return original_build_graph(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", counting_build_graph)

    routing.isochrone(lat, lon, minutes=5, mode="walk")
    routing.isochrone(lat, lon, minutes=5, mode="cycle")
    assert len(calls) == 2


def test_graph_cache_misses_on_different_release(monkeypatch):
    """The cache key includes the resolved release, so a release bump can't
    accidentally reuse a graph built from a different release's data — even
    though this test's data path override keeps both queries pointed at the
    same fixture file, only the cache key should change, not the data."""
    lat, lon = fx.node_latlon(10, 10)
    calls = []
    original_build_graph = routing.build_graph

    def counting_build_graph(*args, **kwargs):
        calls.append((args, kwargs))
        return original_build_graph(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", counting_build_graph)

    routing.isochrone(lat, lon, minutes=5, mode="walk")
    monkeypatch.setattr(routing.release, "resolve_release", lambda: "9999-99-99.9")
    routing.isochrone(lat, lon, minutes=5, mode="walk")
    assert len(calls) == 2


# --- Issue #73: MAX_GRAPH_SEGMENTS cap (defense in depth) ------------------


def test_build_graph_not_truncated_under_normal_cap():
    """The fixture's whole grid (769 segments) sits far under the real
    MAX_GRAPH_SEGMENTS default — normal-sized graphs must not be flagged."""
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M)
    assert graph.truncated is False
    assert graph.node_count() == EXPECTED_NODE_COUNT


def test_build_graph_truncates_when_segment_cap_is_exceeded(monkeypatch):
    """With a cap far below the fixture's 769 segments, build_graph must stop
    early, flag the graph truncated, and still return a usable (smaller)
    graph rather than raising or silently building the full thing."""
    monkeypatch.setattr(routing, "MAX_GRAPH_SEGMENTS", 50)
    graph = routing.build_graph(ORIGIN_LAT, ORIGIN_LON, WHOLE_GRID_RADIUS_M)
    assert graph.truncated is True
    # Fewer segments read -> a strictly smaller graph than the untruncated one.
    assert graph.node_count() < EXPECTED_NODE_COUNT
    assert graph.node_count() > 0


def test_isochrone_flags_truncated_result_when_graph_cap_is_exceeded(monkeypatch):
    """cap=70 keeps just enough of the fixture's row-ordered horizontal edges
    (columns 0-4 for low-numbered rows) to form one small usable component
    around node (1, 3) — enough for snap_to_graph/dijkstra to succeed, while
    still being a small fraction of the fixture's 769 segments, so the
    truncation signal is unambiguous."""
    monkeypatch.setattr(routing, "MAX_GRAPH_SEGMENTS", 70)
    lat, lon = fx.node_latlon(1, 3)
    result = routing.isochrone(lat, lon, minutes=15, radius_m=WHOLE_GRID_RADIUS_M)
    assert result["truncated"] is True
    assert result["stats"]["graph_truncated"] is True
    assert "note" in result["stats"]


def test_isochrone_not_flagged_graph_truncated_under_normal_cap():
    """Distinct from the polygon-simplification `truncated` flag (which can
    legitimately fire on its own, see test_isochrone_polygon_is_valid_...):
    this only checks the graph-cap-specific signal stays absent for a normal
    small graph."""
    lat, lon = fx.node_latlon(10, 10)
    result = routing.isochrone(lat, lon, minutes=15)
    assert "graph_truncated" not in result["stats"]


def test_graph_cache_max_size_evicts_least_recently_used():
    routing.clear_graph_cache()
    lat, lon = fx.node_latlon(10, 10)
    # Each origin a full degree apart lands in a distinct cache tile, so
    # every call below is guaranteed to insert (never reuse) an entry —
    # build_graph on an empty/no-op query is a valid, cheap, zero-node
    # Graph, not an error (NoGraphNearby is only raised inside isochrone()).
    for i in range(routing.GRAPH_CACHE_MAXSIZE + 3):
        offset_lat = lat + i * 1.0
        routing._get_or_build_graph(offset_lat, lon, 100.0, "walk", None)
    assert len(routing._graph_cache) <= routing.GRAPH_CACHE_MAXSIZE
