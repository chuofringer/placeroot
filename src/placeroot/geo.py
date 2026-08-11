"""Shared geometry helpers for the query layer.

bbox_around/bbox_filter_sql build antimeridian-safe bounding boxes;
geom_expr resolves the "native GEOMETRY column or WKB BLOB fixture" probe
shared by every theme module (as_wkt covers callers that need text).
"""

import logging
import math
from functools import lru_cache

import duckdb

from placeroot import db

logger = logging.getLogger(__name__)

# Upper bound (meters) on a query radius the tool layer will honor. radius_m
# comes straight from a tool caller, and nothing downstream bounded it: a
# single call with a multi-thousand-kilometre radius produces a bbox
# spanning most of the planet, which cache.local_paths_for_query() then fans
# out into tens of thousands of 1-degree tile fetches (a background thread +
# an upstream COPY each), and an effectively full-dataset upstream scan. 500
# km is already far larger than any "near a point" query these tools answer
# (results are capped at MAX_ROWS / the token budget regardless of radius),
# so clamping here is an abuse guard, not a functional limit. The
# transportation/isochrone path has its own, tighter per-mode caps and
# doesn't go through this.
MAX_QUERY_RADIUS_M = 500_000.0


def clamp_radius_m(radius_m: float) -> float:
    """Clamp a caller-supplied query radius to [0, MAX_QUERY_RADIUS_M].

    Non-finite input (NaN/inf) maps to 0.0 rather than propagating into the
    bbox math (math.floor(nan) raises). Callers use the returned value for
    *both* the bbox prefilter and the exact-distance predicate so the two
    stay consistent — clamping only the bbox would let it prune rows the
    distance filter still wants.
    """
    if not math.isfinite(radius_m):
        return 0.0
    return min(max(radius_m, 0.0), MAX_QUERY_RADIUS_M)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters (R=6371000), matching overture.DISTANCE_EXPR.

    Pure-Python counterpart to that SQL expression, for callers (like
    distance_matrix) that compute over caller-supplied points rather than
    rows already in a DuckDB query.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def haversine_sql(
    lat_expr: str, lon_expr: str, lat_param: str = "lat", lon_param: str = "lon"
) -> str:
    """SQL great-circle distance (meters, R=6371000) between a row expression and a param point.

    lat_expr/lon_expr are SQL expressions naming the row's coordinates
    (a bbox corner, a CTE column, ST_Y(...)); lat_param/lon_param name the
    DuckDB named parameters holding the query point. Same formula, same
    radius, as haversine_m() and overture.DISTANCE_EXPR — one place to read
    it, so the themes can't drift apart on what "distance" means.
    """
    return (
        "2 * 6371000 * asin(sqrt("
        f"pow(sin(radians({lat_expr} - ${lat_param}) / 2), 2)"
        f" + cos(radians(${lat_param})) * cos(radians({lat_expr}))"
        f" * pow(sin(radians({lon_expr} - ${lon_param}) / 2), 2)"
        "))"
    )


