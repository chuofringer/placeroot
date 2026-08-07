"""PlaceRoot MCP server — ground AI agents in open map data.

Run (stdio, the default): uv run placeroot
Run (streamable-HTTP): uv run placeroot --http [--host 127.0.0.1] [--port 8321]

HTTP mode uses the mcp SDK's first-party streamable-HTTP transport
(MCPServer.run(transport="streamable-http", ...), backed by uvicorn — no
hand-rolled protocol bridge) and can serve multiple requests concurrently.
See overture.py's module docstring for how the shared query connection
stays safe under that concurrency.
"""

import argparse
import logging
import os
import threading

from mcp.server.mcpserver import MCPServer

from placeroot import (
    budget,
    buildings,
    cache,
    divisions,
    mapview,
    overture,
    release,
    routing,
    simplify,
)
from placeroot import geocode as geocoding

logger = logging.getLogger(__name__)

BASE_INSTRUCTIONS = (
    "Grounds spatial questions in Overture Maps open data. "
    "Answers are compact and ranked; distances in meters. Responses "
    "that don't fit the token budget carry truncated: true — narrow "
    "the query (smaller radius, a category or name filter) instead of "
    "raising limit."
)

mcp = MCPServer("placeroot", instructions=BASE_INSTRUCTIONS)

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8321


def _upstream_error(e: Exception) -> dict:
    """Structured, agent-readable error for a failed remote scan — never a raw traceback."""
    return {"error": "upstream_unavailable", "detail": e.detail, "retry_advised": True}


def _schema_error(e: overture.SchemaDegraded) -> dict:
    return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}


def _with_degraded_fields(result: dict) -> dict:
    degraded = overture.degraded_fields()
    if degraded:
        result["degraded_fields"] = degraded
    return result


def _with_buildings_degraded_fields(result: dict) -> dict:
    degraded = buildings.degraded_fields()
    if degraded:
        result["degraded_fields"] = degraded
    return result


def _with_category_hint(payload: dict, category: str | None, widen_hint: str) -> dict:
    """Add a non-fatal "note" when a category filter matched nothing (#117).

    find_places matches category by substring, so a wrong or invalid
    Overture slug just returns zero rows — indistinguishable from "this
    area really has none". The note points at search_categories and at
    whichever widening move fits the mode the caller used (a bigger
    radius for the point path, a bigger division for the polygon path).
    """
    if category and not payload.get("results"):
        payload["note"] = (
            f"no places matched category '{category}' here; if that may not be a "
            "valid Overture category slug, use search_categories to find the right "
            f"one, or {widen_hint} / drop the category filter."
        )
    return payload


