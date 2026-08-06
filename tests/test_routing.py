import pytest

from placeroot import routing, server

from ._routing_fixture import build_routing_fixture as fx
from .conftest import FIXTURE_PATH as PLACES_FIXTURE_PATH

ORIGIN_LAT = fx.ORIGIN_LAT
ORIGIN_LON = fx.ORIGIN_LON

# Radius (m) generous enough to pull the whole 20x20 grid into one query.
WHOLE_GRID_RADIUS_M = 3000

EXPECTED_NODE_COUNT = fx.GRID_N * fx.GRID_N
# All grid edges (horizontal + vertical + diagonal) minus the one motorway
# shortcut, which the walkable filter must exclude.
EXPECTED_EDGE_COUNT = len(fx.build_edges()) - 1


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
