"""Nearest transit stops, a filtered view over Overture base/infrastructure (issue #453).

Not a new query pipeline: infrastructure.py's exact query shape (radius
search, bbox prefilter, closest-point distance for mixed point/line/polygon
geometry, degrade-on-missing-column, per-type cache theme, MAX_ROWS response
cap — see infrastructure.py's module docstring for why each of those exists)
restricted to `subtype='transit' AND class IN <a fixed stop-like set>`, with
a compact {id, kind, name, distance_m} row shape instead of infrastructure_at's
{id, subtype, class, name, distance_m}: subtype is always "transit" here, so
it is dropped, and "kind" is the `class` column under a name that does not
collide with Python's builtin.

Overture's base/infrastructure `class` enum for subtype='transit' (schema:
https://github.com/OvertureMaps/schema/blob/main/packages/overture-schema-theme-base/src/overture/schema/base/infrastructure.py)
mixes three different kinds of row: actual boardable stops (bus_stop,
bus_station, railway_station, railway_halt, subway_station, tram_stop,
ferry_terminal, aerialway_station); OSM's per-geometry re-tagging of the
*same* stop (platform — the waiting area — and stop_position — the
vehicle's exact stopping point, often a separate node from the bus_stop
itself); and rows that are not stops at all (bicycle_parking,
bicycle_rental, parking). Issue #453's measurement around Dam Square found
bicycle_parking alone outnumbering every real stop class combined, and
platform/stop_position outnumbering bus_stop 2:1 — so an unfiltered
subtype='transit' query answers "nearest transit stop" with a bike rack.
This module never returns the parking/bicycle classes, and only reaches for
platform/stop_position as a fallback when zero real stops are in range (or
when `kind` asks for one of them by name) — see STOP_CLASSES /
FALLBACK_CLASSES.

What this deliberately does not do: no schedules, no live arrivals.
Overture base/infrastructure is a static conflation of OpenStreetMap, which
carries neither, so there is nothing here to return even in principle — a
caller wanting "next bus" needs a live transit API, not this tool. Base
coverage is also OSM-derived and patchy (see infrastructure.py's module
docstring): a city with rich OSM transit tagging returns rich results, one
without returns none, and an empty result is that real difference showing
through, a finding rather than a bug.

Dataset override: this module has no set_data_path of its own. It reads
theme=base/type=infrastructure through infrastructure.py's helpers
(_upstream_glob, _check_schema, _from_source, _geom_expr, _ensure_spatial,
_NEAREST_DISTANCE_EXPR, _BBOX_PREFILTER_PAD — all private, called directly
rather than duplicated) so infrastructure_at and transit_stops_near can
never point at two different datasets; infrastructure.set_data_path(path)
is the one override a test (or a live install) needs.
"""

import logging

import duckdb

from placeroot import db, geo, infrastructure, overture

logger = logging.getLogger(__name__)

# Real, boardable transit stops — the default answer. See the module
# docstring for where this list comes from and why platform/stop_position/
# bicycle_parking/bicycle_rental/parking are excluded.
STOP_CLASSES = frozenset(
    {
        "bus_stop",
        "bus_station",
        "railway_station",
        "railway_halt",
        "subway_station",
        "tram_stop",
        "ferry_terminal",
        "aerialway_station",
    }
)

# OSM's per-geometry re-tagging of the same physical stop: returned only as
# a fallback when no STOP_CLASSES row is within radius, or when `kind`
# names one of them directly. Never returned silently mixed in with real
# stops — a caller who got these back should know they are looking at a
# platform/stop-position record, not a bus_stop.
FALLBACK_CLASSES = frozenset({"platform", "stop_position"})

# Every value `kind` accepts. Parking/bicycle classes are deliberately not
# in this set: there is no way to ask this tool for a bike rack.
ALLOWED_KINDS = STOP_CLASSES | FALLBACK_CLASSES

DEFAULT_RADIUS_M = 800
DEFAULT_LIMIT = 10
MAX_ROWS = infrastructure.MAX_ROWS  # same response-size cap every other tool uses