@mcp.tool()
def find_places(
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = 1000,
    category: str | None = None,
    name: str | None = None,
    min_confidence: float | None = None,
    operating_status: str | None = None,
    limit: int = 10,
    brand: str | None = None,
    has_website: bool | None = None,
    has_phone: bool | None = None,
    division_id: str | None = None,
) -> dict:
    """Find named places, either near a point or inside a division's boundary.

    Two mutually exclusive modes:
    - Point + radius: pass lat and lon (radius_m defaults to 1000m).
      Results are nearest-first, within a circle around (lat, lon).
    - Division polygon: pass division_id (a GERS id, e.g. from admin_lookup's
      chain) instead of lat/lon. Results are every matching place whose
      point falls inside that division's true boundary polygon — no radius
      to guess, and no circle clipping a coastline or straddling a border.
      Results are ordered by name (there's no reference point to rank
      distance from).

    category matches Overture's taxonomy (e.g. 'coffee_shop', 'restaurant',
    'grocery'); name is a substring match on the place name — both compose
    with either mode. Results include operating_status ("in business" /
    "permanently closed" / null when unknown) — a business-lifecycle
    signal, NOT opening hours; this data has no open-now information.

    min_confidence (0.0-1.0) keeps only rows whose confidence score is at
    least that value; out-of-range values return a bad_request error.
    operating_status filters to a single status, accepting either the
    relabeled value ("in business", "permanently closed", "temporarily
    closed") or the raw Overture value ("open", "closed",
    "closed_permanently", "closed_temporarily"), matched case-insensitively
    — "permanently closed"/"closed" also match Overture's separate
    "closed_permanently" raw value, since both relabel the same way.
    Unrecognized values return a bad_request error.

    brand is a substring match on the place's brand name (e.g. 'Starbucks').
    Brand data is sparse — most independent businesses have no brand at all,
    so brand=X narrows results down to that chain only; the absence of a
    result does NOT mean "not a Starbucks", it may just mean brand isn't
    populated for that place. has_website/has_phone filter on whether a
    place has any website/phone entries at all (presence, not content) —
    each result row carries brand (string or null) and has_website/has_phone
    (booleans) so an agent can see why a place matched, but the full
    websites/phones arrays are only returned by place_details.

    Every filter above composes with either mode, and each is a silent
    no-op (not an error) if the column it needs (confidence /
    operating_status / brand / websites / phones) is absent from the active
    dataset — see degraded_fields() on the response.
    Returns {"results": [...]}, plus truncated/omitted_count if the answer
    didn't fit the token budget. Returns a structured {"error": "bad_request",
    ...} if neither mode's inputs are given (or both are), {"error":
    "not_found", ...} if division_id doesn't match any known division, or
    a structured {"error": ...} if the upstream dataset is unavailable or
    missing columns this tool depends on. If a category filter was given and
    it matched nothing (in either mode), a non-fatal "note" field hints that
    the category slug may be wrong and points at search_categories.
    """
    point_given = lat is not None or lon is not None
    if division_id is not None and point_given:
        return {
            "error": "bad_request",
            "detail": "pass either lat/lon (+radius_m) or division_id, not both",
        }
    if division_id is None and not point_given:
        return {
            "error": "bad_request",
            "detail": "pass either lat/lon (+radius_m) or division_id",
        }
    if division_id is not None:
        try:
            rows = overture.find_places_in_division(
                division_id, category, name,
                min_confidence, operating_status, brand, has_website, has_phone, limit,
            )
        except ValueError as e:
            return {"error": "bad_request", "detail": str(e)}
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        except overture.SchemaDegraded as e:
            return _schema_error(e)
        if rows is None:
            return {"error": "not_found", "detail": "no division matched division_id"}
        payload = _with_degraded_fields(budget.apply_budget({"results": rows}, "results"))
        return _with_category_hint(payload, category, widen_hint="try a larger division")

    if lat is None or lon is None:
        return {"error": "bad_request", "detail": "pass both lat and lon"}
    try:
        rows = overture.find_places(
            lat, lon, radius_m, category, name,
            min_confidence, operating_status, brand, has_website, has_phone, limit,
        )
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    payload = _with_degraded_fields(budget.apply_budget({"results": rows}, "results"))
    return _with_category_hint(payload, category, widen_hint="widen radius_m")


@mcp.tool()
def summarize_area(lat: float, lon: float, radius_m: float = 1000) -> dict:
    """Summarize what's in an area: total places and top categories.

    Returns a structured {"error": ...} instead of raising if upstream is
    unavailable or the dataset is missing columns this tool depends on.
    """
    try:
        result = overture.summarize_area(lat, lon, radius_m)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_degraded_fields(budget.apply_budget(result, "top_categories"))


