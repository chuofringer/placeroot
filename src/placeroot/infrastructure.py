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

The approximation left: ST_ClosestPoint picks its nearest vertex/edge
position in *planar* lon/lat degree space, which is not the nearest
position on the ground — a degree of longitude is cos(latitude) shorter
than a degree of latitude. This was originally assumed second order; it
is not. Measured on a diagonal segment, the point degree-space picks is
25% too far at 60 deg N and 63% too far at 70 deg N, enough to reorder
the nearest-first list and to push an in-range feature past the
radius_m cutoff. So the search runs in a cos(latitude)-scaled space
(geo.closest_point_sql) and the longitude is divided back out before the
haversine. At the antimeridian seam it is a known limitation, not a small
error: for a line or polygon near lon=±180 queried from the opposite
side of the seam, planar degree distance picks the geodesically *far*
end of the geometry (359.99 degrees "away" planar is 0.01 degrees away
on the sphere), so distance_m is overstated and an in-range feature can
be dropped by the radius predicate. This matches the repo's existing
seam behavior — overture.DISTANCE_EXPR measures to a raw bbox corner
and misreports the same rows — so it is accepted here rather than
special-cased; a repo-wide seam-aware distance is follow-up material.
The reported distance itself is a proper haversine of the picked point,
identical in formula to overture.DISTANCE_EXPR, not a planar number.

Cache theme key: "base_infrastructure", following land_use.py's composite
"base_<type>" convention — cache.py uses the theme string verbatim as a
directory component, so it must stay distinct per base-theme type and
must avoid ':' (illegal in a Windows path component).

What the data actually looks like (the thing that shapes this module's
API): Overture base/infrastructure is not mostly landmarks. It is mostly
street furniture — street_lamp, bench, waste_basket, bollard, kerb,
crossing outnumber the bridges/towers/piers roughly 50:1 in a dense city
centre. A nearest-first query around Dam Square at r=500 has 1263 rows in
range, including 72 bridges and 29 towers, and its 10 nearest are all
lamps and benches. Two consequences, both handled here: the query returns
the true in-range count alongside the clipped rows, so a caller can never
mistake "the 10 nearest" for "all there is" (an unflagged slice turns
"is there a bridge near here?" into a confident, wrong "no"); and
subtype/class filters exist so "bridges near here" is directly askable.

Design rule: answers, not data — compact rows ({id, subtype, class, name,
distance_m}), no geometry, no source_tags. id is the GERS id, kept so a
row composes with gers_lookup. Empty results are a first-class answer:
base-theme coverage is OSM-derived and patchy, and "no infrastructure
within 500 m" is a real, useful finding, not an error.
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

# The bbox prefilter is built from geo.bbox_around, which converts metres to
# degrees with 111_320.0 m/deg (a WGS84 mid-latitude figure) while the exact
# distance predicate below is a spherical haversine at R=6371000, i.e.
# 111_194.93 m/deg. The prefilter box is therefore ~0.11% *narrower* than the
# circle it is supposed to contain, and a bare POINT sitting in that outer
# ring gets dropped before the exact test ever sees it (a tower 499.9 m due
# north is missed at radius_m=500). The exact ratio is 111320 / 111194.93 =
# 1.00112, so the pad has to exceed that — 1.001 is *not* enough, it still
# loses a point at exactly 500 m. 1.002 clears it with margin; the exact
# predicate still decides membership, so the pad can only add candidate
# rows, never results.
# geo.py's constant is shared by every theme module, so it is left alone here
# — see the PR body's repo-wide follow-up note.
_BBOX_PREFILTER_PAD = 1.002


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
# query point ($lat/$lon) — the shared expression every theme uses, just
# reading the CTE's columns instead of a bbox corner.
_NEAREST_DISTANCE_EXPR = geo.haversine_sql("nlat", "nlon")


def _attribute_filters(
    missing: set[str], subtype: str | None, infra_class: str | None, params: dict
) -> list[str]:
    """subtype/class filter clauses (params added in place).

    Same semantics as overture._place_category_name_filters' category/name
    filters — case-insensitive substring match on a *bound* parameter, with
    the caller's own LIKE metacharacters escaped so Overture's snake_case
    values (power_tower, waste_basket) match literally rather than turning
    '_' into a single-character wildcard. A filter whose column is absent
    from the active dataset is a no-op rather than an error, matching how
    every other theme's filters degrade.
    """
    filters = []
    if subtype and "subtype" not in missing:
        filters.append("subtype ILIKE $subtype ESCAPE '\\'")
        params["subtype"] = f"%{overture._like_escape(subtype)}%"
    if infra_class and "class" not in missing:
        filters.append("class ILIKE $infra_class ESCAPE '\\'")
        params["infra_class"] = f"%{overture._like_escape(infra_class)}%"
    return filters


