"""map_match 1/3 (#440): per-point trace snapping against the street graph.

snap_trace(points, mode) snaps an ordered list of {lat, lon} points onto the
same street graph route()/isochrone() build from Overture transportation
data — internal only, no MCP surface yet (see #439 for the umbrella
map_match plan).

Unlike routing.snap_to_graph, which resolves a route's endpoint to the
nearest graph *node*, a GPS trace point is typically mid-block: snapping it
to the nearest node would teleport it to whichever intersection happens to
be closest, discarding the actual road position. Instead every point is
projected onto the nearest graph *edge* — its node-pair chord plus the
edge's real shape vertices (Graph.shape_between), the same geometry
route(include_path=True) emits — recording where along that edge (as a
0..1 fraction) the point lands.

One graph build per call: the whole trace's bounding circle (padded by
SNAP_RADIUS_M, mirroring _stops_extraction_geometry's containment margin)
is extracted once via routing._get_or_build_graph and reused for every
point, never one graph per point.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

from . import routing

MAX_TRACE_POINTS = 100


@dataclass
class SnappedPoint:
    """One input point's snap result.

    `matched` is False when either nothing usable lies within
    routing.SNAP_RADIUS_M of the point, or the graph has no usable edges at
    all near the trace (tiny/disconnected fragments skipped, mirroring
    snap_to_graph's MIN_USABLE_COMPONENT_NODES treatment) — the point is
    still returned, never dropped. When a candidate edge was found at all
    (even one too far away to count as matched), edge/fraction/snapped_lat/
    snapped_lon/distance_m are populated with that best candidate so a
    caller can see how close the trace came; they are None only when the
    graph had no usable edge whatsoever near the trace.
    """

    index: int
    lat: float
    lon: float
    matched: bool
    edge: tuple[str, str] | None = None
    fraction: float | None = None
    snapped_lat: float | None = None
    snapped_lon: float | None = None
    distance_m: float | None = None


def _usable_nodes(graph: routing.Graph) -> set[str]:
    """Node ids belonging to a "usable" component: at least
    routing.MIN_USABLE_COMPONENT_NODES nodes, or the graph's largest
    component if every component happens to be smaller than that (same
    tie-breaker snap_to_graph applies to its single nearest-node search).
    """
    components = graph.connected_components()
    if not components:
        return set()
    largest = max(components, key=len)
    usable: set[str] = set()
    for component in components:
        if len(component) >= routing.MIN_USABLE_COMPONENT_NODES or component is largest:
            usable |= component
    return usable


def _edge_polyline(graph: routing.Graph, a: str, b: str) -> list[tuple[float, float]]:
    """(lat, lon) vertices from node `a` to node `b`, endpoints included.

    Edge shapes are stored as (lon, lat) pairs (see Graph._register_shape);
    flipped here so the whole polyline is consistently (lat, lon).
    """
    alat, alon = graph.coords[a]
    blat, blon = graph.coords[b]
    shape, _dropped_m = graph.shape_between(a, b)
    interior = [(lat, lon) for lon, lat in shape]
    return [(alat, alon), *interior, (blat, blon)]


def _project_onto_polyline(
    plat: float, plon: float, polyline: list[tuple[float, float]]
) -> tuple[float, float, float, float]:
    """(distance_m, fraction, snapped_lat, snapped_lon) of the closest point
    on `polyline` to (plat, plon). fraction is the arc-length fraction
    (0..1) along the whole polyline, matching Overture's own connector `at`
    convention (see routing._point_at_fraction)."""
    cum = [0.0]
    for i in range(len(polyline) - 1):
        lat1, lon1 = polyline[i]
        lat2, lon2 = polyline[i + 1]
        cum.append(cum[-1] + routing._haversine_m(lat1, lon1, lat2, lon2))
    total = cum[-1]
    if total <= 0.0:
        lat0, lon0 = polyline[0]
        return routing._haversine_m(plat, plon, lat0, lon0), 0.0, lat0, lon0

    best_dist_m = math.inf
    best_along_m = 0.0
    best_lat, best_lon = polyline[0]
    for i in range(len(polyline) - 1):
        alat, alon = polyline[i]
        blat, blon = polyline[i + 1]
        dist_m, t = routing._point_to_segment_m(plat, plon, alat, alon, blat, blon)
        if dist_m < best_dist_m:
            best_dist_m = dist_m
            best_along_m = cum[i] + t * (cum[i + 1] - cum[i])
            best_lat = alat + t * (blat - alat)
            best_lon = alon + t * (blon - alon)
    return best_dist_m, best_along_m / total, best_lat, best_lon


def _snap_one(
    graph: routing.Graph, usable_nodes: set[str], index: int, lat: float, lon: float
) -> SnappedPoint:
    best: tuple[float, tuple[str, str], float, float, float] | None = None
    # Parallel edges collapse onto one unordered node pair here, and
    # _edge_polyline -> shape_between then resolves to the lowest-weight
    # traversal for that pair — so "nearest edge" means nearest of the
    # surviving representatives, not of every physical way. Two parallel
    # edges sharing both endpoints (a one-way twin bulging the other way)
    # can therefore snap against the wrong twin's geometry; shared endpoints
    # keep the error second-order, accepted for the scan's simplicity.
    seen_pairs: set[tuple[str, str]] = set()
    for a, neighbors in graph.adjacency.items():
        if a not in usable_nodes:
            continue
        for b, _weight, _length_m in neighbors:
            if b not in usable_nodes:
                continue
            pair_key = (a, b) if a <= b else (b, a)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            polyline = _edge_polyline(graph, a, b)
            dist_m, fraction, slat, slon = _project_onto_polyline(lat, lon, polyline)
            if best is None or dist_m < best[0]:
                best = (dist_m, (a, b), fraction, slat, slon)

    if best is None:
        return SnappedPoint(index=index, lat=lat, lon=lon, matched=False)

    dist_m, edge, fraction, slat, slon = best
    return SnappedPoint(
        index=index,
        lat=lat,
        lon=lon,
        matched=dist_m <= routing.SNAP_RADIUS_M,
        edge=edge,
        fraction=fraction,
        snapped_lat=slat,
        snapped_lon=slon,
        distance_m=dist_m,
    )


def snap_trace(points: list[Mapping[str, float]], mode: str = "walk") -> list[SnappedPoint]:
    """Snap each of `points` ({"lat": ..., "lon": ...}) onto `mode`'s street
    graph edges, in input order.

    Builds exactly one graph, over the padded bounding circle of the whole
    trace, reusing routing._get_or_build_graph's build/cache path — never
    one graph per point. The graph is built with the mode's own class
    exclusions (routing.MODE_CONFIG[mode]["excluded_classes"], applied
    inside build_graph), so e.g. a drive trace never snaps onto a footway
    edge.

    Raises ValueError if len(points) exceeds MAX_TRACE_POINTS,
    routing.UnsupportedMode for an unknown mode string, or
    routing.RadiusTooLarge when the trace's padded bounding circle exceeds
    the mode's extraction cap (MODE_CONFIG[mode]["max_radius_m"] — e.g. a
    walk trace spanning more than ~2x5 km of diameter). Deliberately NOT
    converted to ValueError here: the typed exception carries
    radius_m/max_radius_m, and the MCP tool layer (#442) translates it into
    the same structured "radius_too_large" answer its sibling tools return.
    """
    if len(points) > MAX_TRACE_POINTS:
        raise ValueError(f"snap_trace accepts at most {MAX_TRACE_POINTS} points, got {len(points)}")
    if mode not in routing.MODE_CONFIG:
        raise routing.UnsupportedMode(mode)
    if not points:
        return []

    latlon = [(p["lat"], p["lon"]) for p in points]
    center_lat, center_lon = routing._minimum_enclosing_center(latlon)
    enclosing_radius_m = max(
        (routing._haversine_m(center_lat, center_lon, lat, lon) for lat, lon in latlon),
        default=0.0,
    )
    radius_m = enclosing_radius_m + routing.SNAP_RADIUS_M

    graph = routing._get_or_build_graph(
        center_lat, center_lon, radius_m, mode, speed_m_s=None, want_shapes=True
    )
    usable_nodes = _usable_nodes(graph)

    return [
        _snap_one(graph, usable_nodes, index, lat, lon) for index, (lat, lon) in enumerate(latlon)
    ]