@mcp.tool()
def place_details(
    id: str | None = None,
    name: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = overture.DEFAULT_DETAILS_RADIUS_M,
    near_lat: float | None = None,
    near_lon: float | None = None,
) -> dict:
    """One place, in full: addresses, websites, phones, socials, brand,
    source attribution, GERS id, confidence, and operating status.

    Resolve either by GERS id (the `id` field find_places and other tools
    return) or by name + lat/lon (nearest name match within radius_m of
    that point). Pass id, or pass name together with lat and lon — not
    both. Long array fields (addresses, websites, phones, socials, sources)
    are capped and never silently dropped: a truncated field carries a
    matching "<field>_omitted_count". Returns {"error": "not_found", ...}
    if nothing matches, or a structured {"error": ...} if the upstream
    dataset is unavailable or missing columns this tool depends on.

    When looking up by id, also pass near_lat/near_lon — the lat/lon from
    the find_places (or other tool) row the id came from — so the lookup
    can be narrowed to a ~50km box instead of scanning the whole dataset.
    Ignored when resolving by name. Omitting it still works, just slower on
    a cold, uncached id.
    """
    try:
        result = overture.place_details(id, name, lat, lon, radius_m, near_lat, near_lon)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    if result is None:
        return {"error": "not_found", "detail": "no place matched id, or name near lat/lon"}
    return _with_degraded_fields(result)


@mcp.tool()
def within_distance(
    lat: float,
    lon: float,
    max_distance_m: float,
    category: str | None = None,
    name: str | None = None,
) -> dict:
    """Is the nearest place matching category/name within max_distance_m of (lat, lon)?

    Returns {"within": bool, "nearest": {...place row with id...} | None,
    "distance_m": float | None}. nearest is None if nothing matches within
    a search window capped at max_distance_m * 2 — a real match further out
    than that isn't found (documented, not a bug: keeps the search bounded).
    Returns a structured {"error": ...} if upstream is unavailable or the
    dataset is missing columns this tool depends on.
    """
    try:
        result = overture.within_distance(lat, lon, max_distance_m, category, name)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_degraded_fields(result)


@mcp.tool()
def compare_areas(areas: list[dict], radius_m: float = 1000) -> dict:
    """Compare 2-5 areas side by side: category mix, density, and what differs.

    areas is a list of {"lat": ..., "lon": ...} centers sharing one
    radius_m. Returns per-area total_places, place density per km^2, and
    category_counts aligned across areas for the top ~10 categories by
    combined count, plus "differentiators" — those categories ranked by how
    much they differ, relatively, between areas (the fastest way to answer
    "how is area A different from area B"). Returns a structured {"error":
    ...} if areas isn't 2-5 centers, or if upstream is unavailable or the
    dataset is missing columns this tool depends on for any area (a partial
    comparison is not returned).
    """
    try:
        centers = [(a["lat"], a["lon"]) for a in areas]
    except (KeyError, TypeError) as e:
        return {"error": "bad_request", "detail": f"each area needs lat and lon: {e}"}
    try:
        result = overture.compare_areas(centers, radius_m)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_degraded_fields(budget.apply_budget(result, "differentiators"))


@mcp.tool()
def admin_lookup(lat: float, lon: float) -> dict:
    """Containing admin hierarchy for a point: neighborhood up to country.

    Point-in-polygon against Overture's divisions theme. Returns {"chain":
    [{"name": ..., "type": "locality", "id": ...}, ...]} smallest division
    first (e.g. neighborhood, then locality, county, region, country) — an
    empty chain means no division in the active dataset contains the
    point, which is a valid answer for remote areas, not an error. Returns
    a structured {"error": ...} if upstream is unavailable or the divisions
    dataset is missing the geometry column this tool depends on.
    """
    try:
        result = divisions.admin_lookup(lat, lon)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return budget.apply_budget(result, "chain")


@mcp.tool()
def summarize_buildings(
    lat: float, lon: float, radius_m: float = buildings.DEFAULT_SUMMARIZE_RADIUS_M
) -> dict:
    """Summarize building footprints in an area: count, footprint area, height/floor coverage, mix.

    From Overture's buildings theme (issue #23). Returns count,
    total/mean footprint area in m^2, height_known_pct/num_floors_known_pct
    (height and floor count are sparse in real Overture data — this reports
    coverage rather than pretending every building has a value, with
    mean_height_m/mean_num_floors alongside when any are known), and
    top_subtypes/top_classes (top 10 each by count). Returns a structured
    {"error": ...} if upstream is unavailable or the dataset is missing
    geometry/bbox.
    """
    try:
        result = buildings.summarize_buildings(lat, lon, radius_m)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    result = budget.apply_budget(result, "top_subtypes") if "top_subtypes" in result else result
    result = budget.apply_budget(result, "top_classes") if "top_classes" in result else result
    return _with_buildings_degraded_fields(result)


