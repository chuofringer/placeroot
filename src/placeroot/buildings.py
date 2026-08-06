"""Building footprints from Overture's buildings theme (issue #23).

The buildings theme is the largest in Overture (hundreds of millions of
footprint polygons worldwide) — every query here goes through the same
bbox prefilter divisions.py adopted after its own "point-in-polygon over
every division on Earth measured 10+ minutes live" lesson (issue #11).
Never scan theme=buildings/type=building without one.

Data-path injection follows overture.py's per-theme(+type) design exactly:
buildings.set_data_path(path) resolves to
overture.set_data_path(path, theme="buildings", type_="building"), and reads
go through overture._upstream_glob(THEME, type_="building") — the same
pattern divisions.py established for its own type=division_area queries.

Geometry handling mirrors divisions._geom_expr: real Overture GeoParquet
carries geo metadata so DuckDB spatial reads `geometry` as a native
GEOMETRY column directly; our WKB-BLOB test fixture needs
ST_GeomFromWKB(geometry) first. Probed once per glob, same as divisions.py.

Area/distance in meters, and why not ST_*_Spheroid: DuckDB spatial's
geodesic family (ST_Area_Spheroid, ST_Distance_Spheroid,
ST_Perimeter_Spheroid) was verified empirically against the installed
extension (duckdb 1.5.5, spatial build eb1e57c) and returns NaN for every
input tried — a real polygon, a real point pair, all of it. Rather than
ship a stat that's silently NaN, this module uses the same trick
simplify.py already uses for RDP simplification: reproject to a local
equirectangular tangent plane in meters (scaled by cos(reference
latitude), METERS_PER_DEGREE_LAT imported from simplify.py so both
modules agree on the constant) and compute ST_Area/distance in that plane.
Building footprints are small (tens of meters across) and queries are
local (radius_m rarely exceeds a few km), so the flat-earth error here is
negligible — nothing like the divisions-theme country-polygon case where
it would matter.

Distance is measured from each footprint's centroid, not (as places.py
does) a bbox corner: places' bbox is a single point (xmin == xmax), so the
corner *is* the place's location; a building's bbox spans its whole
footprint, so its corner is a poor stand-in for "where the building is."
ST_Centroid(geom) is computed once per candidate row (aliased clat/clon in
the `filtered` CTE) and reused by every consumer of the haversine formula
below, not recomputed per use.

Tile cache: buildings shares cache.py's tile-materialization path exactly
as places.py does, via theme="buildings" (cache.py only ever keys off the
theme string, so this works as-is with no changes there) — the only
wrinkle is that overture._from_source() hardcodes type_="place" for its
own upstream-glob resolution, so this module has its own _from_source()
that's identical except for passing type_="building" through to
overture._upstream_glob().

Design rule: answers, not data. Both tools omit raw geometry by default;
buildings_at's include_geometry=True opts back in, but even then each
footprint is passed through simplify.simplify_geometry() capped at
PER_ROW_GEOMETRY_TOKEN_CAP tokens each — small enough that `limit` rows'
worth of geometry still fits the overall response budget applied at the
tool boundary (server.py).
"""

import json
import logging
import math
from collections import Counter

import duckdb

from placeroot import cache, db, geo, overture, release, simplify
from placeroot.simplify import METERS_PER_DEGREE_LAT

logger = logging.getLogger(__name__)

THEME = "buildings"
TYPE_ = "building"

# geometry/bbox are essential (no query can run without them, same as
# divisions.py's stance on geometry); everything else degrades gracefully —
# a tool still answers, just without that field/stat, and callers see it
# reflected in degraded_fields().
REQUIRED_COLUMNS = ["id", "geometry", "bbox", "subtype", "class", "height", "num_floors"]
ESSENTIAL_COLUMNS = {"geometry", "bbox"}

