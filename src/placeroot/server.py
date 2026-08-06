"""PlaceRoot MCP server — ground AI agents in open map data.

Run: uv run placeroot
"""

import logging
import os
import threading

from mcp.server.mcpserver import MCPServer

from placeroot import budget, cache, divisions, overture, release

logger = logging.getLogger(__name__)

BASE_INSTRUCTIONS = (
    "Grounds spatial questions in Overture Maps open data. "
    "Answers are compact and ranked; distances in meters. Responses "
    "that don't fit the token budget carry truncated: true — narrow "
    "the query (smaller radius, a category or name filter) instead of "
    "raising limit."
)

mcp = MCPServer("placeroot", instructions=BASE_INSTRUCTIONS)


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


@mcp.tool()
def find_places(
    lat: float,
    lon: float,
    radius_m: float = 1000,
    category: str | None = None,
    name: str | None = None,
    limit: int = 10,
) -> dict:
    """Find named places near a point, nearest first.

    category matches Overture's taxonomy (e.g. 'coffee_shop', 'restaurant',
    'grocery'); name is a substring match on the place name. Results include
    operating_status so agents can reason about whether a place is open.
    Returns {"results": [...]}, plus truncated/omitted_count if the answer
    didn't fit the token budget. If the upstream dataset is unavailable or
    missing columns this tool depends on, returns a structured {"error":
    ...} instead of raising.
    """
    try:
        rows = overture.find_places(lat, lon, radius_m, category, name, limit)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_degraded_fields(budget.apply_budget({"results": rows}, "results"))


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
    """
    try:
        result = overture.place_details(id, name, lat, lon, radius_m)
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


def main() -> None:
    active_release = release.resolve_release()
    # MCPServer.instructions is a read-only property over the low-level
    # server, which is what the initialize response actually reads from.
    mcp._lowlevel_server.instructions = (
        f"{BASE_INSTRUCTIONS} Backed by Overture Maps release {active_release}."
    )
    _warm_metadata_async()
    _warm_start()
    mcp.run()


if __name__ == "__main__":
    main()
