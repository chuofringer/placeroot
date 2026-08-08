"""Walking/cycling/driving isochrones from Overture's transportation theme.

Extracts a bounded street graph (segments -> connector nodes + edges)
around a point, runs Dijkstra out to a time budget, and returns the
reachable-node set as a polygon plus stats.

Node model: every connectors entry on a segment — endpoints (at == 0.0 or
1.0) and interior connectors (0 < at < 1, e.g. a mid-segment intersection
with another segment) — becomes a graph node. A segment with connectors at
[0.0, 0.4, 1.0] is split into two edges (0.0->0.4, 0.4->1.0). Overture's
`at` is a linear-reference fraction of the segment's *arc length*, not of
its vertex count, so an edge's length is simply
`abs(at_b - at_a) * segment_length_m` — no re-walking of the vertex list
is needed for length; a vertex list walk is only used to interpolate the
(lat, lon) of an interior split point for the node's coordinates. Because
node identity is keyed by the connector id, two segments that share only
an interior connector (a real-world crossing that isn't a shared endpoint)
now unify onto the same node and the crossing is routable.

Snapping: the origin doesn't just snap to the nearest node overall.
Overture data sometimes has small disconnected fragments (a short sidewalk
pair with no link to the rest of the graph) that can be geometrically
closer to the query point than the real street network. After building
the graph, snap_to_graph() computes connected components and prefers the
nearest node within SNAP_RADIUS_M whose component has at least
MIN_USABLE_COMPONENT_NODES nodes, skipping over closer nodes that belong
to tiny fragments (logged when this happens). If nothing within the snap
radius sits in a usable component, isochrone() raises NoGraphNearby, same
as when the graph is empty.

Modes (#38): "walk" (constant DEFAULT_SPEED_M_S), "cycle" (constant
CYCLE_SPEED_M_S, excludes motorway/trunk), "drive" (per-edge speed from
Overture's `speed_limits` column where present, else a class-based default
table — see DRIVE_CLASS_SPEEDS_M_S — excludes footway/path/steps/
pedestrian/cycleway). Each mode has its own extraction radius cap
(MODE_CONFIG[mode]["max_radius_m"]). Walk mode ignores one-way; cycle and
drive respect it via `access_restrictions` (real Overture: a list of
structs with access_type ["allowed"|"denied"|"designated"] and an optional
`when.heading` of "forward"/"backward" naming which direction, along the
segment's digitization order, is restricted) — a one-way segment becomes a
directed edge instead of two. Graph.add_edge's `directed` flag threads
this through; dijkstra doesn't change at all, since it already only
follows a node's *outgoing* adjacency entries.

Edge weight units: for walk/cycle (constant speed per mode), adjacency
weights are plain meters and dijkstra divides by the mode's speed_m_s at
query time — unchanged from the original design, and independent of which
speed_m_s a caller passes (so the graph itself is speed-independent and
cacheable, see #39). For drive with no explicit speed_m_s override, the
per-edge speed varies (speed_limit or class default), so build_graph bakes
each edge's weight as `length_m / edge_speed_m_s` — already seconds — and
callers must invoke dijkstra with speed_m_s=1.0 for that graph.
Graph.weight_is_time records which convention a given Graph uses.

Polygon method (#36): reached nodes are bucketed onto a grid (cell size
max(60m, reached_radius/40)) and the boundary of the occupied-cell union
is traced (a simple square/marching-squares-style contour walk) and
simplified via simplify.py to the token budget — a much tighter fit around
concave boundaries (e.g. a river with one bridge no longer shows an
isochrone that floods across the water) than a convex hull, while still
always producing one valid, closed ring. Below CONCAVE_MIN_NODES reached
nodes (too few to bucket meaningfully) or when the boundary trace fails to
find a loop, isochrone() falls back to the convex hull, same as before
#36. Either way the *stats* (reachable node count, per-node distances) are
exact; only the drawn polygon shape approximates.

Graph cache (#39): isochrone() reuses a previously-built Graph when the
newly-needed extraction area is fully contained within a cached graph's
(deliberately over-fetched) area, keyed by (release, upstream source,
mode, drive-speed-baking) — see _get_or_build_graph.

Graph size cap (#73, defense in depth): DRIVE_MAX_RADIUS_M bounds the
extraction *radius*, not the graph's node/edge count — a dense-enough urban
core within that radius can still pull a very large graph. build_graph caps
the number of segment rows it reads from upstream via MAX_GRAPH_SEGMENTS (a
LIMIT on the extraction query, so the cap stops the scan itself rather than
letting an unbounded result set be pulled and only noticing after the fact).
When the cap is hit, the returned Graph is marked truncated=True and
isochrone() surfaces that as a `truncated: true` top-level flag plus a note
in `stats`, so the result is honestly a partial graph rather than either an
unbounded one or a silent undercount.

Corridor search (#171): places_along_route() answers "what's on the way
from A to B" by keeping the node path of the same shortest path route()
computes (_dijkstra_path_to_target) and measuring find_places-style
candidates from the places theme against it — see that function for the
detour cost model.

Query layer: this module shares the package-wide connection, bbox
helpers, and schema probe (via db.py/geo.py); db.ensure_spatial() is
called explicitly here since this module needs the spatial extension.
"""

import heapq
import logging
import math
import threading
from collections import OrderedDict

import duckdb

from placeroot import budget, cache, db, errors, geo, overture, release, simplify
from placeroot.errors import UpstreamUnavailable  # noqa: F401 - re-exported; see below

logger = logging.getLogger(__name__)

THEME = "transportation"

DEFAULT_SPEED_M_S = 1.4  # ~5 km/h walking pace
CYCLE_SPEED_M_S = 4.2  # ~15 km/h cycling pace
WALK_MAX_RADIUS_M = 5000.0  # walking isochrones never need to look further than this
CYCLE_MAX_RADIUS_M = 15000.0
DRIVE_MAX_RADIUS_M = 60000.0
RADIUS_BUFFER = 1.25  # street paths aren't straight lines; pad the extraction radius
MAX_POLYGON_POINTS = 100  # decimation cap before the token-budget pass (convex hull path)

SNAP_RADIUS_M = 300.0  # how far the origin may snap to reach a usable graph node
MIN_USABLE_COMPONENT_NODES = 5  # components smaller than this are treated as fragments

# Defense in depth against a dense-urban drive-mode extraction pulling an
# unbounded graph (radius alone doesn't bound density — a 60km DRIVE_MAX_RADIUS_M
# circle over a dense city core can contain far more street network than the
# same radius in a sparse area; a measured 10-minute drive already pulled
# ~40k nodes). MAX_GRAPH_SEGMENTS caps the number of segment rows build_graph
# will read from upstream via a LIMIT on the extraction query — cheaper than
# letting an unbounded scan build an unbounded in-memory graph and only
# noticing afterward. Chosen well above any real query (a segment yields at
# most a handful of nodes/edges, so 200k segments is a very large graph, on
# the order of a small metro area) but finite, so a pathological area can't
# grow the graph without limit. When the cap is hit, build_graph marks the
# resulting Graph truncated=True rather than silently returning a partial
# graph as if it were complete.
MAX_GRAPH_SEGMENTS = 200_000

# Build-time pruning tolerance (m) for the per-edge shape vertices a Graph
# retains so route(include_path=True) can follow the road instead of
# chording it — see _edge_shape. A vertex this close to the line its
# neighbours already draw is noise at street scale; keeping it would cost
# memory in every cached graph for detail no caller can act on.
GRAPH_SHAPE_EPSILON_M = 2.0

# Overture road classes a pedestrian cannot use. Everything else (footway,
# path, residential, service, tertiary, living_street, cycleway, steps,
# unclassified, unknown, ...) is treated as walkable.
EXCLUDED_WALK_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link"}

# Cyclists share the walk exclusions (freeways) but not footway/path/etc,
# which remain cyclable.
EXCLUDED_CYCLE_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link"}

# Drivers can use anything except pedestrian/cycle-only infrastructure.
EXCLUDED_DRIVE_CLASSES = {"footway", "path", "steps", "pedestrian", "cycleway"}

# Class-based fallback driving speeds (m/s), used per-edge whenever a
# segment's `speed_limits` column is absent/empty/unparseable. Deliberately
# conservative city-street guesses, not a claim of accuracy — real posted
# limits (read from speed_limits when present) always take precedence.
# Roughly: motorway ~97km/h, primary ~47km/h, residential ~29km/h.
DRIVE_CLASS_SPEEDS_M_S = {
    "motorway": 27.0,
    "motorway_link": 20.0,
    "trunk": 24.0,
    "trunk_link": 18.0,
    "primary": 13.0,
    "primary_link": 11.0,
    "secondary": 11.0,
    "secondary_link": 9.0,
    "tertiary": 9.0,
    "tertiary_link": 8.0,
    "residential": 8.0,
    "living_street": 4.0,
    "service": 5.0,
    "unclassified": 8.0,
}
DRIVE_DEFAULT_CLASS_SPEED_M_S = 8.0  # unknown/missing class, ~residential pace
DRIVE_FASTEST_CLASS_SPEED_M_S = max(DRIVE_CLASS_SPEEDS_M_S.values())

# access_restrictions `when.mode` tokens (Overture's vehicle-type vocabulary)
# that count as "applies to this placeroot mode". An entry with no mode list
# at all applies to every non-walk mode (walk never consults restrictions).
RESTRICTION_MODE_TOKENS = {
    "cycle": {"bicycle"},
    "drive": {"motorVehicle", "car", "hgv", "motorcycle"},
}

MODE_CONFIG = {
    "walk": {
        "excluded_classes": EXCLUDED_WALK_CLASSES,
        "default_speed_m_s": DEFAULT_SPEED_M_S,
        "max_radius_m": WALK_MAX_RADIUS_M,
        "respects_oneway": False,
    },
    "cycle": {
        "excluded_classes": EXCLUDED_CYCLE_CLASSES,
        "default_speed_m_s": CYCLE_SPEED_M_S,
        "max_radius_m": CYCLE_MAX_RADIUS_M,
        "respects_oneway": True,
    },
    "drive": {
        "excluded_classes": EXCLUDED_DRIVE_CLASSES,
        "default_speed_m_s": None,  # None => per-edge variable speed, see DRIVE_CLASS_SPEEDS_M_S
        "max_radius_m": DRIVE_MAX_RADIUS_M,
        "respects_oneway": True,
    },
}

