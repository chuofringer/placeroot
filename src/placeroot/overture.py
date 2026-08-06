"""DuckDB query layer over Overture Maps GeoParquet on S3.

Queries the public Overture bucket directly — no ETL, no database, no API key.
bbox column pushdown keeps remote scans to a handful of row groups. A local
tile cache (see cache.py) sits in front of repeat remote scans for the same
area, and the active release (see release.py) is discovered at runtime
instead of hardcoded.

Concurrency (issue #24): DuckDB connections aren't safe for concurrent
execute() calls from multiple threads, which HTTP transport mode makes
possible (stdio only ever has one request in flight). db.conn_lock
serializes every use of the shared connection — every call site in this
file (and in routing.py, divisions.py, buildings.py, and geocode.py, which
all now reuse the same connection via db.py — issue #40) acquires it
first. Background tile materialization (cache.py) is the only exception:
it always runs on its own connection via db.new_connection().

Connection setup, schema probing, and geometry helpers used to be
duplicated here and in routing.py (issue #40); both now live in db.py and
geo.py. _conn/_conn_lock/_probe_schema remain here as thin aliases
(deprecated in favor of importing db directly) so existing external
references — tests included — keep working unchanged.
"""

import logging
import math
import os

import duckdb

from placeroot import budget, cache, db, release
from placeroot.errors import (  # noqa: F401 - re-exported; see below
    SchemaDegraded,
    UpstreamUnavailable,
)
from placeroot.geo import bbox_around as _bbox_around  # noqa: F401 - back-compat alias, see below
from placeroot.geo import bbox_filter_sql as _bbox_filter_sql  # noqa: F401 - back-compat alias

logger = logging.getLogger(__name__)

# Every tool answer stays within this many rows so responses remain
# small enough for an agent's context window.
MAX_ROWS = 25

THEME = "places"

# Columns every tool depends on somewhere. bbox is the only one treated as
# essential (it's what the radius/distance math runs on) — the rest degrade
# gracefully: a tool still answers, just without that field, and callers can
# check degraded_fields() to note it. id (GERS, issue #25) and the place_details
# fields (addresses/websites/phones/socials/brand/sources, issue #9) degrade
# the same way: missing means that field comes back None/empty, not a failure.
REQUIRED_COLUMNS = [
    "names", "taxonomy", "basic_category", "operating_status", "confidence", "bbox",
    "id", "addresses", "websites", "phones", "socials", "brand", "sources",
]
ESSENTIAL_COLUMNS = {"bbox"}

# Overrides a theme's dataset location — set by tests to point at a
# committed fixture instead of the live S3 release. Takes precedence over
# the PLACEROOT_DATA_PATH[_<THEME>] env var, which in turn overrides the
# live release. Keyed by theme so divisions (#11) and geocode's own
# divisions/addresses queries (#10) can each be pointed at their own
# fixture independently of the places fixture.
#
# A single Overture theme can carry more than one `type` (divisions has
# both type=division, which geocode.py reads, and type=division_area,
# which divisions.py reads) — a bare theme key can't distinguish those, so
# an override may also be keyed on the more specific "theme:type_" string.
# _upstream_glob checks the specific key first and falls back to the bare
# theme key, so every existing call site that overrides by theme alone
# (divisions.py's admin_lookup tests included) keeps working unchanged.
_data_path_overrides: dict[str, str] = {}


def _override_key(theme: str, type_: str | None) -> str:
    return theme if type_ is None else f"{theme}:{type_}"


def set_data_path(path: str | None, theme: str = THEME, type_: str | None = None) -> None:
    """Point the query layer at a local dataset instead of live S3.

    Pass None to restore the default (env var, then discovered release) for
    that theme. Intended for tests. theme defaults to "places" for
    back-compat with existing callers that only ever queried places.

    type_ defaults to None, which overrides every type under that theme
    (what every existing caller — admin_lookup's tests included — wants:
    "divisions" only ever has one type in play for them). Pass type_
    explicitly only when a theme has more than one type active at once, as
    divisions now does (type=division for geocode.py, type=division_area
    for divisions.py) — that overrides just that type, leaving a bare-theme
    override (or the live default) in place for the others.
    """
    key = _override_key(theme, type_)
    if path is None:
        _data_path_overrides.pop(key, None)
    else:
        _data_path_overrides[key] = path


def _env_var_for_theme(theme: str) -> str:
    # Back-compat: places kept its original, unsuffixed env var name.
    return "PLACEROOT_DATA_PATH" if theme == THEME else f"PLACEROOT_DATA_PATH_{theme.upper()}"


