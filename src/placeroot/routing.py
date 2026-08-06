"""Walking isochrones from Overture's transportation theme.

Extracts a bounded street graph (segments -> connector nodes + edges)
around a point, runs Dijkstra out to a time budget, and returns the
reachable-node set as a polygon plus stats. MVP scope is walking only —
see isochrone()'s mode handling for driving/cycling.

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

Polygon method: convex hull of reached nodes. This is the "honest fallback"
named in the design brief, not a concave alpha-shape — it's simple,
always valid, and cheap, at the cost of visually overstating reachable
area around concave boundaries (e.g. a river with one bridge will still
show a convex isochrone that covers the water). The *stats* (reachable
node count, per-node distances) are exact; only the drawn polygon shape
is an overestimate. Tighter hulls are a follow-up.
"""

import heapq
import logging
import math
import os
from functools import lru_cache

import duckdb

from placeroot import budget, cache, overture, release

logger = logging.getLogger(__name__)

THEME = "transportation"

DEFAULT_SPEED_M_S = 1.4  # ~5 km/h walking pace
MAX_RADIUS_M = 5000.0  # walking isochrones never need to look further than this
RADIUS_BUFFER = 1.25  # street paths aren't straight lines; pad the extraction radius
MAX_POLYGON_POINTS = 100  # decimation cap before the token-budget pass

SNAP_RADIUS_M = 300.0  # how far the origin may snap to reach a usable graph node
MIN_USABLE_COMPONENT_NODES = 5  # components smaller than this are treated as fragments

# Overture road classes a pedestrian cannot use. Everything else (footway,
# path, residential, service, tertiary, living_street, cycleway, steps,
# unclassified, unknown, ...) is treated as walkable.
EXCLUDED_WALK_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link"}

REQUIRED_COLUMNS = ["id", "geometry", "bbox", "class", "connectors"]
ESSENTIAL_COLUMNS = {"geometry", "bbox"}

EARTH_RADIUS_M = 6371000.0

_data_path_override: str | None = None