@mcp.tool()
def buildings_at(
    lat: float,
    lon: float,
    radius_m: float = buildings.DEFAULT_NEAREST_RADIUS_M,
    limit: int = buildings.DEFAULT_NEAREST_LIMIT,
    include_geometry: bool = False,
) -> dict:
    """Nearest building footprints to a point, nearest first.

    From Overture's buildings theme (issue #23). Returns {"results": [{id
    (GERS), subtype, class, footprint_area_m2, height_m, num_floors,
    distance_m}, ...]}. No raw geometry by default (design rule: answers,
    not data) — pass include_geometry=true to also get each row's
    footprint as GeoJSON, simplified to a small per-row token cap (each row
    then also carries geometry_max_deviation_m, reporting what was lost).
    Returns a structured {"error": ...} if upstream is unavailable or the
    dataset is missing geometry/bbox.
    """
    try:
        rows = buildings.buildings_at(lat, lon, radius_m, limit, include_geometry)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_buildings_degraded_fields(budget.apply_budget({"results": rows}, "results"))


@mcp.tool()
def geocode(query: str, limit: int = 5) -> dict:
    """Free-text place name -> ranked candidate locations, from Overture divisions and places.

    No Nominatim, no third-party geocoding API. Matches localities,
    neighborhoods, regions, and countries by name (exact > prefix >
    substring), falling back to named places if that doesn't fill `limit`.
    Returns {"results": [{name, type, lat, lon, id (GERS), admin_context,
    rank_score}, ...]}, budgeted like every other tool. Returns a structured
    {"error": ...} instead of raising if the remote scan fails.
    """
    try:
        rows = geocoding.geocode(query, limit)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    return budget.apply_budget({"results": rows}, "results")


@mcp.tool()
def geocode_batch(queries: list[str], limit_per_query: int = 3) -> dict:
    """Geocode up to 20 free-text queries in one call, one best match each.

    Cuts N round-trips of geocode() into one: for each query, runs
    geocode(query, limit_per_query) and keeps only the top candidate.
    Returns {"results": [{"query", "name", "type", "lat", "lon", "id"
    (GERS), "rank_score"}, ...]}, one row per query, in input order — a
    query with no match gets {"query", "error": "no match"} instead, and
    does not fail the rest of the batch. queries is capped at 20; a longer
    list returns a structured {"error": ...} rather than truncating
    silently. Budgeted like every other tool. Returns a structured
    {"error": ...} instead of raising if the remote scan itself fails.
    """
    if len(queries) > 20:
        return {
            "error": "bad_request",
            "detail": f"geocode_batch accepts at most 20 queries, got {len(queries)}",
        }
    rows = []
    try:
        for query in queries:
            candidates = geocoding.geocode(query, limit_per_query)
            if not candidates:
                rows.append({"query": query, "error": "no match"})
                continue
            top = candidates[0]
            rows.append(
                {
                    "query": query,
                    "name": top["name"],
                    "type": top["type"],
                    "lat": top["lat"],
                    "lon": top["lon"],
                    "id": top["id"],
                    "rank_score": top["rank_score"],
                }
            )
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    return budget.apply_budget({"results": rows}, "results")