# route(): a per-mode straight-line-distance cap, rejected before any graph
# extraction is attempted. Deliberately conservative — real road distance is
# always >= straight-line distance, so a straight-line distance beyond a
# mode's cap can never produce a route worth extracting a graph for.
#
# This cap must be the TRUE, enforced limit, not just an advertised one:
# route()'s extraction radius is base_radius_m = max(straight_line_m / 2 *
# RADIUS_BUFFER + SNAP_RADIUS_M, ROUTE_MIN_RADIUS_M), and build_graph refuses
# any radius beyond MODE_CONFIG[mode]["max_radius_m"] (the isochrone
# extraction cap). That radius cap binds *before* a hard-coded straight-line
# cap would if the two aren't derived from the same relation — e.g. walk's
# radius cap of 5km already rejects two points 7.6km apart, well under a
# naively-chosen 25km "cap". So instead of an independent constant, invert
# the radius relation to get the largest straight-line distance that stays
# within max_radius_m: straight_line_max = 2 * (max_radius_m - SNAP_RADIUS_M)
# / RADIUS_BUFFER. Deriving it this way keeps the two caps in sync
# automatically if MODE_CONFIG's radii ever change, and makes the
# straight-line check below and the base_radius_m > max_radius_m check
# below it equivalent (the latter becomes a redundant safety net that
# should essentially never fire, modulo floating point).
ROUTE_MAX_STRAIGHT_LINE_M = {
    mode: 2.0 * (cfg["max_radius_m"] - SNAP_RADIUS_M) / RADIUS_BUFFER
    for mode, cfg in MODE_CONFIG.items()
}
ROUTE_MIN_RADIUS_M = 500.0  # extraction radius floor, so very-close points still get a real graph
ROUTE_RADIUS_RETRY_FACTOR = 1.6  # widen-and-retry factor when the first extraction misses a path

# places_along_route (#171): how far off the route a place may sit and still
# count as "on the way". CORRIDOR_MAX_DETOUR_M caps the caller's
# max_detour_m — the corridor bbox grows with it in both dimensions, so an
# unbounded value would turn a cross-town route into a metro-wide place
# scan. 5km is already far wider than any "on the way" question (it's a
# ~10km round-trip detour) while keeping the candidate box bounded.
CORRIDOR_DEFAULT_DETOUR_M = 1000.0
CORRIDOR_MAX_DETOUR_M = 5000.0
# Cost bound for the per-place corridor test, which is O(candidates x path
# segments) of pure-Python point-to-segment math. A long drive route can
# settle tens of thousands of nodes; scanning every one against every
# candidate is the only expensive part of this tool. Above this count the
# path is evenly subsampled, endpoints always kept.
#
# Since the test measures against the *polyline* through the retained nodes
# (not the nodes themselves), subsampling costs far less accuracy than it
# used to: what's lost is only the difference between the real path and the
# chords across the dropped runs. That error runs both ways — a chord can
# cut a corner the street actually takes, admitting a place the real route
# passes farther from, or bulge inside a curve and exclude one — and is
# bounded by how far the dropped nodes stray from their chord. On a
# city-scale route thinned to 1000 nodes that is metres against a
# max_detour_m measured in hundreds.
CORRIDOR_MAX_PATH_NODES = 1000
# One bbox over a whole route lets a dense stretch consume the entire
# overture.BBOX_MAX_CANDIDATES budget and starve the rest of the corridor:
# the SQL cap slices by the query's ORDER BY, not by geography, so 500
# alphabetically-early places clustered at one end can hide every match
# elsewhere. Instead the path is cut into this many chunks, each querying
# its own tight box with its own share of the budget, so coverage is
# spatially uniform and the scanned area is much smaller. Consecutive
# chunks overlap by one node, so every path *segment* lies wholly inside
# some chunk's box and no slice of the corridor falls between two boxes.
CORRIDOR_BBOX_CHUNKS = 20

CONCAVE_MIN_NODES = 8  # fewer reached nodes than this: convex hull is used directly
CONCAVE_CELL_MIN_M = 60.0
CONCAVE_CELL_DIVISOR = 40.0  # cell size ~= max(CONCAVE_CELL_MIN_M, reached_radius_m / this)

GRAPH_CACHE_MAXSIZE = 8
GRAPH_CACHE_MARGIN = 1.3  # over-fetch factor so nearby repeat queries hit the cache

REQUIRED_COLUMNS = ["id", "geometry", "bbox", "class", "connectors"]
ESSENTIAL_COLUMNS = {"geometry", "bbox"}

EARTH_RADIUS_M = 6371000.0

# Thin aliases so tests and benchmarks can reach the shared db/geo
# helpers through this module; import db/geo directly in new code.
_conn_lock = db.conn_lock
_conn = db.shared_conn
_bbox_around = geo.bbox_around
_bbox_filter_sql = geo.bbox_filter_sql


class UnsupportedMode(Exception):
    def __init__(self, mode: str):
        detail = f"unsupported mode {mode!r}; supported: {sorted(MODE_CONFIG)}"
        super().__init__(detail)
        self.detail = detail
        self.mode = mode


class SchemaDegraded(errors.SchemaDegraded):
    """Same as overture.SchemaDegraded, just labeled for the transportation
    dataset — see errors.py for why the message text differs."""

    def __init__(self, missing: list[str]):
        super().__init__(missing, dataset="transportation dataset")


class NoGraphNearby(Exception):
    def __init__(self, lat: float, lon: float, radius_m: float):
        detail = (
            f"no walkable segments found within {radius_m:.0f}m of ({lat}, {lon})"
        )
        super().__init__(detail)
        self.detail = detail


class RadiusTooLarge(Exception):
    def __init__(self, radius_m: float, max_radius_m: float):
        detail = f"radius_m={radius_m:.0f} exceeds the {max_radius_m:.0f}m cap for this mode"
        super().__init__(detail)
        self.detail = detail
        self.radius_m = radius_m
        self.max_radius_m = max_radius_m


class RouteTooLong(Exception):
    """The straight-line distance between the two points exceeds the mode's cap.

    Real road distance is always >= straight-line distance, so this is a
    conservative reject: a straight-line distance beyond the cap can never
    produce a route we'd be willing to extract a graph for anyway.
    """

    def __init__(self, distance_m: float, max_distance_m: float):
        detail = (
            f"straight-line distance {distance_m:.0f}m exceeds the "
            f"{max_distance_m:.0f}m cap for this mode"
        )
        super().__init__(detail)
        self.detail = detail
        self.distance_m = distance_m
        self.max_distance_m = max_distance_m


def set_data_path(path: str | None) -> None:
    """Point the query layer at a local dataset instead of live S3. Tests only.

    Delegates to overture.set_data_path's per-theme+type override (issue
    #40) — the same mechanism buildings.py uses for its own type override.
    """
    overture.set_data_path(path, theme=THEME, type_="segment")


def _upstream_glob() -> str:
    # type_="segment" matches this module's upstream path
    # (theme=transportation/type=segment); overture._upstream_glob also
    # honors PLACEROOT_TRANSPORTATION_DATA_PATH as a fallback for this
    # theme specifically.
    return overture._upstream_glob(THEME, type_="segment")


def missing_columns(glob: str) -> list[str]:
    present = db.probe_schema(glob)
    if present is None:
        return []
    return [c for c in REQUIRED_COLUMNS if c not in present]


def _check_schema(glob: str) -> list[str]:
    missing = missing_columns(glob)
    essential_missing = [c for c in missing if c in ESSENTIAL_COLUMNS]
    if essential_missing:
        raise SchemaDegraded(essential_missing)
    return missing


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _cumulative_lengths_m(points: list[tuple[float, float]]) -> list[float]:
    """Cumulative haversine arc length (m) at each vertex, points[0] -> 0.0."""
    cum = [0.0]
    for i in range(len(points) - 1):
        lon1, lat1 = points[i]
        lon2, lat2 = points[i + 1]
        cum.append(cum[-1] + _haversine_m(lat1, lon1, lat2, lon2))
    return cum


def _point_at_fraction(
    points: list[tuple[float, float]], at: float, cum: list[float]
) -> tuple[float, float]:
    """(lat, lon) at arc-length fraction `at` (0..1) along points, per cum lengths.

    `at` is Overture's linear-reference fraction of the segment's *arc
    length*, not of the vertex index — so we walk the cumulative-length
    table to find which vertex pair straddles the target distance, then
    interpolate linearly within that pair.
    """
    total = cum[-1]
    if at <= 0.0 or total <= 0.0:
        lon, lat = points[0]
        return lat, lon
    if at >= 1.0:
        lon, lat = points[-1]
        return lat, lon
    target = at * total
    i = 0
    while i < len(cum) - 2 and cum[i + 1] < target:
        i += 1
    seg_len = cum[i + 1] - cum[i]
    frac = (target - cum[i]) / seg_len if seg_len > 0 else 0.0
    lon1, lat1 = points[i]
    lon2, lat2 = points[i + 1]
    return lat1 + frac * (lat2 - lat1), lon1 + frac * (lon2 - lon1)


def _edge_shape(
    points: list[tuple[float, float]], cum: list[float], at_a: float, at_b: float
) -> tuple[list[tuple[float, float]], float]:
    """(source vertices strictly between at_a and at_b, meters dropped pruning them).

    The counterpart to _point_at_fraction: where that interpolates the node
    position at a connector's `at`, this returns the real shape vertices
    that fall in the open interval between two consecutive stops, i.e. the
    road's actual bends inside one graph edge. Strict comparisons keep the
    edge's own endpoints out of the list (they are graph nodes already and
    are emitted from Graph.coords).

    Vertices within GRAPH_SHAPE_EPSILON_M of the line their neighbours
    already describe are dropped here, at build time. Every graph carries
    these shapes whether or not anyone asks for a path, and a gridded city
    spends most of its vertices restating "still straight" — the worst-case
    extraction (60 km drive radius over Manhattan, capped at
    MAX_GRAPH_SEGMENTS) keeps a graph that would otherwise be over half
    shape data. The dropped vertices are sub-lane-width detail, well under
    the accuracy of the source geometry itself — but the second return value
    says exactly how far off the retained shape is, so route() can report
    that rather than a 0.0 it hasn't earned.
    """
    total = cum[-1]
    if total <= 0.0:
        return [], 0.0
    lo, hi = at_a * total, at_b * total
    interior = [p for p, d in zip(points, cum) if lo < d < hi]
    if not interior:
        return [], 0.0
    lat_a, lon_a = _point_at_fraction(points, at_a, cum)
    lat_b, lon_b = _point_at_fraction(points, at_b, cum)
    line = [(lon_a, lat_a), *interior, (lon_b, lat_b)]
    mpd_lat = simplify.METERS_PER_DEGREE_LAT
    mpd_lon = mpd_lat * max(math.cos(math.radians(lat_a)), 1e-6)
    projected = [(lon * mpd_lon, lat * mpd_lat) for lon, lat in line]
    kept = simplify._rdp_keep_indices(projected, GRAPH_SHAPE_EPSILON_M)
    dropped_m = simplify._max_deviation_m(projected, kept)
    return [line[i] for i in kept if 0 < i < len(line) - 1], dropped_m


def _parse_linestring_wkt(wkt: str) -> list[tuple[float, float]]:
    """"LINESTRING (lon1 lat1, lon2 lat2, ...)" -> [(lon, lat), ...]."""
    inner = wkt.strip()
    inner = inner[inner.index("(") + 1 : inner.rindex(")")]
    points = []
    for pair in inner.split(","):
        lon_str, lat_str = pair.strip().split()
        points.append((float(lon_str), float(lat_str)))
    return points


