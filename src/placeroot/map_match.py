"""map_match 1-2/3 (#440, #441): trace snapping and stitching against the
street graph.

snap_trace(points, mode) snaps an ordered list of {lat, lon} points onto the
same street graph route()/isochrone() build from Overture transportation
data. match_trace(points, mode) goes one step further: it connects the
ordered MATCHED snapped points into a single continuous route, the way a
real GPS trace's "which streets did I actually walk" answer should look —
both internal only, no MCP surface yet (see #439 for the umbrella map_match
plan; #442 is the eventual MCP tool).

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

Spatial prune (#441, addressing #440's review comment): per-point edge
scanning used to be a full O(points x edges) pass over every graph edge.
Edges are now bucketed once per call into a lat/lon grid (_build_edge_index)
sized GRID_CELL_M — SNAP_RADIUS_M itself. That size is not arbitrary: for a
point anywhere inside its own cell, any edge within one cell-width of it
(in either axis) is guaranteed to fall in the point's cell or one of its 8
neighbors (a point at a cell's far edge can reach at most one cell further
before crossing a second boundary), so querying the 3x3 neighborhood around
a point's cell is an exact stand-in for "every edge within SNAP_RADIUS_M"
whenever that neighborhood is non-empty. When it's empty (no edge bucketed
anywhere nearby — a point genuinely far from the graph), _snap_one falls
back to a full scan, which is also exactly the case where a full scan is
cheap to justify (SnappedPoint still wants the best candidate anywhere, not
just within SNAP_RADIUS_M, to report distance_m honestly).

Stitching (match_trace) connects each consecutive pair of MATCHED snapped
points through the graph with routing's own target-terminated Dijkstra
(_dijkstra_path_to_target), anchoring each snapped point to the NEARER of
its edge's two endpoint nodes (by snap fraction) rather than trimming the
edge geometry at the exact fractional position. This is a deliberate
simplification over full fractional trimming: it keeps every leg a plain
node-to-node shortest path (reusable dijkstra, reusable geometry
concatenation, reusable name lookup) at the cost of up to half an edge's
length of positional slop at each end of the stitched route — negligible
next to SNAP_RADIUS_M and the outlier guard's own slack.

Outlier guard: a leg is rejected (its later point added to unmatched,
stitching continuing from the same anchor) when the routed distance between
consecutive anchors exceeds max(STITCH_OUTLIER_RATIO_K * straight_line_m,
straight_line_m + STITCH_OUTLIER_MIN_SLACK_M). The two-term max matters:
the ratio alone would flag adjacent points a few meters apart (straight_line_m
near zero, so even a short detour around a building looks like a huge
multiple) as outliers; the absolute STITCH_OUTLIER_MIN_SLACK_M term gives
those short hops room to detour without tripping the guard, while the ratio
term still catches a genuine trace glitch (a GPS point that teleported
hundreds of meters) once distances are large enough for a fixed slack to
stop being convincing on its own. STITCH_OUTLIER_RATIO_K = 3.0 admits any
plausible street detour (going around a block instead of cutting through
it rarely triples the straight-line distance) while still catching a trace
point that jumped to a different street or a different block entirely.

Road names are read straight off the stitched node path via
routing.Graph.name_between (Overture names.primary, threaded through
routing.build_graph/Graph in this same change — route() itself has never
emitted road names, so there was no existing mechanism to reuse; this adds
the minimal one and both route() and map_match sit on top of it going
forward). Names are collected in travel order and deduplicated only when
they repeat on *consecutive* edges, so a route that leaves a street and
later returns to it lists that name twice, honestly.

Confidence is (stitched matched point count / total point count) *
(1 - mean_snap_distance_m / routing.SNAP_RADIUS_M) over the points that
ended up stitched. Both factors move the right way under noise: jittering a
clean trace pushes points further from their true street (raising
mean_snap_distance_m, shrinking the second factor) and can knock some
points below the survivability of the outlier guard onto entirely different
edges (shrinking the matched fraction) — so a clean trace's confidence is
never lower than the same trace with noise added, and is strictly higher
whenever the noise moved at least one snap distance or matched point.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

from . import routing

MAX_TRACE_POINTS = 100

# Spatial-prune bucket size — see the module docstring for why this exact
# value (SNAP_RADIUS_M) makes the 3x3 neighborhood query exact rather than
# approximate.
GRID_CELL_M = routing.SNAP_RADIUS_M

# Outlier guard for match_trace's stitching — see the module docstring.
STITCH_OUTLIER_RATIO_K = 3.0
STITCH_OUTLIER_MIN_SLACK_M = 30.0


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


def _all_usable_pairs(graph: routing.Graph, usable_nodes: set[str]) -> list[tuple[str, str]]:
    """Every usable undirected edge, once each, as an (a, b) with a <= b."""
    seen_pairs: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
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
            pairs.append(pair_key)
    return pairs


def _edge_index(
    graph: routing.Graph, usable_nodes: set[str], center_lat: float, cell_m: float = GRID_CELL_M
) -> tuple[dict[tuple[int, int], list[tuple[str, str]]], list[tuple[str, str]], float, float]:
    """(grid, all_pairs, cell_deg_lat, cell_deg_lon): a lat/lon-cell index over
    every usable edge's bounding box, bucketed by every cell it overlaps —
    see the module docstring for why 3x3-neighborhood lookups against a
    cell_m == SNAP_RADIUS_M grid are exact, not approximate. `all_pairs` is
    the same full pair list _snap_one falls back to scanning when a point's
    neighborhood comes back empty.

    center_lat is a single representative latitude for the whole trace (the
    same enclosing-circle center snap_trace already computes) used only to
    pick one meters-per-degree-longitude conversion for the whole grid — the
    same fixed-latitude approximation geo.bbox_around makes, fine at the
    city scale a single trace spans.
    """
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(center_lat)), 1e-6)
    cell_deg_lat = cell_m / m_per_deg_lat
    cell_deg_lon = cell_m / m_per_deg_lon

    all_pairs = _all_usable_pairs(graph, usable_nodes)
    grid: dict[tuple[int, int], list[tuple[str, str]]] = {}
    for pair in all_pairs:
        polyline = _edge_polyline(graph, *pair)
        lats = [lat for lat, _lon in polyline]
        lons = [lon for _lat, lon in polyline]
        i0, i1 = math.floor(min(lats) / cell_deg_lat), math.floor(max(lats) / cell_deg_lat)
        j0, j1 = math.floor(min(lons) / cell_deg_lon), math.floor(max(lons) / cell_deg_lon)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                grid.setdefault((i, j), []).append(pair)
    return grid, all_pairs, cell_deg_lat, cell_deg_lon


def _candidates_near(
    grid: dict[tuple[int, int], list[tuple[str, str]]],
    all_pairs: list[tuple[str, str]],
    cell_deg_lat: float,
    cell_deg_lon: float,
    lat: float,
    lon: float,
) -> list[tuple[str, str]]:
    """Edge pairs from (lat, lon)'s grid cell and its 8 neighbors, deduplicated;
    falls back to `all_pairs` (the naive full scan) when that neighborhood
    is empty — see the module docstring's spatial-prune section."""
    ci, cj = math.floor(lat / cell_deg_lat), math.floor(lon / cell_deg_lon)
    seen: set[tuple[str, str]] = set()
    candidates: list[tuple[str, str]] = []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for pair in grid.get((ci + di, cj + dj), ()):
                if pair not in seen:
                    seen.add(pair)
                    candidates.append(pair)
    return candidates or all_pairs


