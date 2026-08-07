"""Infrastructure near a point, from Overture's base theme (issue #179).

The second PlaceRoot tool over theme=base, and land_use.py's sibling:
type=infrastructure carries the built things that aren't buildings and
aren't POIs — bridges, airport runways and terminals, aerialways,
communication towers, power lines and pylons, piers, dams. Structurally
this is land_use.py (schema probe/degrade, per-type cache key,
set_data_path for tests) pointed at a third base-theme type.

Radius search, not point-in-polygon — the one deliberate departure from
land_use.py. Most infrastructure is linear (a power line, a bridge deck)
or a bare point (a tower); asking "what polygon contains this point"
would answer null for nearly every row that matters. So the query shape
here is buildings_at's / find_places' point mode: bbox prefilter around a
radius, exact distance predicate, nearest-first ORDER BY, capped LIMIT.

Distance for mixed geometry types (the part worth stating precisely):
rows are points, linestrings *and* polygons, so a centroid stand-in —
what buildings.py uses, where every row is a compact footprint — would be
badly wrong here. The centroid of a 30 km power line says nothing about
how far away the line is from you; you can be standing under it. This
module instead asks DuckDB for the closest point *on* each geometry to
the query point (ST_ClosestPoint) and haversines that, so a bridge you
are standing on measures ~0 m rather than "distance to the middle of the
bridge". For a point row this collapses to the point itself, so all three
geometry kinds go through one expression.

The one approximation left: ST_ClosestPoint picks its nearest vertex/edge
position in lon/lat degree space, where a degree of longitude is shorter
than a degree of latitude away from the equator. That can select a point
slightly off the true geodesic-nearest one, but the resulting error is
second order (it is nearly zero for the many rows whose nearest point is
a vertex, and the along-edge misplacement it can cause at a few-hundred-
metre radius is well under a metre). The reported distance itself is a
proper haversine, identical to overture.DISTANCE_EXPR, not a planar
approximation.

Cache theme key: "base_infrastructure", following land_use.py's composite
"base_<type>" convention — cache.py uses the theme string verbatim as a
directory component, so it must stay distinct per base-theme type and
must avoid ':' (illegal in a Windows path component).

Design rule: answers, not data — compact rows ({subtype, class, name,
distance_m}), no geometry, no source_tags. Empty results are a
first-class answer: base-theme coverage is OSM-derived and patchy, and
"no infrastructure within 500 m" is a real, useful finding, not an error.
"""

import logging

import duckdb

from placeroot import cache, db, geo, overture, release

logger = logging.getLogger(__name__)

THEME = "base"
TYPE_ = "infrastructure"

# geometry/bbox are essential (no distance can be measured without them);
# everything else degrades to None in its field rather than failing the
# call — see degraded_fields().
REQUIRED_COLUMNS = ["id", "geometry", "bbox", "subtype", "class", "names"]
ESSENTIAL_COLUMNS = {"geometry", "bbox"}

DEFAULT_RADIUS_M = 500
DEFAULT_LIMIT = 10
MAX_ROWS = overture.MAX_ROWS  # same response-size cap every other tool uses


def _ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once.

    Thin wrapper over db.ensure_spatial() (mirrors land_use.py/buildings.py).
    """
    try:
        db.ensure_spatial()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(f"could not load spatial extension: {e}") from e


def _geom_expr(upstream: str) -> str:
    """SQL expression yielding a GEOMETRY for the dataset's geometry column."""
    return geo.geom_expr(upstream)


def _cache_theme() -> str:
    """Composite cache theme key for this base-theme type — see module docstring.

    Underscore separator, not ':': cache.tile_path uses this string
    verbatim as a directory component, and ':' is illegal in a Windows
    path component (the mkdir would raise WinError 123 on the first
    cached query).
    """
    return f"{THEME}_{TYPE_}"


def set_data_path(path: str | None) -> None:
    """Point infrastructure queries at path instead of live S3 (tests)."""
    overture.set_data_path(path, theme=THEME, type_=TYPE_)


def _upstream_glob() -> str:
    return overture._upstream_glob(THEME, type_=TYPE_)


def _check_schema(glob: str) -> list[str]:
    """Missing REQUIRED_COLUMNS for glob, raising SchemaDegraded if any are essential."""
    missing = overture.missing_columns(glob, REQUIRED_COLUMNS)
    essential_missing = [c for c in missing if c in ESSENTIAL_COLUMNS]
    if essential_missing:
        raise overture.SchemaDegraded(essential_missing)
    return missing


