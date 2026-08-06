"""PlaceRoot MCP server — ground AI agents in open map data.

Run: uv run placeroot
"""

import logging
import os

from mcp.server.mcpserver import MCPServer

from placeroot import budget, cache, overture, release

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


def _warm_start() -> None:
    """Best-effort cache pre-warm for PLACEROOT_WARM_REGION. Never blocks or raises."""
    spec = os.environ.get("PLACEROOT_WARM_REGION")
    if not spec or not cache.enabled():
        return
    parsed = cache.parse_warm_region(spec)
    if parsed is None:
        logger.warning("PLACEROOT_WARM_REGION=%r is malformed, expected 'lat,lon,radius_m'", spec)
        return
    lat, lon, radius_m = parsed
    try:
        overture.find_places(lat, lon, radius_m, limit=1)
    except Exception as e:  # noqa: BLE001 - warm-on-start must never break startup
        logger.warning("PLACEROOT_WARM_REGION pre-warm failed (continuing): %s", e)


def main() -> None:
    active_release = release.resolve_release()
    # MCPServer.instructions is a read-only property over the low-level
    # server, which is what the initialize response actually reads from.
    mcp._lowlevel_server.instructions = (
        f"{BASE_INSTRUCTIONS} Backed by Overture Maps release {active_release}."
    )
    _warm_start()
    mcp.run()


if __name__ == "__main__":
    main()