def _snap_one(
    graph: routing.Graph,
    candidate_pairs: list[tuple[str, str]],
    polyline_cache: dict[tuple[str, str], list[tuple[float, float]]],
    index: int,
    lat: float,
    lon: float,
) -> SnappedPoint:
    best: tuple[float, tuple[str, str], float, float, float] | None = None
    for pair in candidate_pairs:
        polyline = polyline_cache.get(pair)
        if polyline is None:
            polyline = _edge_polyline(graph, *pair)
            polyline_cache[pair] = polyline
        dist_m, fraction, slat, slon = _project_onto_polyline(lat, lon, polyline)
        if best is None or dist_m < best[0]:
            best = (dist_m, pair, fraction, slat, slon)

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


def _snap_trace_with_graph(
    points: list[Mapping[str, float]], mode: str, use_grid: bool = True
) -> tuple[routing.Graph | None, list[SnappedPoint]]:
    """Shared build+snap path for snap_trace and match_trace: validates,
    builds exactly one graph over the trace's padded bounding circle, and
    snaps every point against it (grid-pruned by default; use_grid=False is
    a test-only escape hatch to force the naive full-edge scan for
    equivalence comparisons — see tests/test_map_match_stitch.py).

    Raises ValueError if len(points) exceeds MAX_TRACE_POINTS, or
    routing.UnsupportedMode for an unknown mode string.
    """
    if len(points) > MAX_TRACE_POINTS:
        raise ValueError(f"snap_trace accepts at most {MAX_TRACE_POINTS} points, got {len(points)}")
    if mode not in routing.MODE_CONFIG:
        raise routing.UnsupportedMode(mode)
    if not points:
        return None, []

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

    if use_grid:
        grid, all_pairs, cell_deg_lat, cell_deg_lon = _edge_index(graph, usable_nodes, center_lat)
    else:
        all_pairs = _all_usable_pairs(graph, usable_nodes)

    polyline_cache: dict[tuple[str, str], list[tuple[float, float]]] = {}
    snapped = []
    for index, (lat, lon) in enumerate(latlon):
        candidates = (
            _candidates_near(grid, all_pairs, cell_deg_lat, cell_deg_lon, lat, lon)
            if use_grid
            else all_pairs
        )
        snapped.append(_snap_one(graph, candidates, polyline_cache, index, lat, lon))
    return graph, snapped


