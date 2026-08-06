"""DuckDB query layer over Overture Maps GeoParquet on S3.

Queries the public Overture bucket directly — no ETL, no database, no API key.
bbox column pushdown keeps remote scans to a handful of row groups.
"""

import math
from functools import lru_cache

import duckdb

OVERTURE_RELEASE = "2026-07-22.0"
S3_BASE = f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}"

# Every tool answer stays within this many rows so responses remain
# small enough for an agent's context window.
MAX_ROWS = 25


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
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


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
    xmin, ymin, xmax, ymax = _bbox_around(lat, lon, radius_m)
    filters = ["bbox.xmin >= ?", "bbox.ymin >= ?", "bbox.xmax <= ?", "bbox.ymax <= ?"]
    params: list = [xmin, ymin, xmax, ymax]
    if category:
        filters.append(
            "(basic_category ILIKE ? OR taxonomy.primary ILIKE ?"
            " OR list_contains(taxonomy.alternates, ?))"
        )
        params += [f"%{category}%", f"%{category}%", category]
    if name:
        filters.append("names.primary ILIKE ?")
        params.append(f"%{name}%")

    sql = f"""
        SELECT
            names.primary                       AS name,
            taxonomy.primary                    AS category,
            basic_category,
            operating_status,
            round(confidence, 2)                AS confidence,
            round(bbox.ymin, 6)                 AS lat,
            round(bbox.xmin, 6)                 AS lon,
            round(2 * 6371000 * asin(sqrt(
                pow(sin(radians(bbox.ymin - ?) / 2), 2)
                + cos(radians(?)) * cos(radians(bbox.ymin))
                * pow(sin(radians(bbox.xmin - ?) / 2), 2)
            )), 0)                              AS distance_m
        FROM read_parquet('{S3_BASE}/theme=places/type=place/*', hive_partitioning=1)
        WHERE {' AND '.join(filters)}
          AND names.primary IS NOT NULL
        ORDER BY distance_m
        LIMIT {limit}
    """
    rows = _conn().execute(sql, [lat, lat, lon, *params]).fetchall()
    cols = [
        "name", "category", "basic_category", "operating_status",
        "confidence", "lat", "lon", "distance_m",
    ]
    return [dict(zip(cols, r)) for r in rows if r[-1] is None or r[-1] <= radius_m]


def summarize_area(lat: float, lon: float, radius_m: float = 1000) -> dict:
    """Category mix for an area — an answer, not a data dump."""
    xmin, ymin, xmax, ymax = _bbox_around(lat, lon, radius_m)
    sql = f"""
        SELECT basic_category AS category, count(*) AS n
        FROM read_parquet('{S3_BASE}/theme=places/type=place/*', hive_partitioning=1)
        WHERE bbox.xmin >= ? AND bbox.ymin >= ? AND bbox.xmax <= ? AND bbox.ymax <= ?
          AND basic_category IS NOT NULL
        GROUP BY 1 ORDER BY n DESC LIMIT {MAX_ROWS}
    """
    rows = _conn().execute(sql, [xmin, ymin, xmax, ymax]).fetchall()
    return {
        "center": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "total_places": sum(n for _, n in rows),
        "top_categories": [{"category": c, "count": n} for c, n in rows],
    }