def degraded_fields() -> list[str]:
    """Non-essential REQUIRED_COLUMNS missing from the active infrastructure dataset."""
    missing = overture.missing_columns(_upstream_glob(), REQUIRED_COLUMNS)
    return [c for c in missing if c not in ESSENTIAL_COLUMNS]


def _from_source(bbox: tuple[float, float, float, float]) -> str:
    """SQL FROM-clause source for an infrastructure query: local cache tiles, or upstream.

    Mirrors land_use._from_source, keyed by _cache_theme() so base-theme
    types can never share a tile directory.
    """
    upstream = _upstream_glob()
    if cache.enabled():
        try:
            with db.conn_lock:
                paths = cache.local_paths_for_query(
                    db.shared_conn(), release.resolve_release(), _cache_theme(), bbox,
                    upstream, db.new_connection,
                )
        except duckdb.Error as e:
            raise overture.UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


# Haversine distance in meters between the closest point on a row's
# geometry (nlat/nlon, computed once in the `nearest` CTE below) and the
# query point ($lat/$lon) — same formula as overture.DISTANCE_EXPR, just
# reading the CTE's columns instead of a bbox corner.
_NEAREST_DISTANCE_EXPR = """2 * 6371000 * asin(sqrt(
                pow(sin(radians(nlat - $lat) / 2), 2)
                + cos(radians($lat)) * cos(radians(nlat))
                * pow(sin(radians(nlon - $lon) / 2), 2)
            ))"""


def infrastructure_at(
    lat: float,
    lon: float,
    radius_m: float = DEFAULT_RADIUS_M,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict], float]:
    """Nearest infrastructure features to a point, nearest first, compact rows.

    Returns (rows, effective_radius_m) where each row is {"subtype",
    "class", "name", "distance_m"}. No geometry (design rule: answers, not
    data). distance_m is measured to the closest point on the feature, not
    its centroid, so a bridge you are standing on reads ~0 m — see the
    module docstring for the approximation that leaves.

    An empty list is a valid answer, not an error: base-theme coverage is
    OSM-derived and patchy, and "nothing within radius_m" is exactly the
    kind of finding this tool exists to give. The returned radius is the
    *effective* one (geo.clamp_radius_m may have lowered it), so a caller
    echoing it never misdescribes the search.

    Raises SchemaDegraded if geometry or bbox is missing from the active
    dataset, or UpstreamUnavailable if the remote scan (or the one-time
    spatial extension load) fails. Non-essential columns (subtype, class,
    names) missing from the dataset come back as None in their field —
    see degraded_fields().
    """
    _ensure_spatial()
    # int() before the SQL LIMIT interpolation — defense in depth for any
    # direct (non-MCP) caller; the MCP layer already validates the type.
    limit = max(0, min(int(limit), MAX_ROWS))
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    geom_expr = _geom_expr(upstream)

    radius_m = geo.clamp_radius_m(radius_m)
    xmin, ymin, xmax, ymax = geo.bbox_around(lat, lon, radius_m)
    # Cheap row-group prefilter with *intersection* semantics before the
    # exact distance test: a line or polygon that reaches into the search
    # circle usually has a bbox far larger than it, so anything stricter
    # would drop rows the distance predicate still wants.
    bbox_filter, params = geo.bbox_filter_sql(xmin, ymin, xmax, ymax)
    params = {**params, "lat": lat, "lon": lon, "radius_m": radius_m}

    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    class_expr = "NULL" if "class" in missing else "class"
    name_expr = "NULL" if "names" in missing else "names.primary"

    sql = f"""
        WITH nearest AS (
            SELECT
                {subtype_expr} AS subtype,
                {class_expr}   AS class,
                {name_expr}    AS name,
                ST_X(ST_ClosestPoint({geom_expr}, ST_Point($lon, $lat))) AS nlon,
                ST_Y(ST_ClosestPoint({geom_expr}, ST_Point($lon, $lat))) AS nlat
            FROM {_from_source((xmin, ymin, xmax, ymax))}
            WHERE {bbox_filter}
        )
        SELECT subtype, class, name, {_NEAREST_DISTANCE_EXPR} AS distance_m
        FROM nearest
        WHERE {_NEAREST_DISTANCE_EXPR} <= $radius_m
        ORDER BY distance_m
        LIMIT {limit}
    """
    try:
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e

    results = [
        {
            "subtype": subtype,
            "class": class_,
            "name": name,
            "distance_m": round(distance_m, 1),
        }
        for subtype, class_, name, distance_m in rows
    ]
    return results, radius_m
