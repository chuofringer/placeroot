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
# x_a0, x_a1, x_b0, x_b1, x_cross) and the isolated 2-node fragment
# (frag_0, frag_1) near grid node (5, 5). See build_routing_fixture.py.
EXPECTED_NODE_COUNT = fx.GRID_N * fx.GRID_N + 5 + 2
# All grid edges (horizontal + vertical + diagonal) minus the one motorway
# shortcut (excluded by the walkable filter), plus the crossing's 4 split
# edges and the isolated fragment's 1 edge (same #37 additions as above).
EXPECTED_EDGE_COUNT = len(fx.build_edges()) - 1 + 4 + 1


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
    neighbors_of_a = {n for n, _ in graph.adjacency[a]}
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
    assert len(ring) <= routing.MAX_POLYGON_POINTS + 1
    assert result["stats"]["reachable_nodes"] > 0
    assert result["stats"]["area_km2"] >= 0
    assert result["polygon_method"] == "convex_hull"


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
        routing.build_graph(ORIGIN_LAT, ORIGIN_LON, routing.MAX_RADIUS_M + 1)


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
    result = server.isochrone(ORIGIN_LAT, ORIGIN_LON, minutes=15, mode="drive")
    assert result == {"error": "unsupported_mode", "supported": ["walk"]}


def test_server_isochrone_radius_too_large_error():
    result = server.isochrone(ORIGIN_LAT, ORIGIN_LON, minutes=15, radius_m=999_999)
    assert result["error"] == "radius_too_large"
    assert result["max_radius_m"] == routing.MAX_RADIUS_M


def test_server_isochrone_no_graph_nearby_error():
    result = server.isochrone(0.0, 0.0, minutes=10, radius_m=500)
    assert result["error"] == "no_graph_nearby"


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
    neighbor_ids = {n for n, _ in graph.adjacency[fx.CROSS_CONNECTOR_ID]}
    assert neighbor_ids == {"x_a0", "x_a1", "x_b0", "x_b1"}
    for _, length_m in graph.adjacency[fx.CROSS_CONNECTOR_ID]:
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
