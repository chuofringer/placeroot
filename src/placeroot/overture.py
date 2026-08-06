"""DuckDB query layer over Overture Maps GeoParquet on S3.

Queries the public Overture bucket directly — no ETL, no database, no API key.
bbox column pushdown keeps remote scans to a handful of row groups.
"""

import math
import os
from functools import lru_cache

import duckdb

OVERTURE_RELEASE = "2026-07-22.0"
S3_BASE = f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}"

# Every tool answer stays within this many rows so responses remain
# small enough for an agent's context window.
MAX_ROWS = 25

# Overrides the places dataset location — set by tests to point at a
# committed fixture instead of the live S3 release. Takes precedence over
# the PLACEROOT_DATA_PATH env var, which in turn overrides S3_BASE.
_data_path_override: str | None = None


def set_data_path(path: str | None) -> None:
    """Point the query layer at a local dataset instead of live S3.

    Pass None to restore the default (env var, then S3). Intended for tests.
    """
    global _data_path_override
    _data_path_override = path


def _places_glob() -> str:
    if _data_path_override is not None:
        return _data_path_override
    env_path = os.environ.get("PLACEROOT_DATA_PATH")
    if env_path:
        return env_path
    return f"{S3_BASE}/theme=places/type=place/*"


@lru_cache(maxsize=1)
def _conn() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")
    # Public bucket: anonymous access.
    con.execute("SET s3_access_key_id='';")
    con.execute("SET s3_secret_access_key='';")
    return con


def _bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Square bounding box guaranteed to contain the radius_m circle around (lat, lon).

    Not itself a radius filter — corners of the square reach out to
    radius_m * sqrt(2). Used only as a cheap row-group prefilter; the exact
    circle is enforced separately by the haversine predicate from
    area_geometry(). Longitude is not clamped/wrapped at the antimeridian:
    a search near lon=+/-180 produces an out-of-range box and will miss
    places on the other side of the seam.
    """
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


# Haversine great-circle distance in meters between (bbox.ymin, bbox.xmin)
# and a named point, taking three positional params: lat, lat, lon.
_DISTANCE_EXPR = """2 * 6371000 * asin(sqrt(
                pow(sin(radians(bbox.ymin - $lat) / 2), 2)
                + cos(radians($lat)) * cos(radians(bbox.ymin))
                * pow(sin(radians(bbox.xmin - $lon) / 2), 2)
            ))"""


def area_geometry(lat: float, lon: float, radius_m: float) -> tuple[str, str, dict]:
    """Shared "is this place within radius_m of (lat, lon)" predicate.

    Returns (bbox_filter_sql, distance_filter_sql, params) — the bbox filter
    is a cheap row-group prefilter using intersection semantics (so it
    doesn't drop non-point geometry the way full-containment would); the
    distance filter is the exact circle and is what actually decides
    membership. Both find_places and summarize_area use this so they agree
    on what's "in" an area. params is a dict of named parameters shared by
    both filters plus any additional query-specific ones the caller adds.
    """
    xmin, ymin, xmax, ymax = _bbox_around(lat, lon, radius_m)
    bbox_filter = (
        "bbox.xmax >= $xmin AND bbox.xmin <= $xmax"
        " AND bbox.ymax >= $ymin AND bbox.ymin <= $ymax"
    )
    distance_filter = f"{_DISTANCE_EXPR} <= $radius_m"
    params = {
        "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
        "lat": lat, "lon": lon, "radius_m": radius_m,
    }
    return bbox_filter, distance_filter, params


def find_places(
    lat: float,
    lon: float,
    radius_m: float = 1000,
    category: str | None = None,
    name: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Places near a point, nearest first, compact rows."""
    limit = min(limit, MAX_ROWS)
    bbox_filter, distance_filter, params = area_geometry(lat, lon, radius_m)
    filters = [bbox_filter, distance_filter, "names.primary IS NOT NULL"]
    if category:
        filters.append(
            "(basic_category ILIKE $category OR taxonomy.primary ILIKE $category"
            " OR list_contains(taxonomy.alternates, $category_exact))"
        )
        params["category"] = f"%{category}%"
        params["category_exact"] = category
    if name:
        filters.append("names.primary ILIKE $name")
        params["name"] = f"%{name}%"

    sql = f"""
        SELECT
            names.primary                       AS name,
            taxonomy.primary                    AS category,
            basic_category,
            operating_status,
            round(confidence, 2)                AS confidence,
            round(bbox.ymin, 6)                 AS lat,
            round(bbox.xmin, 6)                 AS lon,
            round({_DISTANCE_EXPR}, 0)          AS distance_m
        FROM read_parquet('{_places_glob()}', hive_partitioning=1)
        WHERE {' AND '.join(filters)}
        ORDER BY distance_m
        LIMIT {limit}
    """
    rows = _conn().execute(sql, params).fetchall()
    cols = [
        "name", "category", "basic_category", "operating_status",
        "confidence", "lat", "lon", "distance_m",
    ]
    return [dict(zip(cols, r)) for r in rows]


def summarize_area(lat: float, lon: float, radius_m: float = 1000) -> dict:
    """Category mix for an area — an answer, not a data dump."""
    bbox_filter, distance_filter, params = area_geometry(lat, lon, radius_m)
    sql = f"""
        SELECT
            basic_category AS category,
            count(*) AS n,
            sum(count(*)) OVER ()                                     AS total,
            sum(count(*)) FILTER (WHERE basic_category IS NULL) OVER () AS uncategorized
        FROM read_parquet('{_places_glob()}', hive_partitioning=1)
        WHERE {bbox_filter} AND {distance_filter}
        GROUP BY 1
        ORDER BY n DESC
    """
    rows = _conn().execute(sql, params).fetchall()
    total_places = rows[0][2] if rows else 0
    uncategorized_count = rows[0][3] if rows else 0
    categorized = [r for r in rows if r[0] is not None]
    top = categorized[:MAX_ROWS]
    other_categories_count = sum(n for _, n, _, _ in categorized[MAX_ROWS:])
    return {
        "center": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "total_places": total_places,
        "top_categories": [{"category": c, "count": n} for c, n, _, _ in top],
        "other_categories_count": other_categories_count,
        "uncategorized_count": uncategorized_count,
    }
