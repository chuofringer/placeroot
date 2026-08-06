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

Query layer (issue #40): this module used to keep its own DuckDB
connection/lock/schema-probe/bbox-helpers/geometry-probe, entirely
separate from overture.py's equivalents — the split predated issue #31's
connection-factory wiring and meant this module missed that fix outright.
It now shares overture.py's connection, bbox helpers, and probe_schema
(all via db.py/geo.py); db.ensure_spatial() is called explicitly here
since divisions.py/buildings.py are no longer the only spatial callers.
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
MAX_RADIUS_M = WALK_MAX_RADIUS_M  # back-compat alias; walk was the only mode pre-#38
RADIUS_BUFFER = 1.25  # street paths aren't straight lines; pad the extraction radius
MAX_POLYGON_POINTS = 100  # decimation cap before the token-budget pass (convex hull path)

SNAP_RADIUS_M = 300.0  # how far the origin may snap to reach a usable graph node
MIN_USABLE_COMPONENT_NODES = 5  # components smaller than this are treated as fragments

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

CONCAVE_MIN_NODES = 8  # fewer reached nodes than this: convex hull is used directly
CONCAVE_CELL_MIN_M = 60.0
CONCAVE_CELL_DIVISOR = 40.0  # cell size ~= max(CONCAVE_CELL_MIN_M, reached_radius_m / this)

GRAPH_CACHE_MAXSIZE = 8
GRAPH_CACHE_MARGIN = 1.3  # over-fetch factor so nearby repeat queries hit the cache

REQUIRED_COLUMNS = ["id", "geometry", "bbox", "class", "connectors"]
ESSENTIAL_COLUMNS = {"geometry", "bbox"}

EARTH_RADIUS_M = 6371000.0

# Deprecated: import db/geo directly instead. Thin aliases (issue #40 —
# see the module docstring) so any external reference — test_routing.py's
# own bbox tests included — keeps working unchanged.
_conn_lock = db.conn_lock
_conn = db.shared_conn
_probe_schema = db.probe_schema
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


def set_data_path(path: str | None) -> None:
    """Point the query layer at a local dataset instead of live S3. Tests only.

    Delegates to overture.set_data_path's per-theme+type override (issue
    #40) — the same mechanism buildings.py uses for its own type override.
    """
    overture.set_data_path(path, theme=THEME, type_="segment")


def _upstream_glob() -> str:
    # type_="segment" matches this module's own upstream path
    # (theme=transportation/type=segment); overture._upstream_glob also
    # honors PLACEROOT_TRANSPORTATION_DATA_PATH as a back-compat fallback
    # for this theme specifically, so this reads identically to before #40.
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
    """Weighted graph: node_id -> [(neighbor_id, weight), ...].

    Undirected by default; one-way segments (#38) add a directed edge via
    add_edge(..., directed=True), which only links a -> b. weight_is_time
    is False (the common case: constant-speed modes) when weight is plain
    length_m, meant to be divided by a mode speed_m_s at dijkstra time; it's
    True for a drive-mode graph whose per-edge weight was already baked to
    seconds at build time (variable speed per class/speed-limit) — such a
    graph must be queried with dijkstra(..., speed_m_s=1.0).
    """

    def __init__(self):
        self.adjacency: dict[str, list[tuple[str, float]]] = {}
        self.coords: dict[str, tuple[float, float]] = {}  # node_id -> (lat, lon)
        self.weight_is_time: bool = False
        # Both directions of every edge, regardless of `directed` — used only
        # for connected_components()/snapping, which should treat a fragment
        # linked exclusively by one-way edges as connected either way (a
        # street being one-way for cars doesn't disconnect it as a
        # geometric/topological fragment).
        self._undirected_neighbors: dict[str, set[str]] = {}

    def add_node(self, node_id: str, lat: float, lon: float) -> None:
        self.adjacency.setdefault(node_id, [])
        self._undirected_neighbors.setdefault(node_id, set())
        self.coords.setdefault(node_id, (lat, lon))

    def add_edge(self, a: str, b: str, weight: float, directed: bool = False) -> None:
        if a == b:
            return
        self.adjacency[a].append((b, weight))
        self._undirected_neighbors[a].add(b)
        self._undirected_neighbors[b].add(a)
        if not directed:
            self.adjacency[b].append((a, weight))

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
            for b, _ in neighbors:
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

    graph = Graph()
    graph.weight_is_time = bake_time
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
            if forward_allowed and backward_allowed:
                graph.add_edge(id_a, id_b, weight, directed=False)
            elif forward_allowed:
                graph.add_edge(id_a, id_b, weight, directed=True)
            else:
                graph.add_edge(id_b, id_a, weight, directed=True)

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
        for neighbor, length_m in graph.adjacency[node]:
            nd = d + length_m / speed_m_s
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
    mode string.

    The polygon is a concave grid-boundary trace of reached nodes (#36),
    falling back to a convex hull when there are too few reached nodes to
    bucket meaningfully; either way the *stats* are exact, only the drawn
    shape approximates. The built graph is cached across calls (#39) —
    see _get_or_build_graph.
    """
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

    result = {
        "center": {"lat": lat, "lon": lon},
        "minutes": minutes,
        "mode": mode,
        "speed_m_s": const_speed,
        "polygon": _ring_to_geojson(ring),
        "polygon_method": polygon_method,
        "stats": {
            "reachable_nodes": len(reached),
            "max_radius_m": round(max_radius_reached_m, 1),
            "area_km2": round(area_km2, 4),
        },
    }
    if truncated:
        result["truncated"] = True
    return result