def _from_source(bbox: tuple[float, float, float, float]) -> str:
    upstream = _upstream_glob()
    if cache.enabled():
        try:
            # db.new_connection as the background-fetch factory: the tile
            # COPY only needs httpfs, already covered by db.py's shared
            # connection-configuration site (issue #40).
            with db.conn_lock:
                paths = cache.local_paths_for_query(
                    db.shared_conn(), release.resolve_release(), THEME, bbox, upstream,
                    db.new_connection,
                )
        except duckdb.Error as e:
            raise UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


class Graph:
    """Weighted graph: node_id -> [(neighbor_id, weight, length_m), ...].

    length_m is the edge's true physical length regardless of weight's
    units — for constant-speed modes (walk/cycle) weight == length_m, but
    for drive weight is baked seconds (length_m / edge_speed_m_s), so
    length_m is carried separately so route() can report exact distance
    even for drive-mode (time-weighted) graphs. dijkstra() ignores it.

    Undirected by default; one-way segments (#38) add a directed edge via
    add_edge(..., directed=True), which only links a -> b. weight_is_time
    is False (the common case: constant-speed modes) when weight is plain
    length_m, meant to be divided by a mode speed_m_s at dijkstra time; it's
    True for a drive-mode graph whose per-edge weight was already baked to
    seconds at build time (variable speed per class/speed-limit) — such a
    graph must be queried with dijkstra(..., speed_m_s=1.0).

    Nodes are segment endpoints and interior connectors only, so an edge's
    two node coordinates are a chord across whatever the road actually
    does in between; the intermediate shape vertices are kept separately
    (see shape_between) so route()'s emitted geometry can follow the road.
    """

    def __init__(self):
        self.adjacency: dict[str, list[tuple[str, float, float]]] = {}
        self.coords: dict[str, tuple[float, float]] = {}  # node_id -> (lat, lon)
        self.weight_is_time: bool = False
        # True when build_graph's segment extraction hit MAX_GRAPH_SEGMENTS
        # and stopped early — this graph (and any isochrone built from it) is
        # a partial view of the actual street network in the query area.
        self.truncated: bool = False
        # Both directions of every edge, regardless of `directed` — used only
        # for connected_components()/snapping, which should treat a fragment
        # linked exclusively by one-way edges as connected either way (a
        # street being one-way for cars doesn't disconnect it as a
        # geometric/topological fragment).
        self._undirected_neighbors: dict[str, set[str]] = {}
        # (node_a, node_b) -> (weight, [(lon, lat), ...], dropped_m): the
        # source segment's shape vertices strictly *between* the two nodes,
        # in a -> b order, plus how far GRAPH_SHAPE_EPSILON_M pruning moved
        # the retained shape off the source geometry. Only edges that
        # actually bend carry an entry, so a grid of straight segments
        # stores nothing. Read back through shape_between() so an emitted
        # route line follows the real road rather than cutting the chord
        # across every curve (#161 sweep). Parallel edges between the same
        # node pair keep the lowest-weight shape, which is the one dijkstra
        # would have taken.
        self._edge_shapes: dict[
            tuple[str, str], tuple[float, list[tuple[float, float]], float]
        ] = {}

    def add_node(self, node_id: str, lat: float, lon: float) -> None:
        self.adjacency.setdefault(node_id, [])
        self._undirected_neighbors.setdefault(node_id, set())
        self.coords.setdefault(node_id, (lat, lon))

    def add_edge(
        self,
        a: str,
        b: str,
        weight: float,
        length_m: float,
        directed: bool = False,
        shape: list[tuple[float, float]] | None = None,
        shape_dropped_m: float = 0.0,
    ) -> None:
        if a == b:
            return
        self.adjacency[a].append((b, weight, length_m))
        self._undirected_neighbors[a].add(b)
        self._undirected_neighbors[b].add(a)
        if not directed:
            self.adjacency[b].append((a, weight, length_m))
        # An edge whose every shape vertex was pruned away still gets an
        # entry: its shape is empty but its dropped_m isn't, and losing that
        # would put the emitted line right back on the chord while reporting
        # a deviation of 0.0.
        if shape or shape_dropped_m:
            existing = self._edge_shapes.get((a, b))
            if existing is None or weight < existing[0]:
                self._edge_shapes[(a, b)] = (weight, shape or [], shape_dropped_m)

    def shape_between(self, a: str, b: str) -> tuple[list[tuple[float, float]], float]:
        """(shape vertices strictly between `a` and `b` in a -> b order, meters dropped).

        Empty and 0.0 when the edge is a straight chord (or unknown). An
        undirected edge is stored once, under the orientation build_graph
        walked the source segment in, so the reverse orientation is served
        by reversing the stored vertex list.
        """
        entry = self._edge_shapes.get((a, b))
        if entry is not None:
            return entry[1], entry[2]
        entry = self._edge_shapes.get((b, a))
        if entry is not None:
            return list(reversed(entry[1])), entry[2]
        return [], 0.0

    def node_count(self) -> int:
        return len(self.adjacency)

    def edge_count(self) -> int:
        """Undirected edge count: each undirected edge counts once, each
        directed (one-way) edge also counts once (it's stored only once
        internally, unlike an undirected edge's two adjacency entries). An
        undirected edge appears as both (a,b) and (b,a) adjacency entries, a
        directed one only as (a,b) — count entries per unordered pair to
        tell them apart cheaply."""
        counts: dict[tuple[str, str], int] = {}
        for a, neighbors in self.adjacency.items():
            for b, _weight, _length in neighbors:
                key = (min(a, b), max(a, b))
                counts[key] = counts.get(key, 0) + 1
        return sum(1 if c == 1 else c // 2 for c in counts.values())

    def nearest_node(self, lat: float, lon: float) -> str | None:
        best_id, best_d = None, math.inf
        for node_id, (nlat, nlon) in self.coords.items():
            d = _haversine_m(lat, lon, nlat, nlon)
            if d < best_d:
                best_id, best_d = node_id, d
        return best_id

    def connected_components(self) -> list[set[str]]:
        """Node ids grouped by connectivity (weak/undirected BFS — see
        _undirected_neighbors)."""
        seen: set[str] = set()
        components: list[set[str]] = []
        for start in self.adjacency:
            if start in seen:
                continue
            component: set[str] = set()
            stack = [start]
            seen.add(start)
            while stack:
                node = stack.pop()
                component.add(node)
                for neighbor in self._undirected_neighbors.get(node, ()):
                    if neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)
            components.append(component)
        return components


def _convert_speed_to_m_s(value: float, unit: str | None) -> float:
    """Overture speed_limits values default to km/h when unit is absent/unrecognized."""
    unit = (unit or "km/h").strip().lower()
    if unit in ("mph", "mi/h"):
        return value * 0.44704
    return value / 3.6  # km/h, kph, kmh, or unrecognized — assume km/h


def _speed_limit_m_s(speed_limits: list | None) -> float | None:
    """Slowest whole-segment max_speed in speed_limits, in m/s, or None.

    Entries whose `between` linear-reference range doesn't cover the whole
    segment ([0.0, 1.0] or unset) are skipped — re-deriving a sub-range
    limit against each post-split edge's own [at_a, at_b] window is a
    documented follow-up, not attempted here. When several whole-segment
    entries apply (e.g. different `when` conditions), the slowest one is
    used, matching the conservative spirit of the class-based fallback.
    """
    if not speed_limits:
        return None
    best = None
    for entry in speed_limits:
        between = entry.get("between")
        if between:
            continue
        max_speed = entry.get("max_speed")
        if not max_speed:
            continue
        value = max_speed.get("value")
        if value is None:
            continue
        m_s = _convert_speed_to_m_s(value, max_speed.get("unit"))
        if m_s > 0 and (best is None or m_s < best):
            best = m_s
    return best


def _drive_edge_speed_m_s(cls: str | None, speed_limits: list | None) -> float:
    """Per-edge driving speed: posted speed_limits if present, else a class default."""
    limit = _speed_limit_m_s(speed_limits)
    if limit is not None:
        return limit
    return DRIVE_CLASS_SPEEDS_M_S.get(cls, DRIVE_DEFAULT_CLASS_SPEED_M_S)


def _oneway_allowed(access_restrictions: list | None, mode: str) -> tuple[bool, bool]:
    """(forward_allowed, backward_allowed) for `mode` along a segment's digitized order.

    Walk mode always ignores restrictions — pedestrians aren't bound by
    vehicle one-way rules — and returns (True, True) unconditionally. For
    cycle/drive, an access_restrictions entry only matters when its
    access_type is "denied": a `when.heading` of "forward" or "backward"
    names the disallowed direction (leaving the graph edge directed the
    other way); an entry with no heading at all denies both directions
    (the segment is skipped entirely for this mode). Entries whose
    `when.mode` list is present but doesn't include a token for this
    placeroot mode (see RESTRICTION_MODE_TOKENS) are ignored — a
    bicycle-only restriction shouldn't stop a car and vice versa.
    """
    if mode == "walk" or not access_restrictions:
        return True, True
    forward_allowed, backward_allowed = True, True
    mode_tokens = RESTRICTION_MODE_TOKENS.get(mode, set())
    for entry in access_restrictions:
        if entry.get("access_type") != "denied":
            continue
        when = entry.get("when") or {}
        modes = when.get("mode") or []
        if modes and not (set(modes) & mode_tokens):
            continue
        heading = when.get("heading")
        if heading == "forward":
            forward_allowed = False
        elif heading == "backward":
            backward_allowed = False
        else:
            forward_allowed = backward_allowed = False
    return forward_allowed, backward_allowed