def closest_point_sql(
    geom_expr: str, lat_param: str = "lat", lon_param: str = "lon"
) -> tuple[str, str]:
    """(lon_expr, lat_expr) for the point on geom_expr truly nearest the query point.

    ST_ClosestPoint minimizes distance in *degree* space, and a degree of
    longitude is cos(latitude) shorter on the ground than a degree of
    latitude. Away from the equator the point it returns is therefore not
    the nearest one: on a diagonal segment the resulting distance is ~25%
    too large at 60 deg N and ~63% too large at 70 deg N. That number then
    feeds both the nearest-first ORDER BY and the `<= radius_m` in-range
    test, so the error does not merely misreport a distance — it silently
    drops real features in Norway, Alaska and Iceland and can rank a
    farther feature first.

    The fix is to run the search in a locally equal-scale space: scale
    longitude by cos(query latitude) for both the row geometry and the
    query point (a local equirectangular projection, and over the few
    kilometres a radius query spans the latitude term is effectively
    constant), take the closest point there, then divide the longitude back
    out. The caller haversines the recovered lon/lat exactly as before, so
    the distance is still a true great-circle metre — only the *choice* of
    point changes, and it changes to the right one.

    The cos factor is floored at 1e-9 so a query exactly at a pole cannot
    divide by zero; at that floor the search degenerates to "nearest in
    latitude", which is the correct answer at a pole, where every longitude
    meets.
    """
    k = f"greatest(cos(radians(${lat_param})), 1e-9)"
    scaled = (
        f"ST_ClosestPoint(ST_Scale({geom_expr}, {k}, 1.0),"
        f" ST_Point(${lon_param} * {k}, ${lat_param}))"
    )
    return f"ST_X({scaled}) / {k}", f"ST_Y({scaled})"


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Square bounding box guaranteed to contain the radius_m circle around (lat, lon).

    Not itself a radius filter (corners reach radius_m * sqrt(2)) — a cheap
    row-group prefilter; the exact circle is enforced by the haversine
    predicate in overture.area_geometry(). Latitude is clamped to [-90, 90]
    (issue #42: a pole-crossing search gets a band clamped at the pole
    rather than an invalid ymin/ymax). Longitude is left unclamped/
    unwrapped — xmin/xmax can fall outside [-180, 180] at the antimeridian;
    bbox_filter_sql folds that into an OR of two in-range boxes, and
    cache.tiles_for_bbox() enumerates tiles on both sides of the seam
    directly from these raw values.
    """
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    # Near a pole, cos(lat) floors to 1e-6 and dlon blows up to millions of
    # degrees even for a modest radius (issue #163's A1: lat=90 is a VALID
    # coordinate, but the unclamped dlon there made cache.tiles_for_bbox()
    # enumerate tens of millions of tiles before the MAX_TILES_PER_QUERY cap
    # ever got to look at len(tiles)). A half-width beyond 180/90 degrees
    # already covers every longitude/latitude there is, so clamping here is
    # lossless for any real query and just defuses the degenerate case at
    # its source.
    dlon = min(dlon, 180.0)
    dlat = min(dlat, 90.0)
    ymin = max(lat - dlat, -90.0)
    ymax = min(lat + dlat, 90.0)
    return lon - dlon, ymin, lon + dlon, ymax


def bbox_filter_sql(
    xmin: float, ymin: float, xmax: float, ymax: float
) -> tuple[str, dict]:
    """SQL bbox-intersection filter for [xmin, xmax] x [ymin, ymax], antimeridian-safe.

    xmin/xmax may be outside [-180, 180] (see bbox_around) when the box
    crosses the seam. Non-crossing case: the same single-box filter as
    always (byte-identical SQL — no perf regression for the overwhelming
    majority of queries). Crossing case: splits into an OR of two in-range
    boxes — [xmin_wrapped..180] and [-180..xmax_wrapped] — each still
    AND'd with the shared y-range. Reuses the same $xmin/$ymin/$xmax/$ymax
    param names in both cases so callers don't need to branch on shape.
    """
    if xmin >= -180.0 and xmax <= 180.0:
        filter_sql = (
            "bbox.xmax >= $xmin AND bbox.xmin <= $xmax"
            " AND bbox.ymax >= $ymin AND bbox.ymin <= $ymax"
        )
        return filter_sql, {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}

    # Crosses the antimeridian. Fold whichever side ran past +/-180 back
    # in-range, then OR an "east of the seam" box against a "west of the
    # seam" box instead of a single (now meaningless) xmin..xmax range.
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


def bbox_prune_literal_sql(bbox: tuple[float, float, float, float]) -> str:
    """bbox_filter_sql's intersection test with literal bounds, or "" at the seam.

    For call sites that build SQL where named parameters can't reach (the
    recreation layer's per-branch projection prune, whose whole point is
    pushing physical-column comparisons into a parquet scan). Shares this
    module's seam rule: a box that ran past [-180, 180] returns "" — skip
    the prune, stay correct, stay merely unpruned — rather than mis-pruning
    the wrapped side. Kept next to bbox_filter_sql so the two can't
    silently diverge on what "crosses the antimeridian" means.
    """
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    if not (xmin >= -180.0 and xmax <= 180.0):
        return ""
    return (
        f"bbox.xmax >= {xmin!r} AND bbox.xmin <= {xmax!r}"
        f" AND bbox.ymax >= {ymin!r} AND bbox.ymin <= {ymax!r}"
    )


@lru_cache(maxsize=16)
def geom_expr(glob: str, as_wkt: bool = False) -> str:
    """SQL expression yielding a GEOMETRY (or, if as_wkt, its WKT text) for glob's geometry column.

    Real Overture GeoParquet carries geo metadata, so DuckDB spatial reads
    `geometry` as a native GEOMETRY column directly; plain-parquet test
    fixtures store it as a raw WKB BLOB, needing ST_GeomFromWKB first.
    Detected once per (glob, as_wkt) pair and cached. A failed probe
    degrades to the WKB-BLOB assumption rather than raising — the real
    query hits the same problem and surfaces UpstreamUnavailable instead.
    """
    try:
        with db.conn_lock:
            described = db.shared_conn().execute(
                f"DESCRIBE SELECT geometry FROM read_parquet('{glob}') LIMIT 0"
            ).fetchone()
        type_name = described[1] if described else ""
    except duckdb.Error as e:
        logger.warning("Geometry type probe failed for %s: %s", glob, e)
        type_name = ""
    is_native = type_name.upper().startswith("GEOMETRY")
    if as_wkt:
        return "ST_AsText(geometry)" if is_native else "ST_AsText(ST_GeomFromWKB(geometry))"
    return "geometry" if is_native else "ST_GeomFromWKB(geometry)"