def infrastructure_at(
    lat: float,
    lon: float,
    radius_m: float = DEFAULT_RADIUS_M,
    limit: int = DEFAULT_LIMIT,
    subtype: str | None = None,
    infra_class: str | None = None,
) -> tuple[list[dict], float, int]:
    """Nearest infrastructure features to a point, nearest first, compact rows.

    Returns (rows, effective_radius_m, total_in_range) where each row is
    {"id", "subtype", "class", "name", "distance_m"}. No geometry (design
    rule: answers, not data); id is the feature's GERS id, so a row can be
    handed straight to gers_lookup. distance_m is measured to the closest
    point on the feature, not its centroid, so a bridge you are standing on
    reads ~0 m — see the module docstring for the approximation that leaves.

    total_in_range is how many features matched the radius and filters
    *before* the LIMIT, so a caller can tell "these are all of them" from
    "these are the 10 nearest of 1263" — the distinction that matters most
    here, because the dataset is dominated by street furniture (see the
    module docstring) and an unflagged truncated answer reads as a
    confident "there is no bridge near here".

    subtype/infra_class narrow the search by Overture's subtype and class
    columns (case-insensitive substring, wildcards escaped). infra_class is
    the `class` column under a non-reserved name.

    An empty list is a valid answer, not an error: base-theme coverage is
    OSM-derived and patchy, and "nothing within radius_m" is exactly the
    kind of finding this tool exists to give. The returned radius is the
    *effective* one (geo.clamp_radius_m may have lowered it), so a caller
    echoing it never misdescribes the search.

    Raises SchemaDegraded if geometry or bbox is missing from the active
    dataset, or UpstreamUnavailable if the remote scan (or the one-time
    spatial extension load) fails. Non-essential columns (id, subtype,
    class, names) missing from the dataset come back as None in their field
    — see degraded_fields().
    """
    _ensure_spatial()
    # int() before the SQL LIMIT interpolation — defense in depth for any
    # direct (non-MCP) caller; the MCP layer already validates the type.
    # Floor of 1, not 0: total_in_range rides a window function on the
    # returned rows, so LIMIT 0 would return zero rows AND report
    # total_in_range=0 with features in range — exactly the confident
    # wrong "nothing here" this module's truncation reporting exists to
    # prevent. There is no honest zero-row answer shape, so limit<=0 is
    # treated as "the single nearest".
    limit = max(1, min(int(limit), MAX_ROWS))
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    geom_expr = _geom_expr(upstream)

    radius_m = geo.clamp_radius_m(radius_m)
    # Cheap row-group prefilter with *intersection* semantics before the
    # exact distance test: a line or polygon that reaches into the search
    # circle usually has a bbox far larger than it, so anything stricter
    # would drop rows the distance predicate still wants. The radius is
    # padded because the box is built in degrees off a different constant
    # than the haversine below — see _BBOX_PREFILTER_PAD.
    bbox = geo.bbox_around(lat, lon, radius_m * _BBOX_PREFILTER_PAD)
    xmin, ymin, xmax, ymax = bbox
    bbox_filter, params = geo.bbox_filter_sql(xmin, ymin, xmax, ymax)
    params = {**params, "lat": lat, "lon": lon, "radius_m": radius_m}

    id_expr = "NULL" if "id" in missing else "id"
    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    class_expr = "NULL" if "class" in missing else "class"
    name_expr = "NULL" if "names" in missing else "names.primary"

    filters = [bbox_filter, *_attribute_filters(missing, subtype, infra_class, params)]

    # Latitude-corrected: raw ST_ClosestPoint picks the nearest point in
    # degree space, which is not the nearest point on the ground away from
    # the equator. See geo.closest_point_sql.
    nlon_expr, nlat_expr = geo.closest_point_sql(geom_expr)

    # COUNT(*) OVER () is evaluated over the whole in-range set, before the
    # LIMIT clips it — that is what lets the caller say "10 of 1263" instead
    # of silently presenting a slice of street furniture as the answer. The
    # rows are already materialized for the ORDER BY, so this costs nothing
    # extra beyond the count itself.
    sql = f"""
        WITH nearest AS (
            SELECT
                {id_expr}      AS id,
                {subtype_expr} AS subtype,
                {class_expr}   AS class,
                {name_expr}    AS name,
                {nlon_expr} AS nlon,
                {nlat_expr} AS nlat
            FROM {_from_source(bbox)}
            WHERE {' AND '.join(filters)}
        ),
        in_range AS (
            SELECT id, subtype, class, name, {_NEAREST_DISTANCE_EXPR} AS distance_m
            FROM nearest
            WHERE {_NEAREST_DISTANCE_EXPR} <= $radius_m
        )
        SELECT id, subtype, class, name, distance_m, COUNT(*) OVER () AS total_in_range
        FROM in_range
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
            "id": id_,
            "subtype": subtype,
            "class": class_,
            "name": name,
            "distance_m": round(distance_m, 1),
        }
        for id_, subtype, class_, name, distance_m, _total in rows
    ]
    # No rows means nothing was in range at all; with rows, every row
    # carries the same window-function total.
    total_in_range = rows[0][-1] if rows else 0
    return results, radius_m, total_in_range