def build_graph(
    lat: float, lon: float, radius_m: float, mode: str = "walk", speed_m_s: float | None = None
) -> Graph:
    """Street graph for `mode` within radius_m of (lat, lon).

    Raises RadiusTooLarge if radius_m exceeds MODE_CONFIG[mode]'s cap,
    SchemaDegraded if the transportation dataset is missing geometry/bbox,
    UnsupportedMode for an unknown mode string, or UpstreamUnavailable if
    the remote scan fails.

    speed_m_s, when given, overrides the mode's speed model with a single
    constant speed for every edge (bypassing speed_limits/class defaults
    for drive) — every mode's graph then stores plain length_m weights
    (Graph.weight_is_time stays False) and the caller divides by speed_m_s
    at dijkstra time, same as the original walk-only design. Only a
    speed_m_s-less "drive" call bakes per-edge time weights.

    Defense in depth against a dense-urban extraction pulling an unbounded
    graph within an otherwise-valid radius: the segment extraction query
    carries a LIMIT of MAX_GRAPH_SEGMENTS + 1 rows. When that limit is hit,
    only the first MAX_GRAPH_SEGMENTS rows are used to build the graph and
    the returned Graph's `truncated` flag is set True — a partial-but-honest
    graph rather than an unbounded one, or a silent undercount.
    """
    if mode not in MODE_CONFIG:
        raise UnsupportedMode(mode)
    config = MODE_CONFIG[mode]
    max_radius_m = config["max_radius_m"]
    if radius_m > max_radius_m:
        raise RadiusTooLarge(radius_m, max_radius_m)

    bake_time = mode == "drive" and speed_m_s is None

    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    xmin, ymin, xmax, ymax = geo.bbox_around(lat, lon, radius_m)
    bbox = (xmin, ymin, xmax, ymax)
    bbox_filter, bbox_params = geo.bbox_filter_sql(xmin, ymin, xmax, ymax)

    present = db.probe_schema(upstream)
    class_expr = "NULL" if "class" in missing else "class"
    connectors_expr = "NULL" if "connectors" in missing else "connectors"
    speed_limits_missing = present is not None and "speed_limits" not in present
    speed_limits_expr = "NULL" if speed_limits_missing else "speed_limits"
    access_missing = present is not None and "access_restrictions" not in present
    access_expr = "NULL" if access_missing else "access_restrictions"
    wkt_expr = geo.geom_expr(upstream, as_wkt=True)
    # Overture's transportation theme also carries rail (and other non-road)
    # segments under the same table; subtype isn't in our fixture (road-only
    # by construction) so this only filters when the column is actually
    # present, i.e. against real Overture data.
    subtype_filter = (
        "AND (subtype = 'road' OR subtype IS NULL)"
        if present is not None and "subtype" in present
        else ""
    )
    # LIMIT to one past the cap: fetching exactly MAX_GRAPH_SEGMENTS would
    # look identical whether the true result set was exactly that size or
    # much larger, so the extra row is how truncation is detected below
    # without a separate COUNT(*) query.
    sql = f"""
        SELECT
            id,
            {class_expr} AS class,
            {connectors_expr} AS connectors,
            {speed_limits_expr} AS speed_limits,
            {access_expr} AS access_restrictions,
            {wkt_expr} AS wkt
        FROM {_from_source(bbox)}
        WHERE {bbox_filter}
          {subtype_filter}
        LIMIT {MAX_GRAPH_SEGMENTS + 1}
    """
    params = bbox_params
    try:
        # The WKT expression above may need ST_AsText/ST_GeomFromWKB —
        # ensure the spatial extension is loaded on the shared connection
        # before running it (issue #40: this module used to load spatial
        # unconditionally at connection-creation time; now it's lazy, like
        # divisions.py/buildings.py). Called outside conn_lock (it takes
        # the lock itself) but still inside this try, so a load failure
        # surfaces as UpstreamUnavailable exactly like a query failure did.
        db.ensure_spatial()
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e

    truncated = len(rows) > MAX_GRAPH_SEGMENTS
    if truncated:
        rows = rows[:MAX_GRAPH_SEGMENTS]
        logger.warning(
            "build_graph: segment extraction hit MAX_GRAPH_SEGMENTS=%d within "
            "%.0fm of (%s, %s); graph is truncated",
            MAX_GRAPH_SEGMENTS, radius_m, lat, lon,
        )

    graph = Graph()
    graph.weight_is_time = bake_time
    graph.truncated = truncated
    excluded_classes = config["excluded_classes"]
    respects_oneway = config["respects_oneway"]
    for _id, cls, connectors, speed_limits, access_restrictions, wkt in rows:
        if cls is not None and cls in excluded_classes:
            continue
        try:
            points = _parse_linestring_wkt(wkt)
        except (ValueError, IndexError):
            continue
        if len(points) < 2:
            continue

        start_lon, start_lat = points[0]
        end_lon, end_lat = points[-1]
        start_id, end_id = None, None
        interior: list[tuple[float, str]] = []  # (at, connector_id), 0 < at < 1
        if connectors:
            for conn in connectors:
                at = conn["at"]
                if at <= 0.0:
                    start_id = conn["connector_id"]
                elif at >= 1.0:
                    end_id = conn["connector_id"]
                else:
                    interior.append((at, conn["connector_id"]))
        if start_id is None:
            start_id = f"pt_{round(start_lon, 6)}_{round(start_lat, 6)}"
        if end_id is None:
            end_id = f"pt_{round(end_lon, 6)}_{round(end_lat, 6)}"

        cum = _cumulative_lengths_m(points)
        total_length_m = cum[-1]

        # Node stops along the segment's arc-length parameterization, sorted
        # by `at`: the two endpoints plus any interior connectors (mid-segment
        # intersections). Each consecutive pair becomes one graph edge, so a
        # segment shared with another segment only via an interior connector
        # still gets linked into the graph at that connector's node.
        stops = [(0.0, start_id)] + sorted(interior) + [(1.0, end_id)]

        graph.add_node(start_id, start_lat, start_lon)
        graph.add_node(end_id, end_lat, end_lon)
        for at, connector_id in interior:
            ilat, ilon = _point_at_fraction(points, at, cum)
            graph.add_node(connector_id, ilat, ilon)

        forward_allowed, backward_allowed = (
            _oneway_allowed(access_restrictions, mode) if respects_oneway else (True, True)
        )
        if not forward_allowed and not backward_allowed:
            continue

        edge_speed_m_s = _drive_edge_speed_m_s(cls, speed_limits) if bake_time else None
        for (at_a, id_a), (at_b, id_b) in zip(stops, stops[1:]):
            edge_length_m = (at_b - at_a) * total_length_m
            weight = edge_length_m / edge_speed_m_s if bake_time else edge_length_m
            # edge_length_m is measured along the source geometry, so the
            # edge's real shape has to travel with it — otherwise the only
            # thing left of this segment's curves is a chord between two
            # nodes that is shorter than the length the router charges for
            # it (#161 sweep: a Conzelman Rd switchback came back as a
            # 2-point line 851 m short of its own distance_m).
            shape, dropped_m = _edge_shape(points, cum, at_a, at_b)
            if forward_allowed and backward_allowed:
                graph.add_edge(
                    id_a, id_b, weight, edge_length_m, directed=False,
                    shape=shape, shape_dropped_m=dropped_m,
                )
            elif forward_allowed:
                graph.add_edge(
                    id_a, id_b, weight, edge_length_m, directed=True,
                    shape=shape, shape_dropped_m=dropped_m,
                )
            else:
                graph.add_edge(
                    id_b, id_a, weight, edge_length_m, directed=True,
                    shape=list(reversed(shape)), shape_dropped_m=dropped_m,
                )

    return graph


def dijkstra(graph: Graph, source: str, max_seconds: float, speed_m_s: float) -> dict[str, float]:
    """node_id -> elapsed seconds, for every node reachable within max_seconds."""
    dist: dict[str, float] = {source: 0.0}
    heap = [(0.0, source)]
    while heap:
        d, node = heapq.heappop(heap)
        if d > dist.get(node, math.inf):
            continue
        if d > max_seconds:
            continue
        for neighbor, weight, _length_m in graph.adjacency[node]:
            nd = d + weight / speed_m_s
            if nd <= max_seconds and nd < dist.get(neighbor, math.inf):
                dist[neighbor] = nd
                heapq.heappush(heap, (nd, neighbor))
    return dist


def snap_to_graph(
    graph: Graph,
    lat: float,
    lon: float,
    snap_radius_m: float = SNAP_RADIUS_M,
    min_component_nodes: int = MIN_USABLE_COMPONENT_NODES,
) -> str | None:
    """Nearest usable-component node to (lat, lon), or None if none qualifies.

    "Usable" means: within snap_radius_m, and belonging to a connected
    component with at least min_component_nodes nodes (or the graph's
    largest component, if every component happens to be smaller than the
    threshold). This avoids snapping the origin onto a tiny disconnected
    fragment — e.g. an isolated sidewalk pair — that happens to sit closer
    to the query point than the real street network.
    """
    components = graph.connected_components()
    if not components:
        return None
    largest = max(components, key=len)
    component_of: dict[str, set[str]] = {}
    for component in components:
        for node_id in component:
            component_of[node_id] = component

    candidates = []
    for node_id, (nlat, nlon) in graph.coords.items():
        d = _haversine_m(lat, lon, nlat, nlon)
        if d <= snap_radius_m:
            candidates.append((d, node_id))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])

    nearest_d, nearest_id = candidates[0]
    nearest_component = component_of[nearest_id]
    if len(nearest_component) >= min_component_nodes or nearest_component is largest:
        return nearest_id

    # The nearest node is stuck in a small disconnected fragment; look
    # further out (still within the snap radius) for a node in a usable
    # component instead.
    for d, node_id in candidates[1:]:
        component = component_of[node_id]
        if len(component) >= min_component_nodes or component is largest:
            logger.info(
                "snap_to_graph: nearest node %s is %.1fm away in an isolated "
                "%d-node fragment; snapping to %s (%.1fm away, %d-node "
                "component) instead",
                nearest_id, nearest_d, len(nearest_component),
                node_id, d, len(component),
            )
            return node_id

    # Everything within the snap radius is a tiny fragment.
    return None


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Convex hull of (x, y) points via the monotone chain algorithm.

    Returns hull vertices in counter-clockwise order, not closed (first
    point isn't repeated at the end). No external geometry dependency.
    """
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _polygon_area_m2(
    hull_lonlat: list[tuple[float, float]], origin_lat: float, origin_lon: float
) -> float:
    """Planar (equirectangular, origin-centered) shoelace area — fine at this scale."""
    if len(hull_lonlat) < 3:
        return 0.0
    m_per_deg_lat = 110_540.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(origin_lat)), 1e-6)
    xy = [
        ((lon - origin_lon) * m_per_deg_lon, (lat - origin_lat) * m_per_deg_lat)
        for lon, lat in hull_lonlat
    ]
    area2 = 0.0
    n = len(xy)
    for i in range(n):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    return abs(area2) / 2.0


def decimate(points: list[tuple[float, float]], max_points: int) -> list[tuple[float, float]]:
    """Evenly-spaced decimation of a point sequence down to max_points."""
    if max_points < 3 or len(points) <= max_points:
        return points
    step = len(points) / max_points
    indices = sorted({int(i * step) for i in range(max_points)})
    return [points[i] for i in indices]


def _ring_to_geojson(ring_lonlat: list[tuple[float, float]]) -> dict:
    """Close (if needed) and wrap an (lon, lat) ring as a GeoJSON Polygon."""
    if not ring_lonlat:
        return {"type": "Polygon", "coordinates": []}
    ring = [[lon, lat] for lon, lat in ring_lonlat]
    if ring[0] != ring[-1]:
        ring.append(ring[0])  # GeoJSON polygons are closed rings
    return {"type": "Polygon", "coordinates": [ring]}


def point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test against a closed (lon, lat) ring."""
    inside = False
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > lat) != (y2 > lat):
            x_at_lat = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < x_at_lat:
                inside = not inside
    return inside