@mcp.tool()
def resolve_place(
    query: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
    limit: int = 3,
) -> dict:
    """Free-text place reference -> ranked, typed GERS ids to hold onto.

    Turns something like "the Whole Foods on Lamar" or "Travis County" into
    stable Overture ids: merges geocode()'s division matches (locality,
    region, county, country, ...) with a name-filtered find_places search
    (a business or POI), bbox-limited to near_lat/near_lon if given, else to
    the ~20km vicinity of the top division match. Pass near_lat/near_lon
    whenever you have a rough location for the query — it narrows and
    speeds up the places half of the search.

    Returns {"results": [{"id" (GERS), "kind": "division" | "place",
    "name", "lat", "lon", "match": "exact" | "prefix" | "substring", plus
    "admin_context" for a division or "category" for a place}, ...]},
    ranked by match tier then prominence, budgeted like every other tool.
    An unresolvable query returns {"results": []} — not an error. Returns a
    structured {"error": ...} instead of raising if the remote scan fails
    or the places dataset is missing columns this tool depends on.
    """
    try:
        rows = geocoding.resolve_place(query, near_lat, near_lon, limit)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return budget.apply_budget({"results": rows}, "results")


@mcp.tool()
def resolve_place_batch(gers_ids: list[str]) -> dict:
    """Resolve up to 25 GERS ids to compact place rows in one call.

    Collapses N place_details(id=...) round-trips into one: for each id,
    resolves it via the same lookup place_details uses and keeps only a
    compact row — {"gers_id", "name", "category", "lat", "lon"} — not the
    full place_details payload (addresses, websites, phones, socials,
    sources, brand, confidence, ...). Use place_details for full detail on
    a single id. Results are returned in input order; an id that doesn't
    resolve gets {"gers_id", "error": "not found"} instead and does not
    fail the rest of the batch. gers_ids is capped at 25; a longer list
    returns a structured {"error": ...} rather than truncating silently.
    An empty list returns {"results": []}. Budgeted like every other tool.
    Returns a structured {"error": ...} instead of raising if the remote
    scan fails or the places dataset is missing columns this tool depends
    on.
    """
    if len(gers_ids) > 25:
        return {
            "error": "bad_request",
            "detail": f"resolve_place_batch accepts at most 25 ids, got {len(gers_ids)}",
        }
    rows = []
    try:
        for gers_id in gers_ids:
            place = overture.place_details(id=gers_id)
            if place is None:
                rows.append({"gers_id": gers_id, "error": "not found"})
                continue
            rows.append(
                {
                    "gers_id": gers_id,
                    "name": place.get("name"),
                    "category": place.get("category"),
                    "lat": place.get("lat"),
                    "lon": place.get("lon"),
                }
            )
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return budget.apply_budget({"results": rows}, "results")


@mcp.tool()
def reverse_geocode(lat: float, lon: float) -> dict:
    """Point -> nearest address (street/number/postcode) and its containing division chain.

    Degrades to a divisions-only result (source: "divisions_only", plus a
    note) if the addresses theme is unreachable, missing, or has no nearby
    coverage — addresses is Overture's newest, least complete theme, so
    this is the expected degraded path. Returns a structured {"error": ...}
    instead of raising if the remote scan fails outright.
    """
    try:
        return geocoding.reverse_geocode(lat, lon)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)


@mcp.tool()
def simplify_geometry(geojson: dict, max_tokens: int = 500) -> dict:
    """Simplify a GeoJSON geometry to fit a token budget, reporting what was lost.

    Works on caller-supplied GeoJSON (Polygon, MultiPolygon, LineString,
    MultiLineString; Points/MultiPoints pass through unchanged). Binary
    searches the simplification tolerance until the result fits max_tokens
    instead of asking the caller to guess one. Returns {"geometry": ...,
    "max_deviation_m": ..., "original_points": N, "kept_points": M}, or a
    structured {"error": "invalid_geometry", ...} for malformed input.
    """
    try:
        return simplify.simplify_geometry(geojson, max_tokens)
    except simplify.InvalidGeometry as e:
        return {"error": "invalid_geometry", "detail": e.detail}

