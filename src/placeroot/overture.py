"""DuckDB query layer over Overture Maps GeoParquet on S3.

Queries the public Overture bucket directly — no ETL, no database, no API key.
bbox column pushdown keeps remote scans to a handful of row groups. A local
tile cache (see cache.py) sits in front of repeat remote scans for the same
area, and the active release (see release.py) is discovered at runtime
instead of hardcoded.
"""

import logging
import math
import os
import threading
from functools import lru_cache

import duckdb

from placeroot import cache, release

logger = logging.getLogger(__name__)

# Every tool answer stays within this many rows so responses remain
# small enough for an agent's context window.
MAX_ROWS = 25

THEME = "places"

# Columns every tool depends on somewhere. bbox is the only one treated as
# essential (it's what the radius/distance math runs on) — the rest degrade
# gracefully: a tool still answers, just without that field, and callers can
# check degraded_fields() to note it.
REQUIRED_COLUMNS = ["names", "taxonomy", "basic_category", "operating_status", "confidence", "bbox"]
ESSENTIAL_COLUMNS = {"bbox"}

# Overrides the places dataset location — set by tests to point at a
# committed fixture instead of the live S3 release. Takes precedence over
# the PLACEROOT_DATA_PATH env var, which in turn overrides the live release.
_data_path_override: str | None = None