def _convex_polygon(
    lonlat_points: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], bool]:
    """Convex hull of lonlat_points, decimated to fit the point/token budgets.

    Returns (ring, truncated); ring is open (first != last).
    """
    hull = convex_hull(lonlat_points)
    ring = decimate(hull, MAX_POLYGON_POINTS)
    budget_tokens = budget.token_budget()
    truncated = len(ring) < len(hull)
    cap = len(ring)
    while cap > 4 and budget.estimate_tokens(_ring_to_geojson(ring)) > budget_tokens:
        cap = max(4, cap // 2)
        ring = decimate(ring, cap)
        truncated = True
    return ring, truncated


def _bbox_of(lonlat_points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    lons = [p[0] for p in lonlat_points]
    lats = [p[1] for p in lonlat_points]
    return min(lons), min(lats), max(lons), max(lats)


def _clamp_ring_to_bbox(
    ring: list[tuple[float, float]], bbox: tuple[float, float, float, float]
) -> list[tuple[float, float]]:
    xmin, ymin, xmax, ymax = bbox
    return [(min(max(lon, xmin), xmax), min(max(lat, ymin), ymax)) for lon, lat in ring]


def _grid_bucket(
    reached_coords: list[tuple[float, float]],  # (lat, lon)
    origin_lat: float,
    origin_lon: float,
    radius_for_cell_m: float,
) -> tuple[set[tuple[int, int]], float, float]:
    """Occupied (cx, cy) grid cells for reached_coords, plus the cell size in degrees.

    Cell size is max(CONCAVE_CELL_MIN_M, radius_for_cell_m / CONCAVE_CELL_DIVISOR)
    — coarser for a larger isochrone, so the boundary trace stays cheap, but
    never finer than CONCAVE_CELL_MIN_M so it doesn't fragment into noise at
    small radii. Cells are indexed relative to (origin_lat, origin_lon) using
    a single (origin-latitude) meters-per-degree scale, not each node's own
    latitude, so the grid stays a true regular lattice.
    """
    cell_m = max(CONCAVE_CELL_MIN_M, radius_for_cell_m / CONCAVE_CELL_DIVISOR)
    mpd_lat = 110_540.0
    mpd_lon = 111_320.0 * max(math.cos(math.radians(origin_lat)), 1e-6)
    cell_lat_deg = cell_m / mpd_lat
    cell_lon_deg = cell_m / mpd_lon
    occupied: set[tuple[int, int]] = set()
    for node_lat, node_lon in reached_coords:
        cx = math.floor((node_lon - origin_lon) / cell_lon_deg)
        cy = math.floor((node_lat - origin_lat) / cell_lat_deg)
        occupied.add((cx, cy))
    return occupied, cell_lon_deg, cell_lat_deg


def _trace_cell_boundary(occupied: set[tuple[int, int]]) -> list[tuple[int, int]] | None:
    """Longest closed boundary loop (grid-vertex integer coords) around occupied's union.

    Standard square/marching-squares-style contour trace: every occupied
    cell contributes a directed unit edge for each side that borders a
    non-occupied cell, oriented (south: +x, east: +y, north: -x, west: -y)
    so the occupied interior is always on the edge's left — the usual CCW
    boundary-tracing convention. The directed edges chain head-to-tail into
    one or more closed loops; if the occupied region isn't simply connected
    (rare here, since reached nodes come from one connected graph
    component) multiple loops can result, and the longest one is returned.
    Returns None if there's nothing to trace (occupied is empty).
    """
    out_edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(a: tuple[int, int], b: tuple[int, int]) -> None:
        out_edges.setdefault(a, []).append(b)

    for cx, cy in occupied:
        if (cx, cy - 1) not in occupied:
            add((cx, cy), (cx + 1, cy))  # south
        if (cx + 1, cy) not in occupied:
            add((cx + 1, cy), (cx + 1, cy + 1))  # east
        if (cx, cy + 1) not in occupied:
            add((cx + 1, cy + 1), (cx, cy + 1))  # north
        if (cx - 1, cy) not in occupied:
            add((cx, cy + 1), (cx, cy))  # west

    if not out_edges:
        return None

    visited: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    loops: list[list[tuple[int, int]]] = []
    for start in list(out_edges.keys()):
        for first_next in list(out_edges[start]):
            if (start, first_next) in visited:
                continue
            loop = [start]
            cur, nxt = start, first_next
            ok = True
            while True:
                visited.add((cur, nxt))
                loop.append(nxt)
                if nxt == start:
                    break
                candidates = out_edges.get(nxt)
                if not candidates:
                    ok = False
                    break
                chosen = next((c for c in candidates if (nxt, c) not in visited), None)
                if chosen is None:
                    ok = False
                    break
                cur, nxt = nxt, chosen
            if ok:
                loops.append(loop)
    if not loops:
        return None
    return max(loops, key=len)


def concave_boundary(
    reached_coords: list[tuple[float, float]],  # (lat, lon)
    origin_lat: float,
    origin_lon: float,
    radius_for_cell_m: float,
) -> list[tuple[float, float]] | None:
    """Closed (lon, lat) boundary ring of reached_coords' occupied-cell union, or None.

    None means the trace found no loop (shouldn't happen once the caller has
    already gated on CONCAVE_MIN_NODES, but degenerate inputs are handled
    defensively) — callers fall back to the convex hull in that case.
    """
    occupied, cell_lon_deg, cell_lat_deg = _grid_bucket(
        reached_coords, origin_lat, origin_lon, radius_for_cell_m
    )
    loop = _trace_cell_boundary(occupied)
    if not loop:
        return None
    return [
        (origin_lon + px * cell_lon_deg, origin_lat + py * cell_lat_deg) for px, py in loop
    ]


def _build_polygon(
    reached_coords: list[tuple[float, float]],  # (lat, lon)
    origin_lat: float,
    origin_lon: float,
    max_radius_m: float,
) -> tuple[list[tuple[float, float]], str, bool]:
    """(ring, polygon_method, truncated) for the reached-node set.

    ring is open (first != last) and, whichever method produced it, is
    guaranteed to sit within reached_coords' own bounding box — i.e. never
    larger than the old convex hull's bbox, even after grid bucketing pads
    edge cells slightly beyond their node's exact position.
    """
    lonlat_points = [(lon_, lat_) for lat_, lon_ in reached_coords]
    if len(reached_coords) < CONCAVE_MIN_NODES:
        ring, truncated = _convex_polygon(lonlat_points)
        return ring, "convex_hull", truncated

    raw_ring = concave_boundary(reached_coords, origin_lat, origin_lon, max(max_radius_m, 1.0))
    if raw_ring is None:
        ring, truncated = _convex_polygon(lonlat_points)
        return ring, "convex_hull", truncated

    budget_tokens = budget.token_budget()
    geojson_ring = [[lon_, lat_] for lon_, lat_ in raw_ring]
    simplified = simplify.simplify_geometry(
        {"type": "Polygon", "coordinates": [geojson_ring]}, max_tokens=budget_tokens
    )
    simplified_ring = [tuple(p) for p in simplified["geometry"]["coordinates"][0]]
    if len(simplified_ring) > 1 and simplified_ring[0] == simplified_ring[-1]:
        simplified_ring = simplified_ring[:-1]  # _ring_to_geojson re-closes it
    bbox = _bbox_of(lonlat_points)
    ring = _clamp_ring_to_bbox(simplified_ring, bbox)
    truncated = simplified["kept_points"] < simplified["original_points"]
    return ring, "concave_boundary", truncated


class _GraphCacheEntry:
    __slots__ = ("bbox", "graph")

    def __init__(self, bbox: tuple[float, float, float, float], graph: Graph):
        self.bbox = bbox
        self.graph = graph


_graph_cache: "OrderedDict[tuple, _GraphCacheEntry]" = OrderedDict()
_graph_cache_lock = threading.Lock()


def _bbox_contains(
    outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]
) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _graph_cache_speed_tag(mode: str, speed_m_s: float | None) -> str:
    """Cache-key discriminator for whether a graph's edge weights are speed-independent.

    walk/cycle graphs store plain length_m regardless of speed_m_s (the
    speed is only applied at dijkstra time), so every speed_m_s value for
    those modes shares one cache slot. Only a speed_m_s-less "drive" graph
    bakes per-edge time weights from speed_limits/class defaults — an
    explicit speed_m_s override for drive falls back to the same
    speed-independent length_m representation as walk/cycle.
    """
    return "baked" if (mode == "drive" and speed_m_s is None) else "raw"


def _graph_cache_tile(lat: float, lon: float, tile_deg: float = 0.05) -> tuple[int, int]:
    return math.floor(lat / tile_deg), math.floor(lon / tile_deg)


def clear_graph_cache() -> None:
    """Drop every cached graph. Used by tests; safe to call anytime."""
    with _graph_cache_lock:
        _graph_cache.clear()


def _get_or_build_graph(
    lat: float, lon: float, extraction_radius_m: float, mode: str, speed_m_s: float | None
) -> Graph:
    """build_graph(...), reusing a cached graph when possible (#39).

    A cached graph is reused when its (deliberately over-fetched, by
    GRAPH_CACHE_MARGIN) extraction bbox fully contains the bbox this query
    actually needs, and the cache key (release, upstream source, mode,
    speed-baking) matches exactly. On a miss, a fresh graph is built for
    GRAPH_CACHE_MARGIN x the requested radius (capped at the mode's max) so
    nearby repeat queries are likely to hit next time, and stored, evicting
    the least-recently-used entry once the cache exceeds GRAPH_CACHE_MAXSIZE.
    """
    release_key = release.resolve_release()
    upstream = _upstream_glob()
    key_prefix = (release_key, upstream, mode, _graph_cache_speed_tag(mode, speed_m_s))
    needed_bbox = _bbox_around(lat, lon, extraction_radius_m)

    with _graph_cache_lock:
        for key, entry in _graph_cache.items():
            if key[:4] != key_prefix:
                continue
            if _bbox_contains(entry.bbox, needed_bbox):
                _graph_cache.move_to_end(key)
                return entry.graph

    max_radius_m = MODE_CONFIG[mode]["max_radius_m"]
    padded_radius_m = min(extraction_radius_m * GRAPH_CACHE_MARGIN, max_radius_m)
    graph = build_graph(lat, lon, padded_radius_m, mode=mode, speed_m_s=speed_m_s)
    extraction_bbox = _bbox_around(lat, lon, padded_radius_m)

    with _graph_cache_lock:
        key = (*key_prefix, _graph_cache_tile(lat, lon))
        _graph_cache[key] = _GraphCacheEntry(extraction_bbox, graph)
        _graph_cache.move_to_end(key)
        while len(_graph_cache) > GRAPH_CACHE_MAXSIZE:
            _graph_cache.popitem(last=False)

    return graph


def isochrone(
    lat: float,
    lon: float,
    minutes: float = 15,
    mode: str = "walk",
    speed_m_s: float | None = None,
    radius_m: float | None = None,
) -> dict:
    """Isochrone from (lat, lon) for `mode` ("walk", "cycle", or "drive").

    speed_m_s overrides the mode's default speed model with a single
    constant (bypassing speed_limits/class defaults for drive). radius_m
    overrides the auto-derived graph extraction radius; if given and it
    exceeds the mode's cap (MODE_CONFIG[mode]["max_radius_m"]), raises
    RadiusTooLarge. Otherwise the extraction radius is derived from
    minutes/speed and silently capped — long isochrones may then
    undercount reachable nodes beyond the cap (documented follow-up:
    chained/paged graph extraction). Raises UnsupportedMode for an unknown
    mode string. minutes must be > 0 and radius_m (if given) must be >= 0,
    else raises ValueError.

    The polygon is a concave grid-boundary trace of reached nodes (#36),
    falling back to a convex hull when there are too few reached nodes to
    bucket meaningfully; either way the *stats* are exact, only the drawn
    shape approximates. The built graph is cached across calls (#39) —
    see _get_or_build_graph.
    """
    if minutes <= 0:
        raise ValueError("minutes must be greater than 0")
    if radius_m is not None and radius_m < 0:
        raise ValueError("radius_m must be non-negative")
    if mode not in MODE_CONFIG:
        raise UnsupportedMode(mode)
    config = MODE_CONFIG[mode]
    max_radius_m = config["max_radius_m"]

    max_seconds = minutes * 60
    const_speed = speed_m_s if speed_m_s is not None else config["default_speed_m_s"]
    buffer_speed = const_speed if const_speed is not None else DRIVE_FASTEST_CLASS_SPEED_M_S
    auto_radius = min(max_seconds * buffer_speed * RADIUS_BUFFER, max_radius_m)
    extraction_radius = radius_m if radius_m is not None else auto_radius
    if extraction_radius > max_radius_m:
        raise RadiusTooLarge(extraction_radius, max_radius_m)

    graph = _get_or_build_graph(lat, lon, extraction_radius, mode, speed_m_s)
    if graph.node_count() == 0:
        raise NoGraphNearby(lat, lon, extraction_radius)

    source = snap_to_graph(graph, lat, lon)
    if source is None:
        raise NoGraphNearby(lat, lon, extraction_radius)

    dijkstra_speed = 1.0 if graph.weight_is_time else const_speed
    reached = dijkstra(graph, source, max_seconds, dijkstra_speed)

    reached_coords = [graph.coords[n] for n in reached]  # (lat, lon)
    max_radius_reached_m = max(
        (_haversine_m(lat, lon, node_lat, node_lon) for node_lat, node_lon in reached_coords),
        default=0.0,
    )

    ring, polygon_method, truncated = _build_polygon(reached_coords, lat, lon, max_radius_reached_m)
    area_km2 = _polygon_area_m2(ring, lat, lon) / 1_000_000.0

    stats = {
        "reachable_nodes": len(reached),
        "max_radius_m": round(max_radius_reached_m, 1),
        "area_km2": round(area_km2, 4),
    }
    if graph.truncated:
        stats["graph_truncated"] = True
        stats["note"] = (
            f"graph extraction capped at {MAX_GRAPH_SEGMENTS} segments; "
            "reachable area may be undercounted"
        )

    result = {
        "center": {"lat": lat, "lon": lon},
        "minutes": minutes,
        "mode": mode,
        "speed_m_s": const_speed,
        "polygon": _ring_to_geojson(ring),
        "polygon_method": polygon_method,
        "stats": stats,
    }
    if truncated or graph.truncated:
        result["truncated"] = True
    return result


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _midpoint(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float
) -> tuple[float, float]:
    """(lat, lon) midpoint of two points, antimeridian-aware.

    A plain (from_lon + to_lon) / 2.0 is wrong near the +/-180 seam: for
    from_lon=179.99, to_lon=-179.99 (true midpoint ~180/-180) it gives
    ~0.0, on the opposite side of the globe. When the two longitudes are
    more than 180 degrees apart (the shorter path between them must cross
    the seam), unwrap by adding 360 to the smaller one before averaging,
    then re-wrap the result back into [-180, 180]. Latitude never needs
    this treatment. This is only the *center point* build_graph extracts
    around — _haversine_m (used for straight-line distance and dijkstra
    edge lengths) is already seam-correct on its own.
    """
    lat_mid = (from_lat + to_lat) / 2.0
    lon_a, lon_b = from_lon, to_lon
    if abs(lon_a - lon_b) > 180.0:
        if lon_a < lon_b:
            lon_a += 360.0
        else:
            lon_b += 360.0
    lon_mid = (lon_a + lon_b) / 2.0
    lon_mid = ((lon_mid + 180.0) % 360.0) - 180.0
    return lat_mid, lon_mid


def _dijkstra_path_to_target(
    graph: Graph, source: str, target: str, speed_m_s: float
) -> tuple[float, float, list[tuple[str, float]]] | None:
    """(elapsed_seconds, distance_m, path) of the min-time path source->target, or None.

    Target-terminated Dijkstra: identical relaxation to dijkstra() (only
    outgoing adjacency entries are followed, so directed/one-way edges are
    respected the same way), but tracks a second, distance_m accumulator in
    lockstep with the time accumulator, and returns as soon as `target` is
    popped off the heap (i.e. settled — its shortest time is final, same
    early-exit correctness argument as any single-target Dijkstra). Returns
    None if the heap empties before `target` is reached, meaning target is
    unreachable from source in this graph (different component, or a
    one-way maze that only lets traffic flow away from it).

    `path` is the node sequence from source to target, each paired with the
    cumulative route distance in meters at that node (source -> 0.0, target
    -> distance_m). It comes from a predecessor table written in lockstep
    with dist_to, so a node's recorded along-distance is always the distance
    along the very chain the predecessor table describes — see
    places_along_route, the caller that needs it. route() shares this same
    search through _shortest_path and reads the path only when its caller
    asked for geometry (include_path — see _path_linestring); otherwise it
    uses just the two costs.
    """
    if source == target:
        return 0.0, 0.0, [(source, 0.0)]
    time_to: dict[str, float] = {source: 0.0}
    dist_to: dict[str, float] = {source: 0.0}
    prev: dict[str, str] = {}
    heap = [(0.0, source)]
    while heap:
        t, node = heapq.heappop(heap)
        if t > time_to.get(node, math.inf):
            continue
        if node == target:
            path: list[tuple[str, float]] = []
            cur = node
            while True:
                path.append((cur, dist_to[cur]))
                if cur == source:
                    break
                cur = prev[cur]
            path.reverse()
            return t, dist_to[node], path
        for neighbor, weight, length_m in graph.adjacency[node]:
            nt = t + weight / speed_m_s
            if nt < time_to.get(neighbor, math.inf):
                time_to[neighbor] = nt
                dist_to[neighbor] = dist_to[node] + length_m
                prev[neighbor] = node
                heapq.heappush(heap, (nt, neighbor))
    return None


# Decimal places kept on emitted path coordinates. 6 dp is ~0.11 m at the
# equator — finer than any snapping or graph-node precision the router has —
# and rounding *before* the simplify pass matters: simplify_geometry's
# token-fit search measures the very coordinates that get returned, so
# rounding afterwards would invalidate its budget accounting.
PATH_COORD_PRECISION = 6

# Reserve for the fields that ride alongside "path" in the response
# ("path_max_deviation_m" and its number). Small and fixed; kept explicit so
# the path's own token cap can't eat the envelope it needs.
PATH_ENVELOPE_TOKENS = 12

# Below this many tokens there is no honest path to return: even a bare
# two-point LineString costs roughly this much. If the remaining budget is
# smaller, the path is dropped with a note rather than emitted truncated.
PATH_MIN_TOKENS = 30

# Said instead of a path when one couldn't be fitted. Priced against the
# budget before it's attached (see route) — at very small budgets even this
# doesn't fit, and the bare "path_omitted" flag is all that's left. Kept
# terse for exactly that reason: a note that costs more than the path it is
# apologizing for can only ever be dropped.
PATH_OMITTED_NOTE = (
    "omitted (not truncated) to fit the token budget; raise PLACEROOT_TOKEN_BUDGET"
)


def _path_linestring(
    graph: Graph, path: list[tuple[str, float]], max_tokens: int
) -> tuple[dict, float] | None:
    """(GeoJSON LineString, deviation_m from the source roads), or None if it can't fit max_tokens.

    `path` is _dijkstra_path_to_target's node sequence (source first, target
    last), so the emitted coordinates are already in A->B order; each node
    is read out of Graph.coords and flipped to GeoJSON's [lon, lat], with
    the traversed edge's own shape vertices (Graph.shape_between) spliced
    in between consecutive nodes so the line follows the road instead of
    cutting the chord across each curve. Without them the emitted line can
    be dramatically shorter than the distance_m reported alongside it, while
    a deviation of 0.0 claims it is exact.

    Simplification goes through the same simplify.simplify_geometry the
    isochrone polygon and building footprints use — a token budget, not a
    tolerance in degrees. RDP always keeps the first and last index, so the
    simplified line still starts at A's snapped node and ends at B's; the
    two assignments below re-pin them anyway so that guarantee is stated in
    code rather than inherited by assumption.

    Returns None when even the fully simplified line doesn't fit max_tokens
    (simplify_geometry returns its most-simplified best effort rather than
    failing, so the fit is re-checked here). Callers drop the path and say
    so — coordinates are never silently truncated, which would hand back a
    line that stops short of the destination while looking complete.
    """
    coords: list[list[float]] = []

    def push(lon: float, lat: float) -> None:
        point = [round(lon, PATH_COORD_PRECISION), round(lat, PATH_COORD_PRECISION)]
        # A shape vertex can round onto the node it sits next to; a repeated
        # position adds tokens and tells the reader nothing.
        if not coords or coords[-1] != point:
            coords.append(point)

    prev_node: str | None = None
    shape_dropped_m = 0.0
    for node_id, _along_m in path:
        latlon = graph.coords.get(node_id)
        if latlon is None:  # pragma: no cover - every path node came from the graph
            continue
        if prev_node is not None:
            shape, dropped_m = graph.shape_between(prev_node, node_id)
            shape_dropped_m = max(shape_dropped_m, dropped_m)
            for shape_lon, shape_lat in shape:
                push(shape_lon, shape_lat)
        lat, lon = latlon
        push(lon, lat)
        prev_node = node_id
    if not coords:
        return None
    if len(coords) == 1:
        # source == target (both endpoints snapped to the same node): a
        # one-position LineString isn't valid GeoJSON, so emit the
        # zero-length two-position line, which is.
        coords.append(list(coords[0]))

    simplified = simplify.simplify_geometry(
        {"type": "LineString", "coordinates": coords}, max_tokens=max_tokens
    )
    geometry = simplified["geometry"]
    line = geometry["coordinates"]
    line[0], line[-1] = coords[0], coords[-1]
    if budget.estimate_tokens({"path": geometry}) > max_tokens:
        return None
    # Two things move the emitted line off the source geometry: the token-fit
    # simplification here, and the GRAPH_SHAPE_EPSILON_M pruning build_graph
    # already did. Report the larger — reporting only the first is how a
    # perfectly-fitting curvy route came back claiming 0.0.
    return geometry, max(simplified["max_deviation_m"], shape_dropped_m)


def route(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    mode: str = "drive",
    include_path: bool = False,
) -> dict:
    """Shortest-path distance + duration between two points, by mode; geometry on request (#161).

    Extracts a bounded street graph around the midpoint of the two points
    (radius derived from their straight-line distance, same RADIUS_BUFFER
    padding as isochrone's own extraction; cached across calls via
    _get_or_build_graph (#39), shared with isochrone) and runs a target-terminated
    Dijkstra from the origin's snapped node to the destination's, tracking
    both cumulative time and cumulative true edge length (Graph.adjacency's
    length_m, distinct from a drive graph's baked-seconds weight — see
    Graph's docstring) so distance_m is exact even for drive mode.

    Raises UnsupportedMode for an unknown mode string, ValueError if any
    coordinate isn't a finite number, and RouteTooLong if the two points'
    straight-line distance exceeds ROUTE_MAX_STRAIGHT_LINE_M[mode] (real
    road distance is always >= straight-line, so this is a conservative
    reject before any graph extraction is attempted). ROUTE_MAX_STRAIGHT_LINE_M
    is derived per-mode from MODE_CONFIG[mode]["max_radius_m"] (the same cap
    isochrone's graph extraction enforces) via straight_line_max = 2 *
    (max_radius_m - SNAP_RADIUS_M) / RADIUS_BUFFER, so it's always the true,
    enforced limit rather than an independently-chosen number that could be
    looser than what extraction actually allows. Raises
    NoGraphNearby if no radius (including the widened retry —
    ROUTE_RADIUS_RETRY_FACTOR) ever produces a usable graph with both
    points snapped. A non-final radius never raises on its own — an empty
    graph or a failed snap at that radius just moves on to the next, larger
    radius rather than giving up early; only the outcome of the *last*
    radius tried decides between NoGraphNearby and returning {"error":
    "no_route"}. If both points snap at some radius but no path connects
    them, the extraction radius is widened once and retried; once both
    points have snapped successfully at any radius, a later radius's snap
    failure or empty graph no longer raises NoGraphNearby — it falls
    through to the structured {"error": "no_route"} result instead, since
    both points are known to be on real street graphs and simply
    disconnected, which is the accurate answer.

    On success returns {"distance_m", "duration_s", "mode", "from", "to"},
    plus "truncated"/"note" if the extraction graph hit MAX_GRAPH_SEGMENTS.

    include_path=True additionally returns "path", a GeoJSON LineString
    tracing the route from A's snapped node to B's — following each
    traversed segment's own shape vertices, not chords between
    intersections — and "path_max_deviation_m", how far the simplified line
    strays from that full-fidelity road geometry (0.0 when nothing was
    dropped, which for a curvy route means the whole shape fit). It is omitted entirely
    by default — compact-first: most callers want the distance and duration,
    and the polyline is by far the largest thing this tool can return. The
    line is fitted to whatever of the token budget the rest of the response
    leaves (see _path_linestring); if even a fully simplified line doesn't
    fit, the response carries "path_omitted": true and a "path_note" instead
    of a path that stops short of the destination.

    For walk/cycle (constant speed per mode), distance_m and duration_s
    always satisfy distance_m == duration_s * mode_speed_m_s exactly (both
    are derived from the same edges along the same min-time path); for
    drive, duration_s comes from baked per-edge time weights while
    distance_m is summed length_m independently, so no such identity holds.
    """
    graph, found = _shortest_path(from_lat, from_lon, to_lat, to_lon, mode)
    if found is None:
        return _no_route_result(from_lat, from_lon, to_lat, to_lon, mode)

    duration_s, distance_m, path = found
    result = {
        "distance_m": round(distance_m, 1),
        "duration_s": round(duration_s, 1),
        "mode": mode,
        "from": {"lat": from_lat, "lon": from_lon},
        "to": {"lat": to_lat, "lon": to_lon},
    }
    if graph.truncated:
        result["truncated"] = True
        result["note"] = (
            "the street graph hit its size cap; this route may be suboptimal or incomplete"
        )
    if include_path:
        # Whatever the rest of the response (including any truncation note)
        # leaves of the budget is what the polyline gets, so adding the path
        # can never push the answer over the budget the other tools respect.
        max_tokens = budget.token_budget()
        base_tokens = budget.estimate_tokens(result)
        path_tokens = max_tokens - base_tokens - PATH_ENVELOPE_TOKENS
        line = (
            _path_linestring(graph, path, path_tokens) if path_tokens >= PATH_MIN_TOKENS else None
        )
        if line is None:
            # The "why there's no path" explanation is itself ~35 tokens, so
            # it has to be priced inside the fit decision rather than stapled
            # on after it — a budget too small to hold the path is often too
            # small to hold a paragraph about the path either (#161 sweep:
            # budget=60 produced a 78-token response). Fall back to the bare
            # flag, then to saying nothing, so the answer never overruns.
            explained = {**result, "path_omitted": True, "path_note": PATH_OMITTED_NOTE}
            flagged = {**result, "path_omitted": True}
            if budget.estimate_tokens(explained) <= max_tokens:
                result = explained
            elif budget.estimate_tokens(flagged) <= max_tokens:
                result = flagged
        else:
            geometry, deviation_m = line
            result["path"] = geometry
            result["path_max_deviation_m"] = round(deviation_m, 2)
    return result


def _no_route_result(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float, mode: str
) -> dict:
    """The structured "both points snapped, nothing connects them" answer,
    shared by route() and places_along_route()."""
    return {
        "error": "no_route",
        "detail": "no path found between the points in the searched area",
        "mode": mode,
        "from": {"lat": from_lat, "lon": from_lon},
        "to": {"lat": to_lat, "lon": to_lon},
    }


def _shortest_path(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    mode: str,
) -> tuple[Graph, tuple[float, float, list[tuple[str, float]]] | None]:
    """(graph, (duration_s, distance_m, path)) for the A->B min-time path.

    The shared machinery behind route() and places_along_route(): input
    validation, the straight-line/radius caps, the widen-and-retry
    extraction loop, endpoint snapping, and the path search itself. The
    second element is None when both endpoints snapped into a graph but no
    path connects them — callers turn that into their own structured
    no_route answer (_no_route_result). Every error case (UnsupportedMode,
    ValueError, RouteTooLong, NoGraphNearby) raises exactly as route()'s
    docstring describes.
    """
    if mode not in MODE_CONFIG:
        raise UnsupportedMode(mode)
    for value in (from_lat, from_lon, to_lat, to_lon):
        if not _is_finite_number(value):
            raise ValueError("from_lat/from_lon/to_lat/to_lon must be finite numbers")

    config = MODE_CONFIG[mode]
    max_radius_m = config["max_radius_m"]
    const_speed = config["default_speed_m_s"]  # None => drive's per-edge/baked-time model

    cap_m = ROUTE_MAX_STRAIGHT_LINE_M[mode]
    straight_line_m = _haversine_m(from_lat, from_lon, to_lat, to_lon)
    if straight_line_m > cap_m:
        raise RouteTooLong(straight_line_m, cap_m)

    center_lat, center_lon = _midpoint(from_lat, from_lon, to_lat, to_lon)
    base_radius_m = max(
        straight_line_m / 2.0 * RADIUS_BUFFER + SNAP_RADIUS_M, ROUTE_MIN_RADIUS_M
    )
    if base_radius_m > max_radius_m:
        # ROUTE_MAX_STRAIGHT_LINE_M is derived directly from max_radius_m
        # (see its definition above), so this should essentially never fire
        # — it's a redundant safety net against floating-point edge cases
        # only. Reports the same (already-correct) cap_m so the number in
        # the error always matches the true, enforced limit.
        raise RouteTooLong(straight_line_m, cap_m)

    radii_m = [base_radius_m]
    retry_radius_m = min(base_radius_m * ROUTE_RADIUS_RETRY_FACTOR, max_radius_m)
    if retry_radius_m > base_radius_m:
        radii_m.append(retry_radius_m)

    # A non-final radius never raises here — it only records why this
    # attempt didn't work and continues to the next (larger) radius. Only
    # once every radius has been tried do we decide the failure kind:
    # NoGraphNearby if the points (or the area) never produced a usable
    # graph with both endpoints snapped, or a structured no_route result if
    # they did snap at some radius but no path connected them (snapped_both
    # — Bug 4: a *later*, larger-radius snap failure must not override an
    # earlier successful snapped_both and raise NoGraphNearby instead of
    # falling through to no_route).
    found = None
    graph = None
    snapped_both = False
    for i, radius_m in enumerate(radii_m):
        is_last = i == len(radii_m) - 1
        # Via the shared graph cache (#39), not a bare build_graph: repeat
        # routes over the same area — and route()/isochrone() over the same
        # area — reuse one extraction instead of paying a fresh multi-second
        # upstream scan each call.
        graph = _get_or_build_graph(center_lat, center_lon, radius_m, mode, speed_m_s=None)
        if graph.node_count() == 0:
            if is_last and not snapped_both:
                raise NoGraphNearby(center_lat, center_lon, radius_m)
            continue

        source = snap_to_graph(graph, from_lat, from_lon)
        if source is None:
            if is_last and not snapped_both:
                raise NoGraphNearby(from_lat, from_lon, radius_m)
            continue
        target = snap_to_graph(graph, to_lat, to_lon)
        if target is None:
            if is_last and not snapped_both:
                raise NoGraphNearby(to_lat, to_lon, radius_m)
            continue

        snapped_both = True
        speed = 1.0 if graph.weight_is_time else const_speed
        found = _dijkstra_path_to_target(graph, source, target, speed)
        if found is not None:
            break

    # found is None here only with snapped_both True: the loop above raises
    # NoGraphNearby before falling through on the last radius whenever
    # snapped_both is still False, so reaching that point with found still
    # None means both endpoints snapped at some radius (Bug 4: even if a
    # *later*, larger retry radius then failed to snap, that later failure
    # just `continue`s rather than raising and clobbering this — the
    # accurate answer is no_route, not NoGraphNearby).
    return graph, found


def _sample_evenly(items: list, count: int) -> list:
    """At most `count` items spread evenly across `items`, keeping order and both ends.

    Same idea as decimate() but index-based over a plain sequence. When
    `items` is longer than `count` the step is > 1, so the rounded indices
    strictly increase and exactly `count` items come back — no dedup gap.
    """
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[0]]
    step = (len(items) - 1) / (count - 1)
    return [items[round(i * step)] for i in range(count)]


