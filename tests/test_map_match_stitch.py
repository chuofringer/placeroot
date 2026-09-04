"""match_trace (#441): stitching ordered MATCHED snapped points into one
continuous route, plus the spatial-prune equivalence check for #440's
O(points x edges) fix.

Same offline fixture as test_map_match_snap.py (see its module docstring):
a 20x20 street grid, 100m spacing, with named streets ("Grid Ave {j}" for
horizontal row j, "Grid St {i}" for vertical column i — see
scripts/build_routing_fixture.py) and a river gap at column boundary
RIVER_GAP_I/RIVER_GAP_I+1 crossable only via a footway bridge at row
BRIDGE_J. No network, no live Overture scan.
"""

import pytest

from placeroot import map_match, routing

from ._routing_fixture import build_routing_fixture as fx


def _trace_point(i: int, j: int, along_m: float = 20.0, offset_m: float = 5.0):
    """A point along the (i, j)->(i+1, j) grid edge, `along_m` east of node
    (i, j) then `offset_m` further north — same "offset off the line"
    pattern test_map_match_snap.py uses, chosen (along_m=20 of the 100m
    edge, i.e. fraction ~0.2) so it snaps unambiguously closer to node
    (i, j) than to (i+1, j), making the resulting anchor node predictable.
    """
    lat, lon = fx.node_latlon(i, j)
    lat, lon = fx._offset(lat, lon, along_m, 90)
    return fx._offset(lat, lon, offset_m, 0)


def _row_trace(row_j: int, columns: list[int], offset_m: float = 5.0) -> list[dict]:
    lats_lons = [_trace_point(i, row_j, offset_m=offset_m) for i in columns]
    return [{"lat": lat, "lon": lon} for lat, lon in lats_lons]


def test_clean_trace_along_a_known_street_matches_its_name():
    points = _row_trace(2, [2, 3, 4, 5])

    result = map_match.match_trace(points, mode="walk")

    assert result.unmatched_indices == []
    assert result.road_names == ["Grid Ave 2"]
    assert result.matched_length_m == pytest.approx(300.0, rel=0.10)
    assert len(result.geometry) >= 2
    # Continuous: consecutive vertices are never implausibly far apart for a
    # 100m-spacing grid (catches an accidental jump-cut in the polyline).
    for (lat1, lon1), (lat2, lon2) in zip(result.geometry, result.geometry[1:]):
        assert routing._haversine_m(lat1, lon1, lat2, lon2) < 150.0
    assert result.confidence > 0.0


def test_one_outlier_point_is_unmatched_rest_still_stitches():
    """A point across the river from the rest of the trace, at the same row,
    forces a huge bridge detour (~1700m routed) versus its ~500m
    straight-line distance from the last real anchor — comfortably over the
    STITCH_OUTLIER_RATIO_K=3.0 guard — so it must be dropped from stitching
    without disturbing the rest of the trace.
    """
    clean_points = _row_trace(2, [2, 3, 4, 5])
    outlier_lat, outlier_lon = _trace_point(fx.RIVER_GAP_I + 1, 2, along_m=0.0, offset_m=5.0)
    points = [*clean_points, {"lat": outlier_lat, "lon": outlier_lon}]
    outlier_index = len(points) - 1

    result = map_match.match_trace(points, mode="walk")

    assert result.unmatched_indices == [outlier_index]
    assert result.road_names == ["Grid Ave 2"]
    assert result.matched_length_m == pytest.approx(300.0, rel=0.10)


def test_noisy_trace_scores_below_the_same_clean_trace():
    clean_points = _row_trace(2, [2, 3, 4, 5], offset_m=5.0)
    noisy_points = _row_trace(2, [2, 3, 4, 5], offset_m=50.0)

    clean = map_match.match_trace(clean_points, mode="walk")
    noisy = map_match.match_trace(noisy_points, mode="walk")

    assert clean.unmatched_indices == []
    assert noisy.confidence < clean.confidence


def test_grid_prune_matches_naive_full_scan():
    """The grid-pruned snap and the naive full-edge-scan snap must agree
    point for point on the fixture — equivalence check for the #441 perf
    fix (see map_match._snap_trace_with_graph's use_grid escape hatch)."""
    row_points = _row_trace(2, [2, 3, 4, 5, 6, 7])
    # Off the exact grid lattice (see _trace_point) rather than sitting on
    # node (i, 15) itself — a point exactly at a 4-way intersection is
    # equidistant (0m) from every edge touching it, and which of those tied
    # edges "wins" depends on iteration order, which legitimately differs
    # between the grid-bucketed scan and the naive full scan. That's not a
    # correctness bug in either scan, just an unrepresentative test point.
    scattered_points = _row_trace(15, list(range(0, 18, 3)))
    # A point nowhere near a real edge too, so the fallback path (empty grid
    # neighborhood) gets exercised on both sides.
    near_lat, near_lon = fx.node_latlon(2, 2)
    near_lat, near_lon = fx._offset(near_lat, near_lon, 50.0, 90)
    far_lat, far_lon = fx._offset(near_lat, near_lon, 700.0, 180)
    points = [*row_points, *scattered_points, {"lat": far_lat, "lon": far_lon}]

    _graph_a, pruned = map_match._snap_trace_with_graph(points, "walk", use_grid=True)
    _graph_b, naive = map_match._snap_trace_with_graph(points, "walk", use_grid=False)

    assert len(pruned) == len(naive)
    for p, n in zip(pruned, naive):
        assert p.matched == n.matched
        assert p.edge == n.edge
        assert p.fraction == n.fraction or (
            p.fraction is not None
            and n.fraction is not None
            and p.fraction == pytest.approx(n.fraction)
        )
        assert p.distance_m == n.distance_m or (
            p.distance_m is not None
            and n.distance_m is not None
            and p.distance_m == pytest.approx(n.distance_m)
        )


def test_all_unmatched_trace_returns_empty_result_without_exception():
    # The grid only extends north/east of ORIGIN (node (0, 0) is its
    # southwest corner), so offsetting southwest of ORIGIN itself lands
    # every point off the grid's edge, however the offset is spread out.
    far_points = [
        fx._offset(fx.ORIGIN_LAT, fx.ORIGIN_LON, 700.0 + step * 50.0, 225) for step in range(3)
    ]
    points = [{"lat": lat, "lon": lon} for lat, lon in far_points]

    result = map_match.match_trace(points, mode="walk")

    assert result.matched_length_m == 0.0
    assert result.geometry == []
    assert result.road_names == []
    assert result.confidence == 0.0
    assert result.unmatched_indices == [0, 1, 2]


def test_empty_trace_returns_empty_result():
    result = map_match.match_trace([], mode="walk")
    assert result.matched_length_m == 0.0
    assert result.geometry == []
    assert result.road_names == []
    assert result.confidence == 0.0
    assert result.unmatched_indices == []


def test_over_cap_points_raise_value_error():
    lat, lon = fx.node_latlon(2, 2)
    points = [{"lat": lat, "lon": lon}] * (map_match.MAX_TRACE_POINTS + 1)
    with pytest.raises(ValueError):
        map_match.match_trace(points, mode="walk")


def test_unsupported_mode_raises():
    lat, lon = fx.node_latlon(2, 2)
    with pytest.raises(routing.UnsupportedMode):
        map_match.match_trace([{"lat": lat, "lon": lon}], mode="teleport")