# The public Overture bucket's release root. Issue #20: PLACEROOT_UPSTREAM_BASE
# swaps this for a mirror (our own bucket, or any other S3-compatible/local
# base) without touching the theme=/type=/release path structure below it, so
# an upstream layout change stays an inconvenience (re-point the env var) —
# never an outage. See scripts/mirror_theme.py (builds the mirror) and
# docs/MIRROR.md (the switchover runbook).
DEFAULT_UPSTREAM_BASE = "s3://overturemaps-us-west-2/release"


def _upstream_base() -> str:
    return os.environ.get("PLACEROOT_UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE).rstrip("/")


# Back-compat (issue #40): routing.py used to read its own, differently
# named env var for the transportation theme instead of following the
# PLACEROOT_DATA_PATH_<THEME> convention every other theme uses. Still
# honored — as a fallback, checked only when the standard name isn't set —
# so any deployment already setting it keeps working unchanged.
_TRANSPORTATION_LEGACY_ENV_VAR = "PLACEROOT_TRANSPORTATION_DATA_PATH"


def _upstream_glob(theme: str = THEME, type_: str = "place") -> str:
    specific_key = _override_key(theme, type_)
    if specific_key in _data_path_overrides:
        return _data_path_overrides[specific_key]
    if theme in _data_path_overrides:
        return _data_path_overrides[theme]
    env_path = os.environ.get(_env_var_for_theme(theme))
    if not env_path and theme == "transportation":
        env_path = os.environ.get(_TRANSPORTATION_LEGACY_ENV_VAR)
    if env_path:
        return env_path
    active_release = release.resolve_release()
    return f"{_upstream_base()}/{active_release}/theme={theme}/type={type_}/*"


def conn() -> duckdb.DuckDBPyConnection:
    """The shared DuckDB connection, for other modules (e.g. geocode.py) that
    query themes beyond places but want the same httpfs setup and warm cache.
    Callers must hold _conn_lock around any query they run against it."""
    return db.shared_conn()


def probe_schema(glob: str) -> frozenset | None:
    """Public wrapper over the schema probe, for callers outside overture.py."""
    return db.probe_schema(glob)


def upstream_glob(theme: str = THEME, type_: str = "place") -> str:
    """Public wrapper: the resolved glob/path for theme (fixture override, env, or live S3).

    type_ has no per-theme default beyond "place" — callers querying a
    theme other than places (geocode.py's divisions/addresses queries,
    divisions.py's division_area queries) must pass their own type_
    explicitly, the same way divisions.py already does.
    """
    return _upstream_glob(theme, type_)


# Deprecated: import db directly instead. Kept as thin aliases so existing
# external references (tests included) keep working unchanged — see this
# module's docstring and db.py's.
_conn_lock = db.conn_lock
_conn = db.shared_conn
_new_connection = db.new_connection
_probe_schema = db.probe_schema



def warm_metadata() -> None:
    """Best-effort: touch the shared connection's parquet metadata cache for
    the active upstream dataset (issue #31), so the first real query doesn't
    pay the cold footer-read cost alone. Meant to run on a background thread
    at startup; failures are logged and swallowed, never raised.
    """
    upstream = _upstream_glob()
    try:
        with db.conn_lock:
            db.shared_conn().execute(f"SELECT * FROM read_parquet('{upstream}') LIMIT 0")
    except duckdb.Error as e:
        logger.warning("Metadata pre-warm failed for %s (continuing): %s", upstream, e)


def missing_columns(glob: str, required: list[str] = REQUIRED_COLUMNS) -> list[str]:
    """`required` columns not present in glob's schema. Empty if the probe failed.

    required defaults to the places REQUIRED_COLUMNS; other themes (e.g.
    divisions, see divisions.py) pass their own list.
    """
    present = _probe_schema(glob)
    if present is None:
        return []
    return [c for c in required if c not in present]


def degraded_fields() -> list[str]:
    """Non-essential REQUIRED_COLUMNS missing from the currently active places dataset."""
    return [c for c in missing_columns(_upstream_glob()) if c not in ESSENTIAL_COLUMNS]


def _check_schema(
    glob: str, required: list[str] = REQUIRED_COLUMNS, essential: set[str] = ESSENTIAL_COLUMNS
) -> list[str]:
    """Missing columns for glob, raising SchemaDegraded if any are essential."""
    missing = missing_columns(glob, required)
    essential_missing = [c for c in missing if c in essential]
    if essential_missing:
        raise SchemaDegraded(essential_missing)
    return missing


def _from_source(bbox: tuple[float, float, float, float], theme: str = THEME) -> str:
    """SQL FROM-clause source for a places query: local cache tiles, or upstream."""
    upstream = _upstream_glob(theme)
    if cache.enabled():
        try:
            with db.conn_lock:
                paths = cache.local_paths_for_query(
                    db.shared_conn(), release.resolve_release(), theme, bbox, upstream,
                    db.new_connection,
                )
        except duckdb.Error as e:
            raise UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


# Haversine great-circle distance in meters between (bbox.ymin, bbox.xmin)
# and a named point, using named params $lat and $lon. Public: geocode.py
# reuses this same distance expression for divisions/addresses nearest-point
# queries so all themes agree on what "distance" means.
DISTANCE_EXPR = """2 * 6371000 * asin(sqrt(
                pow(sin(radians(bbox.ymin - $lat) / 2), 2)
                + cos(radians($lat)) * cos(radians(bbox.ymin))
                * pow(sin(radians(bbox.xmin - $lon) / 2), 2)
            ))"""
_DISTANCE_EXPR = DISTANCE_EXPR  # noqa: N816 - kept as an alias for existing in-module uses


def area_geometry(
    lat: float, lon: float, radius_m: float
) -> tuple[str, str, dict, tuple[float, float, float, float]]:
    """Shared "is this place within radius_m of (lat, lon)" predicate.

    Returns (bbox_filter_sql, distance_filter_sql, params, bbox) — the bbox
    filter is a cheap row-group prefilter using intersection semantics (so
    it doesn't drop non-point geometry the way full-containment would); the
    distance filter is the exact circle (already antimeridian-safe: it's
    built from sin/cos, which are periodic, so a raw longitude difference
    across the seam still comes out small) and is what actually decides
    membership. Both find_places and summarize_area use this so they agree
    on what's "in" an area. params is a dict of named parameters shared by
    both filters plus any additional query-specific ones the caller adds.

    bbox is the raw (xmin, ymin, xmax, ymax) box from _bbox_around — pass it
    straight to _from_source()/cache lookups, not the bbox_filter's own
    params: near the antimeridian those diverge (see _bbox_filter_sql), and
    it's this raw, possibly out-of-range box that cache.tiles_for_bbox()
    needs to enumerate tiles on both sides of the seam.
    """
    xmin, ymin, xmax, ymax = _bbox_around(lat, lon, radius_m)
    bbox_filter, bbox_params = _bbox_filter_sql(xmin, ymin, xmax, ymax)
    distance_filter = f"{_DISTANCE_EXPR} <= $radius_m"
    params = {**bbox_params, "lat": lat, "lon": lon, "radius_m": radius_m}
    return bbox_filter, distance_filter, params, (xmin, ymin, xmax, ymax)


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
    bbox_filter, distance_filter, params, bbox = area_geometry(lat, lon, radius_m)
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
    id_expr = "NULL" if "id" in missing else "id"

    sql = f"""
        SELECT
            {id_expr}                           AS id,
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
        "id", "name", "category", "basic_category", "operating_status",
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
    bbox_filter, distance_filter, params, bbox = area_geometry(lat, lon, radius_m)
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


# Array-valued fields on a place row that can be long enough to blow the
# token budget on their own (issue #9) — truncated via budget.truncate_list,
# never dropped silently. Kept small and explicit rather than derived, since
# it's the set place_details actually selects below.
_PLACE_DETAIL_LIST_FIELDS = ("addresses", "websites", "phones", "socials", "sources")

# place_details' own default search radius when resolving by name + lat/lon
# instead of by GERS id — a small "did you mean the place right here" window,
# not a general-purpose area search.
DEFAULT_DETAILS_RADIUS_M = 200

# Issue #41: a location hint (near_lat/near_lon) on an id lookup gets the same
# bbox prefilter treatment as any other query, just with a much wider box —
# the caller usually only knows roughly where the place was found (e.g. a
# find_places row's lat/lon), not that it's within DEFAULT_DETAILS_RADIUS_M.
# 50km comfortably covers "same metro area" while still pruning row groups
# for anything but a very dense, very large release.
ID_HINT_RADIUS_M = 50_000

# (source column that gates this field via `missing`, SQL expression, result alias)
_PLACE_DETAIL_COLUMNS = [
    ("id", "id", "id"),
    ("names", "names.primary", "name"),
    ("taxonomy", "taxonomy.primary", "category"),
    ("basic_category", "basic_category", "basic_category"),
    ("operating_status", "operating_status", "operating_status"),
    ("confidence", "round(confidence, 2)", "confidence"),
    ("brand", "brand.names.primary", "brand"),
    ("addresses", "addresses", "addresses"),
    ("websites", "websites", "websites"),
    ("phones", "phones", "phones"),
    ("socials", "socials", "socials"),
    ("sources", "sources", "sources"),
]
_PLACE_DETAIL_RESULT_COLS = [alias for _, _, alias in _PLACE_DETAIL_COLUMNS] + ["lat", "lon"]


def _place_details_sql(
    from_source: str, filters: list[str], order_by: str, missing: set[str]
) -> str:
    """The shared place_details SELECT, sourced from from_source.

    Column list and result shape stay identical no matter which of
    place_details' several lookup strategies (cached-tile, hint-constrained,
    full-scan, or name+point) supplies from_source/filters — only the FROM
    and WHERE differ between them.
    """
    select_list = ",\n            ".join(
        f'{"NULL" if col in missing else expr} AS {alias}'
        for col, expr, alias in _PLACE_DETAIL_COLUMNS
    )
    return f"""
        SELECT
            {select_list},
            round(bbox.ymin, 6) AS lat,
            round(bbox.xmin, 6) AS lon
        FROM {from_source}
        WHERE {" AND ".join(filters)}
        ORDER BY {order_by}
        LIMIT 1
    """


def _run_place_details_query(from_source: str, filters: list[str], order_by: str,
                              params: dict, missing: set[str]) -> tuple | None:
    sql = _place_details_sql(from_source, filters, order_by, missing)
    try:
        with _conn_lock:
            return _conn().execute(sql, params).fetchone()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e


def _place_details_by_id(id: str, near_lat: float | None, near_lon: float | None,
                          upstream: str, missing: set[str]) -> tuple | None:
    """Resolve a GERS id, cheapest source first (issue #41).

    1. Whatever tiles the local cache already has on disk — no upstream
       contact at all. Covers the common agent flow (find_places, whose
       results already warmed the relevant tiles, followed by
       place_details on one of those results).
    2. If a location hint was given, upstream constrained by a 50km bbox
       around it — row-group pruning applies, and a hit here also
       materializes/reuses the matching cache tile(s) via _from_source.
    3. Full-dataset scan, last resort — logged as a warning so an agent
       calling place_details(id=...) without a hint (or with a hint that
       missed) is visible in the logs as the slow path it is.
    """
    if cache.enabled():
        tile_paths = cache.cached_tile_paths(release.resolve_release(), THEME)
        if tile_paths:
            joined = ", ".join(f"'{p}'" for p in tile_paths)
            row = _run_place_details_query(
                f"read_parquet([{joined}])", ["id = $id"], "1", {"id": id}, missing
            )
            if row is not None:
                return row

    if near_lat is not None and near_lon is not None:
        xmin, ymin, xmax, ymax = _bbox_around(near_lat, near_lon, ID_HINT_RADIUS_M)
        bbox_filter, bbox_params = _bbox_filter_sql(xmin, ymin, xmax, ymax)
        params = {"id": id, **bbox_params}
        from_source = _from_source((xmin, ymin, xmax, ymax))
        row = _run_place_details_query(from_source, ["id = $id", bbox_filter], "1", params, missing)
        if row is not None:
            return row

    logger.warning(
        "place_details(id=%s) fell back to a full-dataset scan (no cache hit, "
        "no near_lat/near_lon hint given, or the hint missed) — issue #41", id,
    )
    return _run_place_details_query(
        f"read_parquet('{upstream}', hive_partitioning=1)", ["id = $id"], "1", {"id": id}, missing
    )


def place_details(
    id: str | None = None,
    name: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = DEFAULT_DETAILS_RADIUS_M,
    near_lat: float | None = None,
    near_lon: float | None = None,
) -> dict | None:
    """One place, in full: resolved by GERS id, or by name + a nearby point.

    Exactly one resolution strategy applies: pass id, or pass name together
    with lat/lon (nearest name match within radius_m wins). Raises ValueError
    if neither is given. Returns None if no place matches — callers (the
    server tool) turn that into a structured {"error": "not_found", ...}.

    near_lat/near_lon (issue #41) are an optional location hint for the id
    path only — pass the lat/lon of the row the id came from (e.g. a
    find_places result) to constrain the id lookup with a 50km bbox
    prefilter instead of scanning the whole dataset. Ignored when id isn't
    given. Bare id lookups (no hint) still work exactly as before; they just
    check the local tile cache first and fall back to a full scan, logged,
    if that misses too.

    Raises SchemaDegraded if bbox is missing (needed for the name+point
    path; an id lookup doesn't strictly need it, but the schema probe
    doesn't distinguish the two calls) or UpstreamUnavailable if the remote
    scan fails after retries. Non-essential columns (addresses, websites,
    phones, socials, brand, sources, confidence, ...) missing from the
    dataset come back as None — see degraded_fields().
    """
    if not id and not (name and lat is not None and lon is not None):
        raise ValueError("place_details requires id, or name together with lat and lon")

    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))

    if id:
        row = _place_details_by_id(id, near_lat, near_lon, upstream, missing)
    else:
        bbox_filter, distance_filter, params, bbox = area_geometry(lat, lon, radius_m)
        filters = [bbox_filter, distance_filter]
        if "names" not in missing:
            filters.append("names.primary ILIKE $name")
            params["name"] = f"%{name}%"
        from_source = _from_source(bbox)
        row = _run_place_details_query(from_source, filters, _DISTANCE_EXPR, params, missing)

    if row is None:
        return None
    result = dict(zip(_PLACE_DETAIL_RESULT_COLS, row))
    for field in _PLACE_DETAIL_LIST_FIELDS:
        kept, omitted = budget.truncate_list(result[field])
        result[field] = kept
        if omitted:
            result[f"{field}_omitted_count"] = omitted
    return result


def within_distance(
    lat: float,
    lon: float,
    max_distance_m: float,
    category: str | None = None,
    name: str | None = None,
) -> dict:
    """Is the nearest matching place within max_distance_m of (lat, lon)?

    category/name narrow the search the same way as find_places. The search
    window is capped at max_distance_m * 2 so a "nothing nearby" answer
    doesn't degrade into an unbounded scan — a real nearest match beyond
    that window comes back as nearest: None rather than its true distance.
    Raises the same SchemaDegraded/UpstreamUnavailable as find_places.
    """
    search_radius_m = max_distance_m * 2
    rows = find_places(lat, lon, search_radius_m, category=category, name=name, limit=1)
    if not rows:
        return {"within": False, "nearest": None, "distance_m": None}
    nearest = rows[0]
    return {
        "within": nearest["distance_m"] <= max_distance_m,
        "nearest": nearest,
        "distance_m": nearest["distance_m"],
    }


def compare_areas(areas: list[tuple[float, float]], radius_m: float = 1000) -> dict:
    """Side-by-side category mix for 2-5 area centers sharing one radius_m.

    Reuses summarize_area per area, then aligns counts across areas for the
    top ~10 categories by combined count, adds place density per km^2 per
    area, and flags the categories with the largest relative difference
    between areas ("differentiators") — the most useful signal for "how is
    area A different from area B."

    Raises ValueError if areas isn't 2-5 centers. Propagates
    SchemaDegraded/UpstreamUnavailable from the first area whose
    summarize_area call fails — a partial comparison isn't returned.
    """
    if not 2 <= len(areas) <= 5:
        raise ValueError("compare_areas takes between 2 and 5 area centers")

    summaries = [summarize_area(lat, lon, radius_m) for lat, lon in areas]

    combined_counts: dict[str, int] = {}
    for s in summaries:
        for row in s["top_categories"]:
            cat, n = row["category"], row["count"]
            combined_counts[cat] = combined_counts.get(cat, 0) + n
    top_categories = sorted(combined_counts, key=lambda c: combined_counts[c], reverse=True)[:10]

    area_km2 = math.pi * (radius_m / 1000) ** 2
    per_area = []
    for (lat, lon), s in zip(areas, summaries):
        counts = {row["category"]: row["count"] for row in s["top_categories"]}
        per_area.append({
            "center": {"lat": lat, "lon": lon},
            "total_places": s["total_places"],
            "density_per_km2": round(s["total_places"] / area_km2, 2) if area_km2 else 0,
            "category_counts": {c: counts.get(c, 0) for c in top_categories},
        })

    differentiators = []
    for c in top_categories:
        values = [a["category_counts"][c] for a in per_area]
        lo, hi = min(values), max(values)
        differentiators.append({
            "category": c,
            "min_count": lo,
            "max_count": hi,
            "relative_difference": round((hi - lo) / hi, 2) if hi else 0,
        })
    differentiators.sort(key=lambda d: d["relative_difference"], reverse=True)

    return {
        "areas": per_area,
        "categories": top_categories,
        "differentiators": differentiators,
    }