@mcp.tool()
def render_map(result: dict | list, title: str | None = None, inline: bool = False) -> dict:
    """Render find_places/summarize_area JSON (or caller-supplied GeoJSON) as a map.

    Writes ONE self-contained HTML file — inline CSS/JS, vector markers with
    labels and click popups, polygon/line shapes (including
    routing.isochrone()'s {"polygon": ..., "stats": {...}} output), a scale
    bar, attribution, no CDN, no tile server, no API key, zero network
    requests when opened — to PLACEROOT_ARTIFACT_DIR (default: alongside the
    tile cache directory). The file itself is the artifact; this tool's
    response stays small on purpose. Returns {"path", "bytes",
    "features_rendered", "skipped_features"} (plus "truncated": True when
    applicable) — skipped_features counts rows/features that couldn't be
    rendered (missing coordinates, malformed geometry, or dropped past
    mapview.MAX_RENDER_VERTICES) rather than failing the call outright. Pass
    inline=true to also get the HTML back in the response when it's small
    enough to be worth it.
    """
    return mapview.write_artifact(result, title=title, inline=inline)

@mcp.tool()
def isochrone(
    lat: float,
    lon: float,
    minutes: float = 15,
    mode: str = "walk",
    speed_m_s: float | None = None,
    radius_m: float | None = None,
) -> dict:
    """Isochrone: the area reachable from (lat, lon) within `minutes`, by mode.

    Builds a street graph from Overture's transportation theme and runs
    Dijkstra out to the time budget. mode is "walk" (default), "cycle", or
    "drive" — each excludes its own set of unusable road classes (e.g.
    drive excludes footway/path/steps; cycle and drive exclude
    motorway/trunk... drive itself allows motorways) and respects one-way
    restrictions for cycle/drive (walk ignores them). speed_m_s overrides
    the mode's default speed model (walk 1.4 m/s, cycle 4.2 m/s, drive
    per-edge from Overture's speed_limits or a class-based default table)
    with a single constant. Returns {"polygon": <GeoJSON Polygon>, "stats":
    {reachable_nodes, max_radius_m, area_km2}, ...}. The polygon traces the
    boundary of reached nodes' occupied grid cells (falling back to a
    convex hull for very small reachable sets); reachable_nodes/
    max_radius_m are always exact, only the drawn polygon shape
    approximates, and is decimated/simplified to fit the token budget.

    radius_m optionally overrides the auto-derived graph extraction radius
    (capped per mode: 5km walk, 15km cycle, 60km drive); passing something
    larger than the cap returns a structured error instead of silently
    truncating. An unrecognized mode string returns a structured
    {"error": "unsupported_mode"}. minutes must be > 0 and radius_m (if
    given) must be >= 0, else returns {"error": "bad_request"}.
    """
    try:
        return routing.isochrone(
            lat, lon, minutes=minutes, mode=mode, speed_m_s=speed_m_s, radius_m=radius_m
        )
    except routing.UnsupportedMode:
        return {"error": "unsupported_mode", "supported": sorted(routing.MODE_CONFIG)}
    except routing.UpstreamUnavailable as e:
        return _upstream_error(e)
    except routing.SchemaDegraded as e:
        return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}
    except routing.NoGraphNearby as e:
        return {"error": "no_graph_nearby", "detail": e.detail}
    except routing.RadiusTooLarge as e:
        return {
            "error": "radius_too_large",
            "detail": e.detail,
            "max_radius_m": e.max_radius_m,
        }
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}


def _warm_start() -> None:
    """Best-effort cache pre-warm for PLACEROOT_WARM_REGION. Never blocks or raises.

    "Never blocks" refers to startup not being able to hang or crash on
    this — the call itself is synchronous (PLACEROOT_CACHE_SYNC), since
    this already only runs once, at startup, specifically to materialize
    the home region's tiles before real traffic arrives.
    """
    spec = os.environ.get("PLACEROOT_WARM_REGION")
    if not spec or not cache.enabled():
        return
    parsed = cache.parse_warm_region(spec)
    if parsed is None:
        logger.warning("PLACEROOT_WARM_REGION=%r is malformed, expected 'lat,lon,radius_m'", spec)
        return
    lat, lon, radius_m = parsed
    previous = os.environ.get("PLACEROOT_CACHE_SYNC")
    os.environ["PLACEROOT_CACHE_SYNC"] = "1"
    try:
        overture.find_places(lat, lon, radius_m, limit=1)
    except Exception as e:  # noqa: BLE001 - warm-on-start must never break startup
        logger.warning("PLACEROOT_WARM_REGION pre-warm failed (continuing): %s", e)
    finally:
        if previous is None:
            os.environ.pop("PLACEROOT_CACHE_SYNC", None)
        else:
            os.environ["PLACEROOT_CACHE_SYNC"] = previous