class UpstreamUnavailable(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SchemaDegraded(Exception):
    def __init__(self, missing: list[str]):
        detail = f"required columns missing from transportation dataset: {', '.join(missing)}"
        super().__init__(detail)
        self.detail = detail
        self.missing = missing


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
    """Point the query layer at a local dataset instead of live S3. Tests only."""
    global _data_path_override
    _data_path_override = path


def _upstream_glob() -> str:
    if _data_path_override is not None:
        return _data_path_override
    env_path = os.environ.get("PLACEROOT_TRANSPORTATION_DATA_PATH")
    if env_path:
        return env_path
    active_release = release.resolve_release()
    return f"s3://overturemaps-us-west-2/release/{active_release}/theme={THEME}/type=segment/*"


@lru_cache(maxsize=1)
def _conn() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    con.execute("SET s3_access_key_id='';")
    con.execute("SET s3_secret_access_key='';")
    try:
        con.execute("SET http_timeout=5000;")
        con.execute("SET http_retries=2;")
        con.execute("SET http_retry_wait_ms=200;")
        con.execute("SET http_retry_backoff=2;")
    except duckdb.Error as e:
        logger.warning("Could not set httpfs timeout/retry options: %s", e)
    return con


@lru_cache(maxsize=8)
def _probe_schema(glob: str) -> frozenset | None:
    try:
        cols = _conn().execute(f"SELECT * FROM read_parquet('{glob}') LIMIT 0").description
        return frozenset(c[0] for c in cols)
    except duckdb.Error as e:
        logger.warning("Schema probe failed for %s: %s", glob, e)
        return None


@lru_cache(maxsize=8)
def _geometry_wkt_expr(glob: str) -> str:
    """SQL expression yielding WKT for the geometry column.

    Real Overture parquet types this column as a native GEOMETRY (via
    GeoParquet metadata) — ST_AsText works on it directly. Our test fixture
    (and possibly other sources) stores it as a plain WKB BLOB instead,
    which needs ST_GeomFromWKB first. Detected once per glob and cached.
    """
    try:
        cols = _conn().execute(f"SELECT * FROM read_parquet('{glob}') LIMIT 0").description
        types = {c[0]: str(c[1]) for c in cols}
    except duckdb.Error as e:
        logger.warning("Geometry type probe failed for %s: %s", glob, e)
        types = {}
    geom_type = types.get("geometry", "")
    if geom_type.upper().startswith("GEOMETRY"):
        return "ST_AsText(geometry)"
    return "ST_AsText(ST_GeomFromWKB(geometry))"


def missing_columns(glob: str) -> list[str]:
    present = _probe_schema(glob)
    if present is None:
        return []
    return [c for c in REQUIRED_COLUMNS if c not in present]


def _check_schema(glob: str) -> list[str]:
    missing = missing_columns(glob)
    essential_missing = [c for c in missing if c in ESSENTIAL_COLUMNS]
    if essential_missing:
        raise SchemaDegraded(essential_missing)
    return missing


def _bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Square bbox guaranteed to contain the radius_m circle around (lat, lon).

    Same approach as overture._bbox_around, duplicated here rather than
    imported so routing.py has no dependency on overture.py's internals —
    see the follow-up list for unifying shared geo helpers (#40).

    Latitude is clamped to [-90, 90] (issue #42), same as overture.py's
    copy. Longitude is left unwrapped; see _bbox_filter_sql below for how
    build_graph() turns a crossing box into a seam-safe SQL filter.
    """
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    ymin = max(lat - dlat, -90.0)
    ymax = min(lat + dlat, 90.0)
    return lon - dlon, ymin, lon + dlon, ymax


def _bbox_filter_sql(xmin: float, ymin: float, xmax: float, ymax: float) -> tuple[str, dict]:
    """SQL bbox filter for [xmin, xmax] x [ymin, ymax], antimeridian-safe.

    Minimal duplicate of overture._bbox_filter_sql (issue #42) — kept
    separate for the same reason _bbox_around is duplicated rather than
    imported (see above); unifying both is #40's job, not this fix's.
    """
    if xmin >= -180.0 and xmax <= 180.0:
        filter_sql = (
            "bbox.xmax >= $xmin AND bbox.xmin <= $xmax"
            " AND bbox.ymax >= $ymin AND bbox.ymin <= $ymax"
        )
        return filter_sql, {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}

    east_xmin = xmin + 360.0 if xmin < -180.0 else xmin
    west_xmax = xmax - 360.0 if xmax > 180.0 else xmax
    filter_sql = (
        "(("
        "bbox.xmax >= $xmin AND bbox.xmin <= 180"
        ") OR ("
        "bbox.xmax >= -180 AND bbox.xmin <= $xmax"
        ")) AND bbox.ymax >= $ymin AND bbox.ymin <= $ymax"
    )
    return filter_sql, {"xmin": east_xmin, "ymin": ymin, "xmax": west_xmax, "ymax": ymax}


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
            # overture._new_connection as the background-fetch factory: the
            # tile COPY only needs httpfs, and reusing it avoids a third
            # connection-configuration site in this module.
            paths = cache.local_paths_for_query(
                _conn(), release.resolve_release(), THEME, bbox, upstream,
                overture._new_connection,
            )
        except duckdb.Error as e:
            raise UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


class Graph:
    """Undirected weighted graph: node_id -> [(neighbor_id, length_m), ...]."""

    def __init__(self):
        self.adjacency: dict[str, list[tuple[str, float]]] = {}
        self.coords: dict[str, tuple[float, float]] = {}  # node_id -> (lat, lon)

    def add_node(self, node_id: str, lat: float, lon: float) -> None:
        self.adjacency.setdefault(node_id, [])
        self.coords.setdefault(node_id, (lat, lon))

    def add_edge(self, a: str, b: str, length_m: float) -> None:
        if a == b:
            return
        self.adjacency[a].append((b, length_m))
        self.adjacency[b].append((a, length_m))

    def node_count(self) -> int:
        return len(self.adjacency)

    def edge_count(self) -> int:
        """Undirected edge count (each stored twice internally)."""
        return sum(len(v) for v in self.adjacency.values()) // 2

    def nearest_node(self, lat: float, lon: float) -> str | None:
        best_id, best_d = None, math.inf
        for node_id, (nlat, nlon) in self.coords.items():
            d = _haversine_m(lat, lon, nlat, nlon)
            if d < best_d:
                best_id, best_d = node_id, d
        return best_id

    def connected_components(self) -> list[set[str]]:
        """Node ids grouped by connectivity (undirected BFS)."""
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
                for neighbor, _ in self.adjacency[node]:
                    if neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)
            components.append(component)
        return components


def build_graph(lat: float, lon: float, radius_m: float) -> Graph:
    """Walkable street graph within radius_m of (lat, lon).

    Raises RadiusTooLarge if radius_m exceeds MAX_RADIUS_M, SchemaDegraded
    if the transportation dataset is missing geometry/bbox, or
    UpstreamUnavailable if the remote scan fails.
    """
    if radius_m > MAX_RADIUS_M:
        raise RadiusTooLarge(radius_m, MAX_RADIUS_M)

    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    xmin, ymin, xmax, ymax = _bbox_around(lat, lon, radius_m)
    bbox = (xmin, ymin, xmax, ymax)
    bbox_filter, bbox_params = _bbox_filter_sql(xmin, ymin, xmax, ymax)

    class_expr = "NULL" if "class" in missing else "class"
    connectors_expr = "NULL" if "connectors" in missing else "connectors"
    wkt_expr = _geometry_wkt_expr(upstream)
    present = _probe_schema(upstream)
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
            {wkt_expr} AS wkt
        FROM {_from_source(bbox)}
        WHERE {bbox_filter}
          {subtype_filter}
    """
    params = bbox_params
    try:
        rows = _conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e

    graph = Graph()
    for _id, cls, connectors, wkt in rows:
        if cls is not None and cls in EXCLUDED_WALK_CLASSES:
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

        for (at_a, id_a), (at_b, id_b) in zip(stops, stops[1:]):
            edge_length_m = (at_b - at_a) * total_length_m
            graph.add_edge(id_a, id_b, edge_length_m)

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


def _hull_to_geojson(hull_lonlat: list[tuple[float, float]]) -> dict:
    if not hull_lonlat:
        return {"type": "Polygon", "coordinates": []}
    ring = [[lon, lat] for lon, lat in hull_lonlat]
    ring.append(ring[0])  # GeoJSON polygons are closed rings
    return {"type": "Polygon", "coordinates": [ring]}


def isochrone(
    lat: float,
    lon: float,
    minutes: float = 15,
    speed_m_s: float = DEFAULT_SPEED_M_S,
    radius_m: float | None = None,
) -> dict:
    """Walking isochrone from (lat, lon).

    radius_m overrides the auto-derived graph extraction radius; if given
    and it exceeds MAX_RADIUS_M, raises RadiusTooLarge. Otherwise the
    extraction radius is derived from minutes/speed_m_s and silently capped
    at MAX_RADIUS_M — long isochrones may then undercount reachable nodes
    beyond the cap (documented follow-up: chained/paged graph extraction).
    """
    max_seconds = minutes * 60
    auto_radius = min(max_seconds * speed_m_s * RADIUS_BUFFER, MAX_RADIUS_M)
    extraction_radius = radius_m if radius_m is not None else auto_radius

    graph = build_graph(lat, lon, extraction_radius)
    if graph.node_count() == 0:
        raise NoGraphNearby(lat, lon, extraction_radius)

    source = snap_to_graph(graph, lat, lon)
    if source is None:
        raise NoGraphNearby(lat, lon, extraction_radius)
    reached = dijkstra(graph, source, max_seconds, speed_m_s)

    reached_coords = [graph.coords[n] for n in reached]  # (lat, lon)
    hull_lonlat = convex_hull([(lon_, lat_) for lat_, lon_ in reached_coords])
    hull_lonlat = decimate(hull_lonlat, MAX_POLYGON_POINTS)

    # Budget-aware decimation: shrink further if the GeoJSON alone blows the
    # token budget (rare, but a very fine grid could produce a big hull).
    budget_tokens = budget.token_budget()
    truncated = False
    cap = len(hull_lonlat)
    while cap > 4 and budget.estimate_tokens(_hull_to_geojson(hull_lonlat)) > budget_tokens:
        cap = max(4, cap // 2)
        hull_lonlat = decimate(hull_lonlat, cap)
        truncated = True

    max_radius_m = max(
        (_haversine_m(lat, lon, node_lat, node_lon) for node_lat, node_lon in reached_coords),
        default=0.0,
    )
    area_km2 = _polygon_area_m2(hull_lonlat, lat, lon) / 1_000_000.0

    result = {
        "center": {"lat": lat, "lon": lon},
        "minutes": minutes,
        "mode": "walk",
        "speed_m_s": speed_m_s,
        "polygon": _hull_to_geojson(hull_lonlat),
        "polygon_method": "convex_hull",
        "stats": {
            "reachable_nodes": len(reached),
            "max_radius_m": round(max_radius_m, 1),
            "area_km2": round(area_km2, 4),
        },
    }
    if truncated:
        result["truncated"] = True
    return result