def _run_query(
    lat: float, lon: float, radius_m: float, limit: int, classes: frozenset[str]
) -> tuple[list[dict], float, int, bool]:
    """One radius query for `class IN classes AND subtype='transit'`.

    Mirrors infrastructure.infrastructure_at's query shape (bbox prefilter,
    closest-point distance, nearest-first ORDER BY, COUNT(*) OVER() total)
    exactly, reading infrastructure.py's private schema/source helpers
    directly rather than re-implementing them, but with an exact-match
    `class IN (...)` filter instead of infrastructure_at's ILIKE substring
    filters — a stop's class must match one of `classes` exactly, not
    "contains this text".

    Returns (rows, effective_radius_m, total_in_range, class_missing).
    class_missing is True when the active dataset has no `class` column at
    all, in which case rows is always [] — see transit_stops_near's
    docstring for why that is the honest answer rather than a guess.
    """
    infrastructure._ensure_spatial()
    limit = max(1, min(int(limit), MAX_ROWS))
    upstream = infrastructure._upstream_glob()
    missing = set(infrastructure._check_schema(upstream))
    geom_expr = infrastructure._geom_expr(upstream)

    radius_m = geo.clamp_radius_m(radius_m)

    if "class" in missing:
        # The whole point of this tool is telling a boardable stop apart
        # from a bike rack or a duplicate platform record; without `class`
        # there is no way to do that safely. Degrading to "everything
        # matches" would silently hand back bicycle parking as a transit
        # stop, so this degrades to an honest empty answer instead (see
        # transit_stops_near's docstring / server.py's note wiring).
        return [], radius_m, 0, True

    bbox = geo.bbox_around(lat, lon, radius_m * infrastructure._BBOX_PREFILTER_PAD)
    xmin, ymin, xmax, ymax = bbox
    bbox_filter, params = geo.bbox_filter_sql(xmin, ymin, xmax, ymax)
    params = {
        **params,
        "lat": lat,
        "lon": lon,
        "radius_m": radius_m,
        "classes": sorted(classes),
    }

    id_expr = "NULL" if "id" in missing else "id"
    name_expr = "NULL" if "names" in missing else "names.primary"

    # `class IN (SELECT unnest($classes))` binds the fixed Python set as a
    # DuckDB list parameter (same pattern as geocode.py's id-list lookup)
    # rather than interpolating the class names into the SQL text.
    filters = [bbox_filter, "class IN (SELECT unnest($classes))"]
    if "subtype" not in missing:
        # subtype='transit' is a literal, not a bound value: Overture's
        # transit classes (bus_stop, platform, ...) are not unique to the
        # transit subtype in principle, so this keeps e.g. a `parking`-class
        # row under some other subtype from ever entering `classes`' match.
        filters.append("subtype = 'transit'")

    nlon_expr, nlat_expr = geo.closest_point_sql(geom_expr)
    distance_expr = infrastructure._NEAREST_DISTANCE_EXPR

    sql = f"""
        WITH nearest AS (
            SELECT
                {id_expr} AS id,
                class     AS kind,
                {name_expr} AS name,
                {nlon_expr} AS nlon,
                {nlat_expr} AS nlat
            FROM {infrastructure._from_source(bbox)}
            WHERE {" AND ".join(filters)}
        ),
        in_range AS (
            SELECT id, kind, name, {distance_expr} AS distance_m
            FROM nearest
            WHERE {distance_expr} <= $radius_m
        )
        SELECT id, kind, name, distance_m, COUNT(*) OVER () AS total_in_range
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
        {"id": id_, "kind": kind, "name": name, "distance_m": round(distance_m, 1)}
        for id_, kind, name, distance_m, _total in rows
    ]
    total_in_range = rows[0][-1] if rows else 0
    return results, radius_m, total_in_range, False


def transit_stops_near(
    lat: float,
    lon: float,
    radius_m: float = DEFAULT_RADIUS_M,
    limit: int = DEFAULT_LIMIT,
    kind: str | None = None,
) -> tuple[list[dict], float, int, bool, bool]:
    """Nearest transit stops to a point, nearest first, compact rows.

    Returns (rows, effective_radius_m, total_in_range, fallback_used,
    class_missing) where each row is {"id", "kind", "name", "distance_m"}.
    kind is Overture's `class` value (e.g. "bus_stop", "railway_station").
    No geometry (design rule this module inherits from infrastructure.py:
    answers, not data); id is the GERS id, composable with gers_lookup.

    Default search (kind=None) restricts to STOP_CLASSES — real, boardable
    stops — and excludes platform/stop_position/bicycle_parking/
    bicycle_rental/parking entirely. If zero STOP_CLASSES rows are within
    radius_m, this re-queries the same radius for FALLBACK_CLASSES
    (platform, stop_position) and returns those instead, with
    fallback_used=True so the caller can say so: these are OSM's
    per-geometry re-tagging of the same physical stops, not a different
    kind of place.

    `kind` restricts to exactly that one class (exact match, not the
    fallback logic above) — callers passing kind themselves choose whether
    to ask for a real stop class or a platform/stop_position record.
    Validating that `kind` is one of ALLOWED_KINDS is the caller's job
    (server.py's transit_stops_near tool does this before calling in,
    returning a bad_request naming the accepted values) so this function
    trusts its `kind` argument.

    total_in_range is the count that matched radius_m and the active class
    filter *before* limit, same convention as infrastructure_at, so a
    caller can tell "these are all of them" from "these are the N nearest
    of total_in_range".

    class_missing is True when the active dataset has no `class` column:
    there is then no way to distinguish a stop from a bike rack, so rows
    is always [] rather than an unfiltered guess.

    An empty result is a valid answer, not an error: base-theme coverage is
    OSM-derived and patchy (see infrastructure.py's module docstring), and
    "no stop within radius_m" is a real finding. radius_m in the return is
    the *effective* one (geo.clamp_radius_m may have lowered it).

    Raises SchemaDegraded (via infrastructure._check_schema) if geometry or
    bbox is missing from the active dataset, or UpstreamUnavailable if the
    remote scan (or the one-time spatial extension load) fails.
    """
    classes = frozenset({kind}) if kind is not None else STOP_CLASSES
    rows, radius_m, total_in_range, class_missing = _run_query(lat, lon, radius_m, limit, classes)
    fallback_used = False
    if not rows and not class_missing and kind is None:
        rows, radius_m, total_in_range, class_missing = _run_query(
            lat, lon, radius_m, limit, FALLBACK_CLASSES
        )
        fallback_used = bool(rows)
    return rows, radius_m, total_in_range, fallback_used, class_missing