def _warm_metadata_async() -> None:
    """Kick off the shared connection's parquet-metadata pre-warm (issue #31)
    on a daemon thread so it doesn't delay startup, only the first query.
    """
    threading.Thread(target=overture.warm_metadata, daemon=True).start()


def _warm_divisions() -> None:
    """Thread target for _warm_divisions_async: build (or reuse) the #43
    local divisions name table, logging and swallowing anything that goes
    wrong rather than letting it become an unhandled exception on a daemon
    thread. geocode._local_divisions_table() already logs and degrades
    internally for the failure modes it recognizes (duckdb.Error,
    UpstreamUnavailable); this is a last-resort backstop for anything else.
    """
    try:
        geocoding._local_divisions_table()
    except Exception as e:  # noqa: BLE001 - warm-on-start must never break startup
        logger.warning("divisions-table pre-warm failed (continuing): %s", e)


def _warm_divisions_async() -> None:
    """Kick off geocode.py's #43 local divisions name-table materialization
    (issue #93) on a daemon thread at startup, mirroring
    _warm_metadata_async — so the ~20-30s one-time build (cold extension
    load plus a full COPY of the divisions theme) is already done, or at
    least underway, before the first real geocode()/resolve_place() call
    pays for it silently.

    A no-op when caching is off (PLACEROOT_CACHE=off) — checked here,
    before a thread is even spawned, rather than relying on
    _local_divisions_table's own cache.enabled() check, so this function's
    behavior is visible without reading into geocode.py.
    """
    if not cache.enabled():
        return
    threading.Thread(target=_warm_divisions, daemon=True).start()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="placeroot",
        description="PlaceRoot MCP server — ground AI agents in open map data.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve the streamable-HTTP transport instead of stdio (the default).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HTTP_HOST,
        help=f"Host to bind in --http mode (default: {DEFAULT_HTTP_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Port to bind in --http mode (default: {DEFAULT_HTTP_PORT}).",
    )
    return parser


def parse_transport_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args into transport config (mode/host/port). Extracted from
    main() so the mode-selection logic is directly unit-testable without
    starting a server.
    """
    return _build_arg_parser().parse_args(argv)


def main() -> None:
    args = parse_transport_args()
    active_release = release.resolve_release()
    # MCPServer.instructions is a read-only property over the low-level
    # server, which is what the initialize response actually reads from.
    mcp._lowlevel_server.instructions = (
        f"{BASE_INSTRUCTIONS} Backed by Overture Maps release {active_release}."
    )
    _warm_metadata_async()
    _warm_divisions_async()
    _warm_start()
    if args.http:
        logger.info("placeroot: streamable-HTTP on http://%s:%s/mcp", args.host, args.port)
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            # The SDK only auto-enables DNS-rebinding/Origin protection for the
            # loopback literals, and placeroot configures no authentication —
            # so a non-loopback bind exposes every tool, unauthenticated, to
            # anyone who can reach this host:port. Warn loudly; the operator
            # must front it with a reverse proxy / auth layer (see README).
            logger.warning(
                "placeroot is bound to a NON-LOOPBACK host (%s) with NO "
                "authentication — every tool is exposed to anyone who can reach "
                "%s:%s. Put a reverse proxy / auth layer in front of it before "
                "using this beyond a trusted local network.",
                args.host, args.host, args.port,
            )
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