def _subsample_path(
    path: list[tuple[str, float]], max_nodes: int
) -> list[tuple[str, float]]:
    """Evenly thin a node path to at most max_nodes entries, keeping both ends.

    The last node is pinned (via _sample_evenly) since the corridor's
    along_m readings should still reach the route's full length after
    thinning. max_nodes < 2 is meaningless for a path — a corridor needs at
    least the two endpoints — so it leaves the path alone.
    """
    if max_nodes < 2:
        return path
    return _sample_evenly(path, max_nodes)


def _corridor_bbox(
    coords: list[tuple[float, float]], buffer_m: float
) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) around (lat, lon) coords, padded by buffer_m.

    A cheap superset of the corridor — the exact "within buffer_m of the
    path" test happens per-place in Python — so the longitude padding uses
    the box's highest-|latitude| edge (where a metre is worth the most
    degrees) to be sure it never under-covers.

    Longitude may come back outside [-180, 180], exactly like geo.bbox_around's
    output, and overture.find_places_in_bbox folds such a box into two
    in-range ones. A path whose raw longitudes span more than half the globe
    has crossed the antimeridian rather than genuinely circled it (routes
    are capped at ROUTE_MAX_STRAIGHT_LINE_M, a few hundred km), so its
    western lons are unwrapped past +180 to give a tight wrapped box instead
    of a global latitude band.
    """
    lats = [lat for lat, _lon in coords]
    lons = [lon for _lat, lon in coords]
    if max(lons) - min(lons) > 180.0:
        lons = [lon + 360.0 if lon < 0.0 else lon for lon in lons]
    widest_lat = max(abs(min(lats)), abs(max(lats)))
    dlat = buffer_m / 111_320.0
    dlon = buffer_m / (111_320.0 * max(math.cos(math.radians(widest_lat)), 1e-6))
    return (
        min(lons) - dlon,
        max(min(lats) - dlat, -90.0),
        max(lons) + dlon,
        min(max(lats) + dlat, 90.0),
    )


def _point_to_segment_m(
    plat: float, plon: float, alat: float, alon: float, blat: float, blon: float
) -> tuple[float, float]:
    """(distance_m, t) from point P to segment A->B.

    t is P's projection along A->B, clamped to [0, 1].

    Works in a local equirectangular projection centred on A: longitude
    differences are scaled by cos(A's latitude) and both axes by metres per
    degree, which turns the segment test into plain 2D geometry. At corridor
    scale — graph edges are tens to a few hundred metres and max_detour_m
    caps the offset at 5km — that projection's distortion is well under a
    metre, far below the precision this filter needs. Longitude differences
    are wrapped into (-180, 180] first, so a segment or a place straddling
    the antimeridian still measures the short way round.
    """
    coslat = math.cos(math.radians(alat))
    m_per_deg = EARTH_RADIUS_M * math.pi / 180.0

    def xy(lat: float, lon: float) -> tuple[float, float]:
        dlon = ((lon - alon + 180.0) % 360.0) - 180.0
        return dlon * coslat * m_per_deg, (lat - alat) * m_per_deg

    px, py = xy(plat, plon)
    bx, by = xy(blat, blon)
    seg_sq = bx * bx + by * by
    if seg_sq <= 0.0:  # degenerate segment (duplicate coords): point distance
        return math.hypot(px, py), 0.0
    t = min(1.0, max(0.0, (px * bx + py * by) / seg_sq))
    return math.hypot(px - t * bx, py - t * by), t


def _nearest_on_path(
    plat: float, plon: float, path_points: list[tuple[float, float, float]]
) -> tuple[float, float]:
    """(distance_m, along_m) of the closest point on the path polyline to (plat, plon).

    Measures against the *segments* between consecutive path nodes, not the
    nodes alone: a place beside the middle of a long block is as much "on
    the way" as one beside a junction, and node-only distance would have
    reported it as up to half a block farther off than it is. along_m is
    interpolated between the two nodes' route distances at the same
    fraction t the closest point sits at.
    """
    if len(path_points) == 1:
        lat, lon, along_m = path_points[0]
        return _haversine_m(plat, plon, lat, lon), along_m
    best_m, best_along_m = math.inf, 0.0
    for i in range(len(path_points) - 1):
        alat, alon, a_along = path_points[i]
        blat, blon, b_along = path_points[i + 1]
        dist_m, t = _point_to_segment_m(plat, plon, alat, alon, blat, blon)
        if dist_m < best_m:
            best_m = dist_m
            best_along_m = a_along + t * (b_along - a_along)
    return best_m, best_along_m


def _path_chunks(
    path_points: list[tuple[float, float, float]], chunks: int
) -> list[list[tuple[float, float, float]]]:
    """Cut the path into <= `chunks` consecutive runs that overlap by one node.

    The overlap matters: a chunk boundary that merely abutted would leave
    the segment spanning it inside neither chunk's padded box, so a place
    beside that segment could be missed. Sharing the boundary node puts
    every segment wholly inside exactly one chunk.
    """
    if len(path_points) < 2:
        return [path_points]
    segments = len(path_points) - 1
    count = max(1, min(chunks, segments))
    return [
        path_points[i * segments // count : (i + 1) * segments // count + 1]
        for i in range(count)
    ]


def _corridor_candidates(
    path_points: list[tuple[float, float, float]],
    max_detour_m: float,
    category: str | None,
    name: str | None,
) -> tuple[list[dict], bool]:
    """(candidate places, capped) near the path, gathered chunk by chunk.

    One query per _path_chunks chunk over that chunk's own padded box, each
    with its own slice of the overture.BBOX_MAX_CANDIDATES budget, ranked by
    distance from the chunk's middle node — see CORRIDOR_BBOX_CHUNKS for why
    a single whole-route box is not enough. Rows are unioned and deduped by
    id (chunks overlap, and a place near a boundary legitimately falls in
    two boxes); `capped` is True if any chunk filled its own limit, meaning
    that stretch of the corridor held more candidates than were measured.
    """
    chunks = _path_chunks(path_points, CORRIDOR_BBOX_CHUNKS)
    per_chunk_limit = max(1, overture.BBOX_MAX_CANDIDATES // len(chunks))
    by_key: dict = {}
    capped = False
    for chunk in chunks:
        coords = [(lat, lon) for lat, lon, _along in chunk]
        bbox = _corridor_bbox(coords, max_detour_m)
        # Proximity ranking compares raw longitudes, so it is only
        # meaningful for a box that hasn't been unwrapped past the seam.
        mid_lat, mid_lon = coords[len(coords) // 2]
        near = (mid_lat, mid_lon) if -180.0 <= bbox[0] and bbox[2] <= 180.0 else None
        rows, chunk_capped = overture.find_places_in_bbox(
            bbox, category, name, limit=per_chunk_limit, near=near
        )
        capped = capped or chunk_capped
        for row in rows:
            key = row["id"] or ("", row["name"], row["lat"], row["lon"])
            by_key.setdefault(key, row)
    return list(by_key.values()), capped


def places_along_route(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    mode: str = "drive",
    category: str | None = None,
    name: str | None = None,
    max_detour_m: float = CORRIDOR_DEFAULT_DETOUR_M,
    limit: int = 10,
) -> dict:
    """Places within a corridor around the A->B route — "on the way" search (#171).

    Composes route()'s machinery with a find_places-style query: the same
    shortest path route() computes (see _shortest_path — identical caps,
    extraction retries, snapping and error taxonomy), but keeping the node
    path, then every candidate place near it (gathered chunk by chunk, see
    _corridor_candidates) is measured against the path.

    A place is "on the way" when the path polyline passes within
    max_detour_m of it — the distance is to the nearest point on the nearest
    *segment* (_nearest_on_path), not to the nearest node, so a place
    halfway along a long block counts exactly as much as one at a junction.
    Its reported detour_m is that distance doubled — an approximation of the
    round trip off and back onto the route, deliberately a *straight-line*
    one: measuring the true routed detour would mean a fresh Dijkstra per
    candidate. along_m is the route distance from the origin at that closest
    point, so an agent can say "about a third of the way there"; results are
    ordered by along_m (route order, not detour cost) so the list reads as
    an itinerary.

    More matches than `limit` are thinned to an even sample along the whole
    route rather than truncated to the first `limit` — a prefix would silently
    drop the far end of the journey — and the result is flagged
    "truncated": true so the caller knows to narrow or raise `limit`.

    max_detour_m must be a positive number no larger than
    CORRIDOR_MAX_DETOUR_M (5km, i.e. a ~10km round-trip detour); anything
    else raises ValueError, as does a non-finite coordinate. Raises
    UnsupportedMode, RouteTooLong and NoGraphNearby exactly as route() does,
    and returns route()'s {"error": "no_route", ...} when both endpoints
    snap but nothing connects them.

    Returns {"results": [find_places row + detour_m + along_m, ...],
    "route": {"distance_m", "duration_s", "mode"}}, plus "truncated": true
    with a "note" when the street graph hit MAX_GRAPH_SEGMENTS (the route
    itself may be suboptimal), a chunk of the corridor held more candidate
    places than its share of overture.BBOX_MAX_CANDIDATES (some "on the way"
    places were never measured — narrow with category/name or a smaller
    max_detour_m), or more than `limit` places were on the way.
    """
    if not _is_finite_number(max_detour_m) or max_detour_m <= 0:
        raise ValueError("max_detour_m must be a positive number")
    if max_detour_m > CORRIDOR_MAX_DETOUR_M:
        raise ValueError(
            f"max_detour_m={max_detour_m:.0f} exceeds the "
            f"{CORRIDOR_MAX_DETOUR_M:.0f}m cap"
        )
    limit = max(0, min(int(limit), overture.MAX_ROWS))

    graph, found = _shortest_path(from_lat, from_lon, to_lat, to_lon, mode)
    if found is None:
        return _no_route_result(from_lat, from_lon, to_lat, to_lon, mode)
    duration_s, distance_m, path = found

    sampled = _subsample_path(path, CORRIDOR_MAX_PATH_NODES)
    path_points = [(*graph.coords[node_id], along_m) for node_id, along_m in sampled]

    candidates, capped = _corridor_candidates(path_points, max_detour_m, category, name)

    rows = []
    for place in candidates:
        nearest_m, nearest_along_m = _nearest_on_path(
            place["lat"], place["lon"], path_points
        )
        if nearest_m > max_detour_m:
            continue
        rows.append({**place, "detour_m": round(2 * nearest_m, 1),
                     "along_m": round(nearest_along_m, 1)})

    # Route order first (the itinerary reading), then the cheaper detour and
    # finally the id, so equal-position ties are still deterministic.
    rows.sort(key=lambda r: (r["along_m"], r["detour_m"], r["id"] or ""))

    result = {
        "results": _sample_evenly(rows, limit),
        "route": {
            "distance_m": round(distance_m, 1),
            "duration_s": round(duration_s, 1),
            "mode": mode,
        },
    }
    notes = []
    if graph.truncated:
        notes.append(
            "the street graph hit its size cap; this route may be suboptimal or incomplete"
        )
    if capped:
        notes.append(
            "part of the route's corridor holds more places than the candidate "
            "budget allows; some on-the-way places were not considered — narrow "
            "with category/name or a smaller max_detour_m"
        )
    if len(rows) > limit:
        notes.append(
            f"{len(rows)} places are on the way but limit is {limit}; the results "
            "are an even sample spanning the whole route, not the first "
            f"{limit} — raise limit or narrow with category/name to see more"
        )
    if notes:
        result["truncated"] = True
        result["note"] = "; ".join(notes)
    return result