class UpstreamUnavailable(Exception):
    """A remote scan failed after DuckDB's built-in retries were exhausted.

    detail is a short, agent-safe message — never a raw stack trace.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SchemaDegraded(Exception):
    """An essential column is missing from the active dataset."""

    def __init__(self, missing: list[str]):
        detail = f"required columns missing from dataset: {', '.join(missing)}"
        super().__init__(detail)
        self.detail = detail
        self.missing = missing


def set_data_path(path: str | None) -> None:
    """Point the query layer at a local dataset instead of live S3.

    Pass None to restore the default (env var, then discovered release).
    Intended for tests.
    """
    global _data_path_override
    _data_path_override = path


def _upstream_glob() -> str:
    if _data_path_override is not None:
        return _data_path_override
    env_path = os.environ.get("PLACEROOT_DATA_PATH")
    if env_path:
        return env_path
    active_release = release.resolve_release()
    return f"s3://overturemaps-us-west-2/release/{active_release}/theme={THEME}/type=place/*"


# Guards every use of the shared _conn() connection. DuckDB connections
# aren't safe for concurrent use, and the MCP stdio server normally
# processes one tool call at a time anyway — this lock formalizes that and
# makes it safe for the startup metadata pre-warm (issue #31) to share the
# same connection object from a background thread without racing a real
# query. Background tile materialization (cache.py) deliberately does NOT
# use this lock: it always runs on its own connection via _new_connection(),
# never touching the shared one.
_conn_lock = threading.Lock()


def _configure(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")
    # Public bucket: anonymous access.
    con.execute("SET s3_access_key_id='';")
    con.execute("SET s3_secret_access_key='';")
    # Caches parquet footer/metadata per connection (issue #31): the cold
    # cost DuckDB pays reading a remote file's footer is ~5s of the ~8s cold
    # query; this makes a second query against the same file on the same
    # connection skip that cost. Combined with the startup pre-warm below,
    # the shared connection often has this paid for before a real query
    # ever arrives.
    con.execute("SET enable_object_cache=true;")
    # Bounded timeout + retry on remote scans (issue #5): DuckDB's httpfs
    # extension applies these to every S3/HTTP request it makes, so a slow
    # or down upstream fails fast instead of hanging a tool call.
    try:
        con.execute("SET http_timeout=5000;")  # ms
        con.execute("SET http_retries=2;")
        con.execute("SET http_retry_wait_ms=200;")
        con.execute("SET http_retry_backoff=2;")
    except duckdb.Error as e:
        logger.warning("Could not set httpfs timeout/retry options: %s", e)
    return con


@lru_cache(maxsize=1)
def _conn() -> duckdb.DuckDBPyConnection:
    return _configure(duckdb.connect())


def _new_connection() -> duckdb.DuckDBPyConnection:
    """A fresh, independently-configured connection for background work.

    Background tile materialization must not share _conn() with whatever
    query is running on the main thread — DuckDB connections aren't safe
    for concurrent use. Pass this (uncalled) as cache.py's connection
    factory so it can create one per background fetch.
    """
    return _configure(duckdb.connect())


def warm_metadata() -> None:
    """Best-effort: touch the shared connection's parquet metadata cache for
    the active upstream dataset (issue #31), so the first real query doesn't
    pay the cold footer-read cost alone. Meant to run on a background thread
    at startup; failures are logged and swallowed, never raised.
    """
    upstream = _upstream_glob()
    try:
        with _conn_lock:
            _conn().execute(f"SELECT * FROM read_parquet('{upstream}') LIMIT 0")
    except duckdb.Error as e:
        logger.warning("Metadata pre-warm failed for %s (continuing): %s", upstream, e)


@lru_cache(maxsize=8)
def _probe_schema(glob: str) -> frozenset | None:
    """Column names present in glob's dataset, or None if the probe itself failed.

    A failed probe (upstream down, glob unreadable) is treated as "unknown,
    assume nothing missing" — the actual query below will hit the same
    problem and surface it as UpstreamUnavailable instead.
    """
    try:
        with _conn_lock:
            cols = _conn().execute(f"SELECT * FROM read_parquet('{glob}') LIMIT 0").description
        return frozenset(c[0] for c in cols)
    except duckdb.Error as e:
        logger.warning("Schema probe failed for %s: %s", glob, e)
        return None


def missing_columns(glob: str) -> list[str]:
    """REQUIRED_COLUMNS not present in glob's schema. Empty if the probe failed."""
    present = _probe_schema(glob)
    if present is None:
        return []
    return [c for c in REQUIRED_COLUMNS if c not in present]


def degraded_fields() -> list[str]:
    """Non-essential REQUIRED_COLUMNS missing from the currently active dataset."""
    return [c for c in missing_columns(_upstream_glob()) if c not in ESSENTIAL_COLUMNS]


def _check_schema(glob: str) -> list[str]:
    """Missing columns for glob, raising SchemaDegraded if any are essential."""
    missing = missing_columns(glob)
    essential_missing = [c for c in missing if c in ESSENTIAL_COLUMNS]
    if essential_missing:
        raise SchemaDegraded(essential_missing)
    return missing


def _from_source(bbox: tuple[float, float, float, float]) -> str:
    """SQL FROM-clause source for a places query: local cache tiles, or upstream."""
    upstream = _upstream_glob()
    if cache.enabled():
        try:
            with _conn_lock:
                paths = cache.local_paths_for_query(
                    _conn(), release.resolve_release(), THEME, bbox, upstream, _new_connection
                )
        except duckdb.Error as e:
            raise UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


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
# and a named point, using named params $lat and $lon.
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
    """Places near a point, nearest first, compact rows.

    Raises SchemaDegraded if bbox is missing from the active dataset (the
    tool can't answer at all without it), or UpstreamUnavailable if the
    remote scan fails after retries. Non-essential columns missing from the
    dataset come back as None in their field — see degraded_fields().
    """
    limit = min(limit, MAX_ROWS)
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    bbox_filter, distance_filter, params = area_geometry(lat, lon, radius_m)
    bbox = (params["xmin"], params["ymin"], params["xmax"], params["ymax"])
    filters = [bbox_filter, distance_filter]
    if "names" not in missing:
        filters.append("names.primary IS NOT NULL")
    if category:
        cat_clauses = []
        if "basic_category" not in missing:
            cat_clauses.append("basic_category ILIKE $category")
        if "taxonomy" not in missing:
            cat_clauses.append(
                "(taxonomy.primary ILIKE $category"
                " OR list_contains(taxonomy.alternates, $category_exact))"
            )
        if cat_clauses:
            filters.append(f"({' OR '.join(cat_clauses)})")
            params["category"] = f"%{category}%"
            params["category_exact"] = category
    if name:
        if "names" not in missing:
            filters.append("names.primary ILIKE $name")
            params["name"] = f"%{name}%"

    name_expr = "NULL" if "names" in missing else "names.primary"
    category_expr = "NULL" if "taxonomy" in missing else "taxonomy.primary"
    basic_category_expr = "NULL" if "basic_category" in missing else "basic_category"
    operating_status_expr = "NULL" if "operating_status" in missing else "operating_status"
    confidence_expr = "NULL" if "confidence" in missing else "round(confidence, 2)"

    sql = f"""
        SELECT
            {name_expr}                         AS name,
            {category_expr}                     AS category,
            {basic_category_expr}               AS basic_category,
            {operating_status_expr}             AS operating_status,
            {confidence_expr}                   AS confidence,
            round(bbox.ymin, 6)                 AS lat,
            round(bbox.xmin, 6)                 AS lon,
            round({_DISTANCE_EXPR}, 0)          AS distance_m
        FROM {_from_source(bbox)}
        WHERE {' AND '.join(filters)}
        ORDER BY distance_m
        LIMIT {limit}
    """
    try:
        with _conn_lock:
            rows = _conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    cols = [
        "name", "category", "basic_category", "operating_status",
        "confidence", "lat", "lon", "distance_m",
    ]
    return [dict(zip(cols, r)) for r in rows]


def summarize_area(lat: float, lon: float, radius_m: float = 1000) -> dict:
    """Category mix for an area — an answer, not a data dump.

    Raises SchemaDegraded if bbox is missing, or UpstreamUnavailable if the
    remote scan fails after retries. If basic_category is missing, every
    place counts as uncategorized (top_categories comes back empty) rather
    than failing the call — see degraded_fields().
    """
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    bbox_filter, distance_filter, params = area_geometry(lat, lon, radius_m)
    bbox = (params["xmin"], params["ymin"], params["xmax"], params["ymax"])
    category_expr = "NULL" if "basic_category" in missing else "basic_category"
    sql = f"""
        SELECT
            {category_expr} AS category,
            count(*) AS n,
            sum(count(*)) OVER ()                                     AS total,
            coalesce(sum(count(*)) FILTER (WHERE {category_expr} IS NULL) OVER (), 0)
                                                                      AS uncategorized
        FROM {_from_source(bbox)}
        WHERE {bbox_filter} AND {distance_filter}
        GROUP BY 1
        ORDER BY n DESC
    """
    try:
        with _conn_lock:
            rows = _conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
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