DEFAULT_SUMMARIZE_RADIUS_M = 500
DEFAULT_NEAREST_RADIUS_M = 100
DEFAULT_NEAREST_LIMIT = 10
MAX_ROWS = overture.MAX_ROWS  # same response-size cap every other tool uses
TOP_N_BREAKDOWN = 10

# Small per-row cap so `limit` rows of simplified geometry still fit inside
# the overall response's token budget (applied separately, at the tool
# boundary) instead of one row's geometry alone blowing it.
PER_ROW_GEOMETRY_TOKEN_CAP = 200


def _ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once.

    Thin wrapper over db.ensure_spatial(), keeping this module's
    UpstreamUnavailable-on-failure contract.
    """
    try:
        db.ensure_spatial()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(f"could not load spatial extension: {e}") from e


def _geom_expr(upstream: str) -> str:
    """SQL expression yielding a GEOMETRY for the dataset's geometry column.

    Thin wrapper over geo.geom_expr() (issue #40) — identical logic to
    divisions.py's former copy; both now delegate to the one shared
    implementation.
    """
    return geo.geom_expr(upstream)


def set_data_path(path: str | None) -> None:
    """Point buildings queries at path instead of live S3 (tests).

    Delegates to overture.set_data_path's per-theme+type override — the
    same mechanism divisions.py uses for its own type=division_area glob.
    """
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
    """Non-essential REQUIRED_COLUMNS missing from the currently active buildings dataset."""
    missing = overture.missing_columns(_upstream_glob(), REQUIRED_COLUMNS)
    return [c for c in missing if c not in ESSENTIAL_COLUMNS]


def _from_source(bbox: tuple[float, float, float, float]) -> str:
    """SQL FROM-clause source for a buildings query: local cache tiles, or upstream.

    Mirrors overture._from_source, just pinned to type_="building" — see
    the module docstring's "Tile cache" section for why this can't reuse
    overture._from_source directly (it hardcodes type_="place").
    """
    upstream = _upstream_glob()
    if cache.enabled():
        try:
            with db.conn_lock:
                paths = cache.local_paths_for_query(
                    db.shared_conn(), release.resolve_release(), THEME, bbox, upstream,
                    db.new_connection,
                )
        except duckdb.Error as e:
            raise overture.UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


def _bbox_filter(lat: float, lon: float, radius_m: float) -> tuple[str, dict]:
    """Cheap row-group bbox prefilter + the params the centroid distance test also needs.

    radius_m is clamped to geo.MAX_QUERY_RADIUS_M (an abuse guard against a
    world-spanning bbox — see geo.clamp_radius_m), the clamped value driving
    both the bbox and the $radius_m centroid-distance parameter so they agree.
    """
    radius_m = geo.clamp_radius_m(radius_m)
    xmin, ymin, xmax, ymax = geo.bbox_around(lat, lon, radius_m)
    bbox_filter = (
        "bbox.xmax >= $xmin AND bbox.xmin <= $xmax"
        " AND bbox.ymax >= $ymin AND bbox.ymin <= $ymax"
    )
    params = {
        "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
        "lat": lat, "lon": lon, "radius_m": radius_m,
    }
    return bbox_filter, params


# Haversine distance in meters between a footprint's centroid (clat/clon,
# computed once in the `filtered` CTE below) and the named query point
# ($lat/$lon) — the exact-circle filter applied after the bbox prefilter,
# same two-stage shape as overture.area_geometry.
_CENTROID_DISTANCE_EXPR = """2 * 6371000 * asin(sqrt(
                pow(sin(radians(clat - $lat) / 2), 2)
                + cos(radians($lat)) * cos(radians(clat))
                * pow(sin(radians(clon - $lon) / 2), 2)
            ))"""


def _area_m2(area_deg2: float | None, ref_lat: float) -> float | None:
    """Convert a planar ST_Area() result (in degrees^2) to square meters.

    Equirectangular approximation about ref_lat — see the module docstring
    for why ST_Area_Spheroid isn't used. Exact for a lon/lat-aligned
    rectangle at ref_lat, a close approximation for anything else at
    building scale.
    """
    if area_deg2 is None:
        return None
    mpd_lon = METERS_PER_DEGREE_LAT * max(math.cos(math.radians(ref_lat)), 1e-6)
    return area_deg2 * METERS_PER_DEGREE_LAT * mpd_lon


def summarize_buildings(
    lat: float, lon: float, radius_m: float = DEFAULT_SUMMARIZE_RADIUS_M
) -> dict:
    """Building stats for an area: count, footprint area, height/floor coverage, subtype/class mix.

    Returns {"center", "radius_m", "count", "total_footprint_area_m2",
    "mean_footprint_area_m2", "height_known_pct", "mean_height_m" (only
    present if any height is known), "num_floors_known_pct",
    "mean_num_floors", "top_subtypes": [{"subtype", "count"}, ...] (top 10
    by count), "uncategorized_subtype_count", "top_classes": [...]}.
    height/num_floors are sparse in real Overture data — *_known_pct
    reports coverage rather than pretending every row has a value.

    If a whole column (height, num_floors, subtype, class) is missing from
    the active dataset entirely (not just sparse — actually absent from the
    schema), its stat(s) are omitted from the response rather than reported
    as 0% — see degraded_fields() for which columns that affects.

    Raises SchemaDegraded if geometry or bbox is missing (no way to
    measure/locate footprints at all), or UpstreamUnavailable if the remote
    scan (or the one-time spatial extension load) fails.
    """
    _ensure_spatial()
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    bbox_filter, params = _bbox_filter(lat, lon, radius_m)
    bbox = (params["xmin"], params["ymin"], params["xmax"], params["ymax"])
    geom_expr = _geom_expr(upstream)

    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    class_expr = "NULL" if "class" in missing else "class"
    height_expr = "NULL" if "height" in missing else "height"
    floors_expr = "NULL" if "num_floors" in missing else "num_floors"

    sql = f"""
        WITH filtered AS (
            SELECT
                {subtype_expr} AS subtype,
                {class_expr}   AS class,
                {height_expr}  AS height,
                {floors_expr}  AS num_floors,
                ST_X(ST_Centroid({geom_expr})) AS clon,
                ST_Y(ST_Centroid({geom_expr})) AS clat,
                ST_Area({geom_expr})           AS area_deg2
            FROM {_from_source(bbox)}
            WHERE {bbox_filter}
        )
        SELECT subtype, class, height, num_floors, area_deg2
        FROM filtered
        WHERE {_CENTROID_DISTANCE_EXPR} <= $radius_m
    """
    try:
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e

    n = len(rows)
    areas_m2 = [
        m2 for (_, _, _, _, area_deg2) in rows
        if (m2 := _area_m2(area_deg2, lat)) is not None
    ]
    heights = [h for (_, _, h, _, _) in rows if h is not None]
    floors = [f for (_, _, _, f, _) in rows if f is not None]
    subtype_counts = Counter(s for (s, _, _, _, _) in rows if s is not None)
    class_counts = Counter(c for (_, c, _, _, _) in rows if c is not None)

    result = {
        "center": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "count": n,
    }
    if areas_m2:
        result["total_footprint_area_m2"] = round(sum(areas_m2), 1)
        result["mean_footprint_area_m2"] = round(sum(areas_m2) / len(areas_m2), 1)
    if "height" not in missing:
        result["height_known_pct"] = round(100 * len(heights) / n, 1) if n else 0.0
        if heights:
            result["mean_height_m"] = round(sum(heights) / len(heights), 1)
    if "num_floors" not in missing:
        result["num_floors_known_pct"] = round(100 * len(floors) / n, 1) if n else 0.0
        if floors:
            result["mean_num_floors"] = round(sum(floors) / len(floors), 1)
    if "subtype" not in missing:
        result["top_subtypes"] = [
            {"subtype": s, "count": c} for s, c in subtype_counts.most_common(TOP_N_BREAKDOWN)
        ]
        result["uncategorized_subtype_count"] = n - sum(subtype_counts.values())
    if "class" not in missing:
        result["top_classes"] = [
            {"class": c, "count": cnt} for c, cnt in class_counts.most_common(TOP_N_BREAKDOWN)
        ]
    return result


def buildings_at(
    lat: float,
    lon: float,
    radius_m: float = DEFAULT_NEAREST_RADIUS_M,
    limit: int = DEFAULT_NEAREST_LIMIT,
    include_geometry: bool = False,
) -> list[dict]:
    """Nearest building footprints to a point, nearest first, compact rows.

    Returns [{"id" (GERS), "subtype", "class", "footprint_area_m2",
    "height_m", "num_floors", "distance_m"}, ...]. No raw geometry by
    default — design rule, answers not data. Pass include_geometry=True to
    also get each row's footprint as GeoJSON, simplified to fit
    PER_ROW_GEOMETRY_TOKEN_CAP tokens (with "geometry_max_deviation_m"
    reporting what was lost); omit it (the default) for a purely tabular
    answer.

    Raises SchemaDegraded if geometry or bbox is missing from the active
    dataset, or UpstreamUnavailable if the remote scan (or the one-time
    spatial extension load) fails. Non-essential columns (subtype, class,
    height, num_floors, id) missing from the dataset come back as None in
    their field — see degraded_fields().
    """
    _ensure_spatial()
    # int() before the SQL LIMIT interpolation — defense in depth for any
    # direct (non-MCP) caller; the MCP layer already validates the type.
    limit = max(0, min(int(limit), MAX_ROWS))
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    bbox_filter, params = _bbox_filter(lat, lon, radius_m)
    bbox = (params["xmin"], params["ymin"], params["xmax"], params["ymax"])
    geom_expr = _geom_expr(upstream)

    id_expr = "NULL" if "id" in missing else "id"
    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    class_expr = "NULL" if "class" in missing else "class"
    height_expr = "NULL" if "height" in missing else "height"
    floors_expr = "NULL" if "num_floors" in missing else "num_floors"
    # Only compute+serialize GeoJSON when actually asked for it — it's the
    # most expensive column in this query by far.
    geojson_expr = f"ST_AsGeoJSON({geom_expr})" if include_geometry else "NULL"

    sql = f"""
        WITH filtered AS (
            SELECT
                {id_expr}      AS id,
                {subtype_expr} AS subtype,
                {class_expr}   AS class,
                {height_expr}  AS height,
                {floors_expr}  AS num_floors,
                ST_X(ST_Centroid({geom_expr})) AS clon,
                ST_Y(ST_Centroid({geom_expr})) AS clat,
                ST_Area({geom_expr})           AS area_deg2,
                {geojson_expr}                 AS geojson
            FROM {_from_source(bbox)}
            WHERE {bbox_filter}
        )
        SELECT id, subtype, class, height, num_floors, area_deg2, geojson,
               {_CENTROID_DISTANCE_EXPR} AS distance_m
        FROM filtered
        WHERE {_CENTROID_DISTANCE_EXPR} <= $radius_m
        ORDER BY distance_m
        LIMIT {limit}
    """
    try:
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e

    results = []
    for id_, subtype, class_, height, num_floors, area_deg2, geojson_text, distance_m in rows:
        row = {
            "id": id_,
            "subtype": subtype,
            "class": class_,
            "footprint_area_m2": (
                round(a, 1) if (a := _area_m2(area_deg2, lat)) is not None else None
            ),
            "height_m": height,
            "num_floors": num_floors,
            "distance_m": round(distance_m, 1),
        }
        if include_geometry and geojson_text:
            geom = json.loads(geojson_text)
            simplified = simplify.simplify_geometry(geom, max_tokens=PER_ROW_GEOMETRY_TOKEN_CAP)
            row["geometry"] = simplified["geometry"]
            row["geometry_max_deviation_m"] = round(simplified["max_deviation_m"], 2)
        results.append(row)
    return results