def snap_trace(points: list[Mapping[str, float]], mode: str = "walk") -> list[SnappedPoint]:
    """Snap each of `points` ({"lat": ..., "lon": ...}) onto `mode`'s street
    graph edges, in input order.

    Builds exactly one graph, over the padded bounding circle of the whole
    trace, reusing routing._get_or_build_graph's build/cache path — never
    one graph per point. The graph is built with the mode's own class
    exclusions (routing.MODE_CONFIG[mode]["excluded_classes"], applied
    inside build_graph), so e.g. a drive trace never snaps onto a footway
    edge.

    Raises ValueError if len(points) exceeds MAX_TRACE_POINTS, or
    routing.UnsupportedMode for an unknown mode string.
    """
    _graph, snapped = _snap_trace_with_graph(points, mode)
    return snapped


@dataclass
class MatchedRoute:
    """match_trace's result — see the module docstring for the stitching
    algorithm, outlier guard, road-name aggregation, and confidence formula.

    matched_length_m is the sum of routed (not straight-line) distances
    across every accepted leg, 0.0 when fewer than two points stitched.
    geometry is the ordered, deduplicated (lat, lon) polyline of the whole
    stitched route (empty when nothing stitched). road_names preserves
    travel order, deduplicating only consecutive repeats. confidence is
    always in [0.0, 1.0]. unmatched_indices covers both points snap_trace
    itself never matched and points the outlier guard rejected during
    stitching, sorted, deduplicated.
    """

    matched_length_m: float
    geometry: list[tuple[float, float]]
    road_names: list[str]
    confidence: float
    unmatched_indices: list[int]


def _anchor_node(snapped: SnappedPoint) -> str:
    """The nearer of a matched SnappedPoint's edge endpoints, by snap
    fraction — see the module docstring's stitching section for why
    match_trace anchors here instead of trimming the edge geometry."""
    a, b = snapped.edge
    return a if snapped.fraction < 0.5 else b


def match_trace(points: list[Mapping[str, float]], mode: str = "walk") -> MatchedRoute:
    """Stitch `points`' MATCHED snapped positions (see snap_trace) into one
    continuous route over `mode`'s street graph — see the module docstring
    for the stitching algorithm, the outlier guard, road-name aggregation,
    and the confidence formula.

    Raises ValueError if len(points) exceeds MAX_TRACE_POINTS, or
    routing.UnsupportedMode for an unknown mode string (both via
    snap_trace's own validation, run first).
    """
    if not points:
        return MatchedRoute(0.0, [], [], 0.0, [])

    graph, snapped = _snap_trace_with_graph(points, mode)

    unmatched_indices: list[int] = [sp.index for sp in snapped if not sp.matched]
    stitchable = [sp for sp in snapped if sp.matched]

    if not stitchable:
        return MatchedRoute(0.0, [], [], 0.0, sorted(unmatched_indices))

    anchor = stitchable[0]
    accepted: list[SnappedPoint] = [anchor]
    geometry: list[tuple[float, float]] = [graph.coords[_anchor_node(anchor)]]
    matched_length_m = 0.0
    road_names: list[str] = []

    for candidate in stitchable[1:]:
        straight_m = routing._haversine_m(
            anchor.snapped_lat, anchor.snapped_lon, candidate.snapped_lat, candidate.snapped_lon
        )
        result = routing._dijkstra_path_to_target(
            graph, _anchor_node(anchor), _anchor_node(candidate), 1.0
        )
        routed_m = result[1] if result is not None else math.inf
        threshold_m = max(
            STITCH_OUTLIER_RATIO_K * straight_m, straight_m + STITCH_OUTLIER_MIN_SLACK_M
        )
        if routed_m > threshold_m:
            unmatched_indices.append(candidate.index)
            continue

        _elapsed_s, _dist_m, path = result
        for (node_a, _cum_a), (node_b, _cum_b) in zip(path, path[1:]):
            geometry.extend(_edge_polyline(graph, node_a, node_b)[1:])
            name = graph.name_between(node_a, node_b)
            if name and (not road_names or road_names[-1] != name):
                road_names.append(name)
        matched_length_m += routed_m
        accepted.append(candidate)
        anchor = candidate

    matched_count = len(accepted)
    if matched_count == 0:
        confidence = 0.0
    else:
        mean_snap_distance_m = sum(sp.distance_m for sp in accepted) / matched_count
        distance_factor = max(0.0, 1.0 - mean_snap_distance_m / routing.SNAP_RADIUS_M)
        confidence = (matched_count / len(points)) * distance_factor

    return MatchedRoute(
        matched_length_m=round(matched_length_m, 1),
        geometry=geometry,
        road_names=road_names,
        confidence=round(confidence, 4),
        unmatched_indices=sorted(set(unmatched_indices)),
    )
