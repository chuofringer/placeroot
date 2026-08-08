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
import functools
import inspect
import logging
import math
import os
import threading
from collections.abc import Callable

from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from placeroot import (
    addresses,
    budget,
    buildings,
    cache,
    categories,
    divisions,
    errors,
    geo,
    gers,
    infrastructure,
    land_use,
    mapview,
    overture,
    prompts,
    release,
    resources,
    routing,
    simplify,
    tool_profiles,
    water,
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

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8321

# Every @_tool function, in definition order. Registration is deferred to
# build_server() so PLACEROOT_TOOLS can select a subset *before* anything is
# registered — a tool outside the selection never reaches the MCP server,
# and so never reaches tools/list (issue #182).
_TOOL_FUNCS: dict[str, Callable] = {}

# The PLACEROOT_TOOLS=progressive meta-tools (issue #210), kept out of
# _TOOL_FUNCS deliberately: they are a way to reach the surface, not part of
# it. Everything that reasons about "the tools PlaceRoot has" — the profile
# definitions, the coverage guard, placeroot_capabilities' own catalog, what
# placeroot_call will dispatch to — reads _TOOL_FUNCS, and none of those
# should grow an entry because a meta-tool exists. Titles and annotations
# still live in the shared dicts below, so registration is one code path.
_META_TOOL_FUNCS: dict[str, Callable] = {}

# Human-readable display title per tool, keyed by function name (issue #193).
_TOOL_TITLES: dict[str, str] = {}

# The behavioral hints a PlaceRoot tool carries (MCP `annotations`).
# This is the default, and 24 of the 25 tools take it unchanged because they
# are pure reads:
#   read_only_hint  — the tool writes nothing anywhere the caller can see;
#                     the only side effect is a local parquet tile cache,
#                     which is invisible to the caller and to the upstream
#                     data. render_map is the exception: it writes an HTML
#                     file to disk, so it overrides this (see below).
#   destructive_hint/idempotent_hint — spelled out even though the spec says
#                     they only matter when read_only_hint is false, because
#                     clients that predate readOnlyHint-aware gating still
#                     read them, and "false/true" is the honest answer.
#   open_world_hint — False: the domain of interaction is one fixed, pinned
#                     Overture Maps release (see data_version), not the open
#                     web. Nothing here searches or fetches arbitrary URLs,
#                     so a client can reason about the blast radius as
#                     closed even though the bytes come over the network.
# Shared instance: ToolAnnotations is treated as immutable here, and the
# per-tool `title` rides on the top-level Tool.title field instead (which is
# what the current spec prefers), so one object serves every registration.
_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# render_map creates a new timestamped HTML file under the map output
# directory on every call. readOnlyHint is what clients gate auto-approval
# on, so claiming it here would hand a filesystem write the approval-free
# path meant for pure lookups. It is:
#   read_only_hint   False — it writes a file (mapview.py, mkdir + write_bytes)
#   destructive_hint False — the filename carries a timestamp, so it only
#                    ever adds; it never overwrites or deletes anything
#   idempotent_hint  False — two identical calls leave two distinct files
#   open_world_hint  False — same closed, pinned domain as everything else
_WRITES_A_FILE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)

# Per-tool annotation overrides, keyed by function name. Anything absent
# here gets _READ_ONLY_ANNOTATIONS.
_TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {}

# placeroot_call reaches every tool, render_map included, so it inherits the
# weakest claim any of them makes rather than its own: a dispatcher that
# advertised readOnlyHint would hand a filesystem write the approval-free
# path, and the client cannot see which tool a given call will land on.
# destructive_hint stays False because nothing behind it deletes or
# overwrites; idempotent_hint False because render_map's timestamped output
# means a repeat call is not a no-op.
_DISPATCH_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def _tool(
    title: str, annotations: ToolAnnotations | None = None, meta: bool = False
) -> Callable[[Callable], Callable]:
    """Mark a function as an MCP tool with a display title.

    Replaces a direct @mcp.tool(). `title` is required rather than
    defaulted so a new tool cannot ship untitled: forgetting it is a
    TypeError at import, not a silently unannotated entry in tools/list.

    `annotations` overrides the read-only default for the rare tool whose
    behavior is not a pure read — pass it at the definition site, next to
    the code that does the writing, so the claim and the behavior are read
    together. Everything else is annotated centrally in build_server().

    `meta` routes the function into _META_TOOL_FUNCS instead: a
    PLACEROOT_TOOLS=progressive meta-tool is registered exactly like any
    other tool but is not one of the tools PlaceRoot offers (issue #210).
    """
    if not isinstance(title, str) or not title.strip():
        # Catches the bare `@_tool` (no call) mistake, which would otherwise
        # bind the tool name to this decorator instead of to the function.
        raise TypeError("@_tool requires a non-empty display title, e.g. @_tool(\"Find places\")")

    def register(fn: Callable) -> Callable:
        (_META_TOOL_FUNCS if meta else _TOOL_FUNCS)[fn.__name__] = fn
        _TOOL_TITLES[fn.__name__] = title
        if annotations is not None:
            _TOOL_ANNOTATIONS[fn.__name__] = annotations
        return fn

    return register


def _upstream_error(e: Exception) -> dict:
    """Structured, agent-readable error for a failed remote scan — never a raw traceback."""
    return {"error": "upstream_unavailable", "detail": e.detail, "retry_advised": True}


def _schema_error(e: overture.SchemaDegraded) -> dict:
    return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}


def _invalid_coord(lat, lon) -> dict | None:
    """bad_request dict if lat/lon are out of range or non-finite, else None.

    Issue #163 (A2): bbox_around only clamps pole-*overshoot*, so an
    out-of-range lat (e.g. 91.0, or the common LLM mistake of swapping
    lat/lon) produced an inverted ymin>ymax box that silently matched zero
    rows instead of erroring. Every coordinate-taking tool calls this at
    its boundary and returns the error before doing any work. bool is
    checked separately from (int, float) because bool is a subclass of int
    (isinstance(True, int) is True) and a stray True/False would otherwise
    pass the range check.
    """
    for name, val, lo, hi in (("lat", lat, -90.0, 90.0), ("lon", lon, -180.0, 180.0)):
        if (
            not isinstance(val, (int, float))
            or isinstance(val, bool)
            or not math.isfinite(val)
            or not (lo <= val <= hi)
        ):
            return {
                "error": "bad_request",
                "detail": (
                    f"{name}={val!r} is out of range; lat must be in [-90, 90] and "
                    "lon in [-180, 180] (did you swap lat and lon?)"
                ),
            }
    return None


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

    Joins onto any note already present rather than replacing it:
    places_along_route can arrive here carrying a truncation note (its
    corridor candidate budget was hit, or the street graph was capped)
    that explains why an empty result may not mean "nothing matched" —
    clobbering it would leave truncated: true with no explanation and
    lose the more accurate advice.
    """
    if category and not payload.get("results"):
        hint = (
            f"no places matched category '{category}' here; if that may not be a "
            "valid Overture category slug, use search_categories to find the right "
            f"one, or {widen_hint} / drop the category filter."
        )
        payload["note"] = "; ".join(filter(None, [payload.get("note"), hint]))
    return payload


@_tool("Find places")
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
    area: str | None = None,
) -> dict:
    """Find named places, either near a point or inside an area's boundary.

    Three mutually exclusive modes:
    - Point + radius: pass lat and lon (radius_m defaults to 1000m).
      Results are nearest-first, within a circle around (lat, lon).
    - Division polygon: pass division_id (a GERS division id, e.g. one from
      an admin-hierarchy chain) instead of lat/lon. Results are every matching place whose
      point falls inside that division's true boundary polygon — no radius
      to guess, and no circle clipping a coastline or straddling a border.
      Results are ordered by name (there's no reference point to rank
      distance from).
    - Area by name: pass area ("Palo Alto") to get the division-polygon mode
      without first resolving the id yourself. The name is resolved with the
      same ranking geocode/resolve_place use; the resolved division is
      echoed back as "area" on the response so it's clear which one was
      searched. A name matching several equally-ranked divisions returns
      {"error": "ambiguous_area", "candidates": [...]} listing their
      division_ids rather than silently picking one, and an unresolvable
      name returns {"error": "not_found"} rather than an empty result that
      would read as "this place has no coffee shops".

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
    ...} if no mode's inputs are given (or more than one is), {"error":
    "not_found", ...} if division_id/area doesn't match any known division, or
    a structured {"error": ...} if the upstream dataset is unavailable or
    missing columns this tool depends on. If a category filter was given and
    it matched nothing (in either mode), a non-fatal "note" field hints that
    the category slug may be wrong and points at search_categories.
    """
    point_given = lat is not None or lon is not None
    modes_given = sum([point_given, division_id is not None, area is not None])
    if modes_given > 1:
        return {
            "error": "bad_request",
            "detail": "pass exactly one of lat/lon (+radius_m), division_id, or area",
        }
    if modes_given == 0:
        return {
            "error": "bad_request",
            "detail": "pass one of lat/lon (+radius_m), division_id, or area",
        }

    # area is sugar over the division_id path: resolve the name to a
    # division, then fall through to exactly the same polygon search.
    resolved_area = None
    if area is not None:
        if not area.strip():
            return {"error": "bad_request", "detail": "area must be a non-empty name"}
        try:
            resolved_area = geocoding.resolve_area(area)
        except errors.AmbiguousArea as e:
            return {
                "error": "ambiguous_area",
                "detail": e.detail,
                "candidates": e.candidates,
            }
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        if resolved_area is None:
            return {"error": "not_found", "detail": f"no division matched area {area!r}"}
        division_id = resolved_area["division_id"]

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
        if resolved_area is not None:
            # Which division the name landed on — an agent that asked for
            # "Springfield" needs to see which one it got.
            payload["area"] = resolved_area
        return _with_category_hint(payload, category, widen_hint="try a larger division")

    if lat is None or lon is None:
        return {"error": "bad_request", "detail": "pass both lat and lon"}
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
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


@_tool("Summarize area")
def summarize_area(lat: float, lon: float, radius_m: float = 1000) -> dict:
    """Summarize what's in an area: total places and top categories.

    Returns a structured {"error": ...} instead of raising if upstream is
    unavailable or the dataset is missing columns this tool depends on.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        result = overture.summarize_area(lat, lon, radius_m)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_degraded_fields(budget.apply_budget(result, "top_categories"))


@_tool("Place details")
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
    if lat is not None and lon is not None:
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    if near_lat is not None and near_lon is not None:
        coord_error = _invalid_coord(near_lat, near_lon)
        if coord_error is not None:
            return coord_error
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


@_tool("Check within distance")
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
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        result = overture.within_distance(lat, lon, max_distance_m, category, name)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_degraded_fields(result)


@_tool("Distance matrix")
def distance_matrix(origins: list[dict], destinations: list[dict]) -> dict:
    """Straight-line (great-circle) distance in meters between every origin and destination.

    origins and destinations are each a list of {"lat": ..., "lon": ...}
    points, capped at 10 each (100 pairs max) — this is a plain haversine
    calculation, not a routed distance or travel time, so it's cheap but it
    is NOT what Google/Mapbox distance-matrix APIs return: no roads, no
    turns, no travel time. For "how far can I get in N minutes" use
    isochrone() instead.

    Returns {"elements": [{"origin_idx": 0, "dest_idx": 0, "distance_m":
    812}, ...]}, flat and origin-major (all destinations for origin 0,
    then origin 1, ...), budgeted like every other tool. Empty origins or
    destinations returns {"elements": []}. Returns a structured {"error":
    "bad_request", ...} instead of raising if either list exceeds 10
    points or a point is missing/non-numeric lat or lon.
    """
    if len(origins) > 10 or len(destinations) > 10:
        return {
            "error": "bad_request",
            "detail": "at most 10 origins and 10 destinations are allowed",
        }
    if not origins or not destinations:
        return {"elements": []}
    try:
        o_pts = [(float(p["lat"]), float(p["lon"])) for p in origins]
        d_pts = [(float(p["lat"]), float(p["lon"])) for p in destinations]
    except (KeyError, TypeError, ValueError) as e:
        return {"error": "bad_request", "detail": f"each point needs numeric lat and lon: {e}"}
    for idx, (plat, plon) in enumerate(o_pts):
        coord_error = _invalid_coord(plat, plon)
        if coord_error is not None:
            coord_error["detail"] = f"origins[{idx}]: {coord_error['detail']}"
            return coord_error
    for idx, (plat, plon) in enumerate(d_pts):
        coord_error = _invalid_coord(plat, plon)
        if coord_error is not None:
            coord_error["detail"] = f"destinations[{idx}]: {coord_error['detail']}"
            return coord_error
    elements = [
        {
            "origin_idx": oi,
            "dest_idx": di,
            "distance_m": round(geo.haversine_m(olat, olon, dlat, dlon)),
        }
        for oi, (olat, olon) in enumerate(o_pts)
        for di, (dlat, dlon) in enumerate(d_pts)
    ]
    return budget.apply_budget({"elements": elements}, "elements")


@_tool("Compare areas")
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
    for idx, (clat, clon) in enumerate(centers):
        coord_error = _invalid_coord(clat, clon)
        if coord_error is not None:
            coord_error["detail"] = f"areas[{idx}]: {coord_error['detail']}"
            return coord_error
    try:
        result = overture.compare_areas(centers, radius_m)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_degraded_fields(budget.apply_budget(result, "differentiators"))


@_tool("Admin hierarchy lookup")
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
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        result = divisions.admin_lookup(lat, lon)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return budget.apply_budget(result, "chain")


@_tool("Summarize buildings")
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
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        result = buildings.summarize_buildings(lat, lon, radius_m)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    result = budget.apply_budget(result, "top_subtypes") if "top_subtypes" in result else result
    result = budget.apply_budget(result, "top_classes") if "top_classes" in result else result
    return _with_buildings_degraded_fields(result)


@_tool("Buildings near a point")
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
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        rows = buildings.buildings_at(lat, lon, radius_m, limit, include_geometry)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return _with_buildings_degraded_fields(budget.apply_budget({"results": rows}, "results"))


@_tool("Land use at a point")
def land_use_at(lat: float, lon: float) -> dict:
    """What kind of land is this: land use and land cover classification at a point.

    From Overture's base theme (issue #167) — PlaceRoot's first tool over
    base, distinct from the place-search and area-summary tools (those cover
    discrete POIs, not the land itself). Returns {"lat", "lon", "land_use":
    {"subtype", "class", "name"} or null, "land_cover": {"subtype",
    "class"} or null}. No raw geometry (design rule: answers, not data).

    null for either field means no polygon of that type covers the point —
    coverage is OSM-derived and patchy outside well-mapped cities, so this
    is a common, valid answer for a rural or remote point, not an error.
    When multiple polygons overlap (Overture nests them, e.g. a park inside
    a residential parcel), the smallest/most specific one is returned and
    a "note" flags that the pick was made among several valid candidates.
    Returns a structured {"error": ...} if upstream is unavailable or a
    base-theme dataset is missing geometry/bbox, and {"error":
    "bad_request"} for a non-finite or out-of-range coordinate.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        result = land_use.land_use_at(lat, lon)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    degraded = land_use.degraded_fields()
    if degraded:
        result["degraded_fields"] = degraded
    return result


def _with_infrastructure_truncation(
    payload: dict, total_in_range: int, subtype: str | None, infra_class: str | None
) -> dict:
    """Flag an infrastructure_at answer that is a slice of a much larger set.

    budget.apply_budget only knows about rows *it* dropped; the SQL LIMIT
    cuts first and invisibly, and base/infrastructure is street-furniture
    dominated, so the 10 nearest features around a city square are lamps
    and benches even when 72 bridges are in range. Left unflagged that
    reads as "no bridge near here". So: whenever fewer rows came back than
    matched, say so, give the true in-range count, and name the move that
    actually finds landmarks (a subtype/infra_class filter), not just
    "narrow the query".
    """
    shown = len(payload.get("results", []))
    if total_in_range <= shown:
        return payload
    payload["truncated"] = True
    payload["omitted_count"] = total_in_range - shown
    payload["total_in_range"] = total_in_range
    if subtype or infra_class:
        payload["note"] = (
            f"showing the {shown} nearest of {total_in_range} matching features; "
            "narrow further with a smaller radius or a more specific filter."
        )
    else:
        payload["note"] = (
            f"showing the {shown} nearest of {total_in_range} infrastructure features "
            "in range. This layer is dominated by street furniture (street lamps, "
            "benches, waste baskets, bollards), so the nearest few are usually not "
            "landmarks and their absence here proves nothing: to ask about landmarks, "
            "filter, e.g. subtype='bridge' / subtype='tower' / infra_class='pier', or "
            "use a smaller radius."
        )
    return payload


@_tool("Infrastructure near a point")
def infrastructure_at(
    lat: float,
    lon: float,
    radius_m: float = infrastructure.DEFAULT_RADIUS_M,
    limit: int = infrastructure.DEFAULT_LIMIT,
    subtype: str | None = None,
    infra_class: str | None = None,
) -> dict:
    """Infrastructure near a point, nearest first: bridges, towers, piers — and street furniture.

    From Overture's base theme (issue #179), type=infrastructure — the
    built things that are neither buildings nor POIs. Read the data
    honestly before trusting an answer: this layer is dominated by street
    furniture (street_lamp, bench, waste_basket, bollard, kerb, crossing),
    which outnumbers landmark infrastructure roughly 50:1 in a city
    centre. An unfiltered query in a dense area returns lamps and benches
    and says nothing about whether a bridge is nearby. To ask about
    landmarks, filter: subtype/infra_class match Overture's `subtype` and
    `class` columns (case-insensitive substring; infra_class is `class`
    under a non-reserved name) — e.g. subtype="bridge", subtype="tower",
    subtype="power", infra_class="pier".

    Returns {"center", "radius_m", "results": [{"id", "subtype", "class",
    "name", "distance_m"}, ...]}, plus "truncated": true, "total_in_range"
    and an explanatory "note" whenever more features matched than were
    returned. id is the GERS id, usable with other GERS-keyed tools. No raw
    geometry (design rule: answers, not data).

    Radius search, not containment: most infrastructure is linear or a
    bare point, so "what's within radius_m" is the answerable question.
    distance_m is measured to the closest point on the feature, not its
    centroid — a bridge you are standing on reads ~0 m, not "distance to
    the middle of the bridge". radius_m echoes the effective radius, which
    may be lower than requested (large values are clamped).

    An empty results list is a valid answer, not an error: base-theme
    coverage is OSM-derived and patchy, and "no infrastructure within
    500 m" is a real finding. Returns a structured {"error": ...} if
    upstream is unavailable or the dataset is missing geometry/bbox, and
    {"error": "bad_request"} for a non-finite or out-of-range coordinate.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        rows, effective_radius_m, total_in_range = infrastructure.infrastructure_at(
            lat, lon, radius_m, limit, subtype=subtype, infra_class=infra_class
        )
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    result = {
        "center": {"lat": lat, "lon": lon},
        "radius_m": effective_radius_m,
        "results": rows,
    }
    result = budget.apply_budget(result, "results")
    result = _with_infrastructure_truncation(result, total_in_range, subtype, infra_class)
    degraded = infrastructure.degraded_fields()
    if degraded:
        result["degraded_fields"] = degraded
    return result


def _water_notes(
    payload: dict,
    in_range_count: int,
    containing: dict | None,
    radius_m: float,
    ocean_only_filter: bool = False,
    filtered: bool = False,
) -> dict:
    """Attach water_near's on_water/truncation/empty notes to the payload.

    Four things a caller can get wrong here, so all four are said out
    loud. (1) The point is inside a generalized *marine* polygon: oceans
    are cut into 1-degree tiles and their landward edge swallows coastal
    land, so "inside" means at-or-near the water, and no shoreline
    distance is derivable from them at all (their boundaries carry phantom
    grid cuts). Inside a lake or a reservoir none of that applies — those
    polygons are real, so the note says plainly which body you are in and
    claims nothing about tiles. (2) The filter asked only for oceans,
    which are reported through on_water and never as a distance row, so
    the empty list means "wrong question", not "no water". (3) The rows
    shown are a slice — a canal district puts hundreds of rows in a 500 m
    circle. (4) Nothing came back: with no filter active that is a real
    finding about an arid or unmapped place rather than a failure — but
    when a subtype/water_class filter is active (`filtered`), it only
    means nothing *matched the filter*, and saying "arid, remote or
    unmapped" about a canal district asked for waterfalls would be
    confidently wrong.
    """
    shown = len(payload.get("results", []))
    notes = []
    if ocean_only_filter:
        notes.append(
            "a subtype filter matching only 'ocean' cannot return distance rows: "
            "Overture ships the ocean as generalized 1-degree tiles whose landward "
            "edge covers dry land, so oceans are reported through \"on_water\" and "
            "\"water_body\" instead. Drop the filter to see the nearest real water "
            "features, or use water_class='bay' for a named coastal body."
        )
    if containing is not None:
        payload["on_water"] = True
        if containing["water_body"]:
            payload["water_body"] = containing["water_body"]
        body = containing["water_body"] or "a water polygon"
        if containing["generalized"]:
            notes.append(
                f"this point falls inside Overture's generalized {body} polygon. "
                "Oceans and seas are cut into 1-degree tiles and their landward edge "
                "is coarse enough to cover dry coastal land, so read this as "
                "'on or close to the water', not as a precise shoreline test — "
                "and no distance to the coast is reported, because the tile "
                "boundaries include phantom grid cuts through open water."
            )
        else:
            notes.append(f"this point is inside {body}.")
    if in_range_count > shown:
        payload["truncated"] = True
        notes.append(
            f"showing the {shown} nearest of {in_range_count} water features in range; "
            "narrow with a smaller radius or a filter, e.g. subtype='canal', "
            "subtype='river', water_class='lake'."
        )
    elif shown == 0 and containing is None and not ocean_only_filter:
        if filtered:
            notes.append(
                f"no water features matching the subtype/water_class filter "
                f"within {radius_m:g} m. That says nothing about water in "
                "general here — drop the filter to see the nearest water "
                "features of any kind."
            )
        else:
            notes.append(
                f"no water features within {radius_m:g} m. base/water coverage is "
                "OSM-derived, so an arid, remote or unmapped area legitimately has "
                "nothing here — this is an answer, not a failure."
            )
    payload["in_range_count"] = in_range_count
    if notes:
        payload["note"] = " ".join(notes)
    return payload


@_tool("Water near a point")
def water_near(
    lat: float,
    lon: float,
    radius_m: float = water.DEFAULT_RADIUS_M,
    limit: int = water.DEFAULT_LIMIT,
    subtype: str | None = None,
    water_class: str | None = None,
) -> dict:
    """Water near a point, nearest first: waterfront check, distance to river/canal/lake.

    From Overture's base theme (issue #200), type=water — oceans, bays,
    lakes, ponds, reservoirs, rivers, streams, canals, springs, pools.
    Returns {"center", "radius_m", "in_range_count", "results": [{"name"
    (when named), "subtype", "class", "distance_m", "is_salt"/
    "is_intermittent" (only when true)}, ...]}, plus "truncated": true and
    a "note" when more matched than were returned. No raw geometry.

    distance_m is to the closest point on the feature, not its centroid —
    a canal bank you are standing on reads ~0 m. Water gets dense (an
    Amsterdam canal district puts hundreds of rows in a 500 m circle),
    which is what in_range_count and the filters are for:
    subtype/water_class match Overture's `subtype`/`class` columns
    (case-insensitive substring; water_class is `class` under a
    non-reserved name) — e.g. subtype="canal", subtype="river",
    water_class="lake".

    "on_water": true plus "water_body" means the point is *inside* a water
    polygon — a lake, a reservoir, a river. For oceans and seas that is a
    coarse signal: Overture cuts them into 1-degree tiles whose landward
    edge covers dry coastal land, so a waterfront building reads as inside
    the ocean. Those bodies are reported this way rather than as a bogus
    0 m "nearest water" row, and no distance-to-coast is derived from them
    (their tile boundaries include phantom cuts through open water), which
    also means subtype="ocean" cannot return distance rows. Lakes and
    rivers carry none of that: however large, they appear in results with
    a real edge distance. The "note" says which case applies.

    An empty results list is a valid answer: coverage is OSM-derived, and
    "no water within 500 m" is a real finding about an arid or unmapped
    place. radius_m echoes the effective radius (large values are
    clamped). Returns a structured {"error": ...} if upstream is
    unavailable or the dataset is missing geometry/bbox, and {"error":
    "bad_request"} for a bad coordinate.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        rows, effective_radius_m, in_range_count, containing = water.water_near(
            lat, lon, radius_m, limit, subtype=subtype, water_class=water_class
        )
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    result = {
        "center": {"lat": lat, "lon": lon},
        "radius_m": effective_radius_m,
        "results": rows,
    }
    result = budget.apply_budget(result, "results")
    degraded = water.degraded_fields()
    # The ocean-only note mirrors water.water_near's short-circuit, which
    # only fires when the subtype column exists: under a degraded schema
    # the filter is a no-op and real distance rows come back, so claiming
    # the call "cannot return distance rows" would contradict the payload.
    ocean_only = (
        water._ocean_only_filter(subtype, water_class) and "subtype" not in degraded
    )
    result = _water_notes(
        result, in_range_count, containing, effective_radius_m,
        ocean_only_filter=ocean_only,
        filtered=bool(subtype or water_class),
    )
    if degraded:
        result["degraded_fields"] = degraded
    return result


@_tool("Geocode a place name")
def geocode(query: str, limit: int = 5) -> dict:
    """Free-text place name -> ranked candidate locations, from Overture divisions and places.

    No Nominatim, no third-party geocoding API. Matches localities,
    neighborhoods, regions, and countries by name (exact > prefix >
    substring), falling back to named places if that doesn't fill `limit`.
    Returns {"results": [{name, type, lat, lon, id (GERS), admin_context,
    rank_score}, ...]}, budgeted like every other tool. Returns a structured
    {"error": ...} instead of raising if the remote scan fails.

    A query with no location context in it at all (a bare place name that
    matches no division, e.g. "Blue Bottle Roastery") can't be bounded to a
    region, so the places half of the search is skipped rather than
    scanning the global places dataset — minutes, not seconds (#105). That
    case comes back empty with a "note" saying so and what to do instead.

    A misspelled name that matches no division literally ("Berekley", or
    "Berekley, CA" — the region suffix is set aside first) gets one
    close-spelling retry over the local divisions table (#215); those
    results rank below any literal match, carry "matched_by": "fuzzy", and
    come with a "note" naming the spelling they were corrected to.

    A query that is entirely a postcode ("94110", "1011AB") is answered as
    one (#223): one result per country whose address points carry that code,
    with "type": "postcode", "country", "address_count" and a null "id" (a
    postcode is not a GERS entity). Codes are shared across countries far
    more often than not, so the alternates below the top row are real
    ambiguity. The accompanying "note" carries the granularity caveat (a
    Dutch code is a street block, a US ZIP a district) and the coverage
    limits -- including the countries the addresses theme covers but that
    carry no postcode values at all, which is why a valid postcode can still
    come back empty.

    Exonyms work too (#214): names are matched against Overture's ~100
    localized alternates as well as its canonical one, so "Munich" answers
    München and "Tokyo" answers 東京都. `name` is always the canonical
    spelling; such rows carry an extra "matched_name" naming the alternate
    that matched.
    """
    try:
        result = geocoding.geocode_detailed(query, limit)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    payload = budget.apply_budget({"results": result["results"]}, "results")
    if "note" in result:
        payload["note"] = result["note"]
    return payload


@_tool("Geocode names in batch")
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


@_tool("Search categories")
def search_categories(query: str, limit: int = 8) -> dict:
    """Free text -> valid Overture category slugs, for the `category` param
    the place-search and area-summary tools take.

    Lookup only — no geo filtering, no upstream dataset dependency; matches
    against a bundled snapshot of Overture's places taxonomy (pinned to
    schema v1.9.0). Ranks exact slug match > slug prefix > slug substring >
    a match on any taxonomy path segment, so close siblings like "cafe" vs
    "coffee_shop" both surface rather than one silently winning. Returns
    {"results": [{"slug", "path"}, ...]} — path is the root-to-leaf
    taxonomy (e.g. ["eat_and_drink", "cafe", "coffee_shop"]), budgeted like
    every other tool. An empty/whitespace query returns {"results": []}.
    limit must be 1-50; out of range returns a structured
    {"error": "bad_request", ...}.
    """
    if limit < 1 or limit > 50:
        return {"error": "bad_request", "detail": "limit must be between 1 and 50"}
    rows = categories.search_categories(query, limit)
    return budget.apply_budget({"results": rows}, "results")


@_tool("Resolve place to GERS id")
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
    "name", "lat", "lon", "match": "exact" | "prefix" | "substring" |
    "fuzzy", plus "admin_context" for a division or "category" for a
    place}, ...]}, ranked by match tier then prominence ("fuzzy" — a
    division reached by close spelling rather than by containing the query
    at all, #215 — ranking below every literal match), budgeted like every
    other tool.
    An unresolvable query returns {"results": []} — not an error. Returns a
    structured {"error": ...} instead of raising if the remote scan fails
    or the places dataset is missing columns this tool depends on.
    """
    if near_lat is not None and near_lon is not None:
        coord_error = _invalid_coord(near_lat, near_lon)
        if coord_error is not None:
            return coord_error
    try:
        rows = geocoding.resolve_place(query, near_lat, near_lon, limit)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return budget.apply_budget({"results": rows}, "results")


@_tool("Resolve GERS ids in batch")
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


@_tool("Look up a GERS id")
def gers_lookup(id: str, near_lat: float | None = None, near_lon: float | None = None) -> dict:
    """Any GERS id -> what it is, across themes, plus its cheap cross-theme joins.

    The reverse of every other tool: hand back an id one of them returned
    (a place, a division, or a building) and get the entity it names —
    {"id", "theme", "type", "name", "lat", "lon", "summary", "related"} —
    without needing to know which theme it came from. summary carries a few
    theme-specific fields (place: category, confidence, brand; division:
    subtype, country, region; building: class, height, floors); related
    carries the containing division, plus the building at the point when
    the id is a place. Never geometry.

    Also pass near_lat/near_lon — the lat/lon of the row the id came from —
    whenever you have them: the lookup is an id scan across up to three
    themes, and the hint narrows each one to a ~50km box instead of a
    full-theme scan. Omitting it still works, just much slower on a cold id.
    The hint *bounds* the search rather than merely ordering it: an id
    outside the box comes back not_found with a note saying so, and the
    exhaustive lookup is the same call without near_lat/near_lon. Pass a
    hint you are sure of, or none at all.

    Transportation segment/connector ids are not resolvable yet and come
    back as not_found. Returns {"error": "not_found"} if no theme claims
    the id, {"error": "bad_request"} for a malformed id (a GERS id is an
    opaque token — 32 lowercase hex characters) or an out-of-range hint,
    or a structured {"error": ...} if upstream is unavailable.
    """
    if near_lat is not None and near_lon is not None:
        coord_error = _invalid_coord(near_lat, near_lon)
        if coord_error is not None:
            return coord_error
    try:
        result = gers.gers_lookup(id, near_lat, near_lon)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    if result is None:
        not_found = {
            "error": "not_found",
            "detail": (
                f"no places, divisions, or buildings entity matched GERS id {id!r} "
                "(transportation segments are not resolvable yet)"
            ),
        }
        if near_lat is not None and near_lon is not None:
            not_found["note"] = gers.HINT_MISS_NOTE
        return not_found
    # Which dataset's degraded columns apply depends on which theme claimed
    # the id; a division answer has no degraded-fields notion of its own.
    if result["theme"] == "places":
        return _with_degraded_fields(result)
    if result["theme"] == "buildings":
        return _with_buildings_degraded_fields(result)
    return result


@_tool("Reverse geocode a point")
def reverse_geocode(lat: float, lon: float) -> dict:
    """Point -> nearest address (street/number/postcode) and its containing division chain.

    Degrades to a divisions-only result (source: "divisions_only", plus a
    note) if the addresses theme is unreachable, missing, or has no nearby
    coverage — addresses is Overture's newest, least complete theme, so
    this is the expected degraded path. Returns a structured {"error": ...}
    instead of raising if the remote scan fails outright.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        return geocoding.reverse_geocode(lat, lon)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)


@_tool("Nearest street addresses to a point")
def address_at(lat: float, lon: float, limit: int = addresses.DEFAULT_LIMIT) -> dict:
    """Nearest street addresses to a point, nearest first: number, street, unit, postcode.

    The address-level counterpart to reverse_geocode (issue #188): where
    that returns one collapsed hop plus the admin chain, this returns the
    few doorways around the point with the attributes an address lookup
    wants. Returns {"results": [{number, street, unit, postcode,
    postal_city, address_levels, country, distance_m}, ...]}, capped at 5.
    Optional attributes are omitted when the source has no value for them.

    No id is returned: Overture documents address ids as not GERS-stable, so
    unlike a place/division/building id there is no durable handle to hand
    out. For a stable reference to what is at a coordinate, use
    reverse_geocode and hold onto the division it names.

    Coverage is the thing to read carefully. The addresses theme is
    Overture's only alpha theme and covers 39 countries — no UK, Ireland,
    India, China, Korea or Russia, no Africa or Middle East, and little of
    Latin America outside Brazil, Mexico, Chile, Colombia and Uruguay. An
    empty results list is a valid answer, never an error, and always carries
    a "note" saying whether the country is outside the theme's coverage
    entirely or is covered but had nothing within the search radius. That
    country is the one whose division polygon contains the point, so the note
    stays correct next to a border; if the lookup behind it cannot run, the
    note says so rather than asserting anything about the data.

    Returns a structured {"error": ...} if upstream is unavailable, if the
    dataset is missing the bbox/street columns this depends on, or for an
    out-of-range coordinate.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        result = addresses.address_at(lat, lon, limit)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return budget.apply_budget(result, "results")


@_tool("Find a street address")
def geocode_address(
    query: str = "",
    limit: int = geocoding.ADDRESS_DEFAULT_LIMIT,
    number: str | None = None,
    street: str | None = None,
    city: str | None = None,
) -> dict:
    """Street address -> coordinates: "1600 Amphitheatre Parkway, Mountain View".

    The forward counterpart to address_at, and finer than geocode, which
    answers at city/neighborhood granularity and never at a doorway. The
    first comma splits the street from the city; a bare integer at either end
    of the street part is the house number ("1600 Amphitheatre Parkway",
    "Hauptstraße 5"). Pass `number`/`street`/`city` instead if you already
    have the parts. Unit/apartment numbers are not parsed.

    The city is resolved first and its boundary bounds the search, so a city
    that resolves to no boundary returns an empty list plus a note rather
    than a scan — never a wrong-neighborhood guess. Street names match in
    either spelling (Parkway/Pkwy, West/W).

    Returns {"results": [{number, street, unit, postcode, country,
    distance_m, lat, lon}, ...], "anchor": {name, id}}, deduplicated to
    distinct number+street and nearest the city's own point first. More matches
    than `limit` adds "truncated", "distinct_in_range" and a note.

    Coverage is alpha: 39 countries, no UK, Ireland, India or China. An empty
    list is a valid answer and always carries a note saying whether the
    country is uncovered or the street simply wasn't found.
    """
    try:
        result = geocoding.geocode_address(
            query, limit, number=number, street=street, city=city
        )
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    return budget.apply_budget(result, "results")


@_tool("Reverse geocode points in batch")
def reverse_geocode_batch(points: list[dict]) -> dict:
    """Reverse-geocode many points in one call, to cut N round-trips down to one.

    Accepts at most 20 points; a longer list returns a structured
    {"error": "bad_request"} instead of processing anything. Returns one
    row per point in `points`, in the same order — each row is whatever
    reverse_geocode(lat, lon) returns (address/divisions chain, or a
    "divisions_only" degrade — see reverse_geocode's docstring). A
    malformed point (missing or non-numeric lat/lon) doesn't fail the
    whole batch — it yields a per-row {"error": ...} in its slot instead.
    """
    if len(points) > 20:
        return {
            "error": "bad_request",
            "detail": f"reverse_geocode_batch accepts at most 20 points, got {len(points)}",
        }

    rows = []
    try:
        for p in points:
            try:
                lat = float(p["lat"])
                lon = float(p["lon"])
            except (KeyError, TypeError, ValueError):
                rows.append(
                    {
                        "lat": p.get("lat") if isinstance(p, dict) else None,
                        "lon": p.get("lon") if isinstance(p, dict) else None,
                        "error": "each point needs numeric lat and lon",
                    }
                )
                continue
            coord_error = _invalid_coord(lat, lon)
            if coord_error is not None:
                rows.append({"lat": lat, "lon": lon, "error": coord_error["detail"]})
                continue
            rows.append(geocoding.reverse_geocode(lat, lon))
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)

    return budget.apply_budget({"results": rows}, "results")


@_tool("Simplify geometry")
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

@_tool("Render map", annotations=_WRITES_A_FILE_ANNOTATIONS)
def render_map(result: dict | list, title: str | None = None, inline: bool = False) -> dict:
    """Render place-search or area-summary JSON (or caller-supplied GeoJSON) as a map.

    Writes ONE self-contained HTML file — inline CSS/JS, vector markers with
    labels and click popups, polygon/line shapes (including reachability
    output shaped {"polygon": ..., "stats": {...}}), a scale
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

@_tool("Reachable area (isochrone)")
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
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
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


@_tool("Route between two points")
def route(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    mode: str = "drive",
    include_path: bool = False,
) -> dict:
    """Route: shortest-path distance and duration between two points, by mode.

    Compact directions, not turn-by-turn: builds a street graph from
    Overture's transportation theme around the two points and returns
    {"distance_m", "duration_s", "mode", "from", "to"} for the fastest path
    — no polyline unless you ask for one. mode is "walk",
    "cycle", or "drive" (default), on the same cost model every routing tool
    uses (walk 1.4 m/s, cycle 4.2 m/s, drive per-edge from Overture's
    speed_limits or a class-based default table). drive's duration is a posted-speed model
    with no live traffic; all modes snap each endpoint to the nearest
    usable street-graph node (real routes rarely start/end exactly on a
    segment).

    Each mode has a straight-line-distance cap on the two points, rejected
    before any graph is built (see routing.ROUTE_MAX_STRAIGHT_LINE_M, derived
    per-mode from the shared graph-extraction radius cap — roughly
    walk 7.5km, cycle 23.5km, drive 95.5km) — real road distance only ever
    exceeds straight-line, so anything past the cap can't produce a route
    worth extracting for anyway; returns {"error": "route_too_long"} with
    the exact cap in "max_distance_m". An unrecognized mode string returns
    {"error": "unsupported_mode"}; non-finite or out-of-range coordinates
    (lat outside [-90, 90], lon outside [-180, 180]) return
    {"error": "bad_request"}. If no usable graph or street node is found
    near either point, returns {"error": "no_graph_nearby"}. If both points
    snap into the graph but no path connects them (e.g. disconnected
    islands of road data), returns {"error": "no_route"} rather than
    raising. If the extraction graph hit its internal size cap, the result
    carries "truncated": true — the route may be suboptimal or incomplete.

    include_path=true adds "path", a GeoJSON LineString from the origin's
    snapped node to the destination's that follows the streets' own
    geometry (curves included), simplified to fit the token budget, with
    "path_max_deviation_m" bounding how far it strays from the exact
    street path. Off by default (the polyline dwarfs the rest of the
    answer) — ask for it only to draw or trace the route. If even a fully
    simplified line won't fit, you get "path_omitted": true instead of a
    line that stops short of the destination.
    """
    for lat, lon in ((from_lat, from_lon), (to_lat, to_lon)):
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    try:
        return routing.route(
            from_lat, from_lon, to_lat, to_lon, mode=mode, include_path=include_path
        )
    except routing.UnsupportedMode:
        return {"error": "unsupported_mode", "supported": sorted(routing.MODE_CONFIG)}
    except routing.UpstreamUnavailable as e:
        return _upstream_error(e)
    except routing.SchemaDegraded as e:
        return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}
    except routing.NoGraphNearby as e:
        return {"error": "no_graph_nearby", "detail": e.detail}
    except routing.RouteTooLong as e:
        return {
            "error": "route_too_long",
            "detail": e.detail,
            "max_distance_m": e.max_distance_m,
        }
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}


@_tool("Places along a route")
def places_along_route(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    mode: str = "drive",
    category: str | None = None,
    name: str | None = None,
    max_detour_m: float = routing.CORRIDOR_DEFAULT_DETOUR_M,
    limit: int = 10,
) -> dict:
    """Places on the way from A to B: corridor search along the route.

    Answers "find a coffee shop on my drive to the airport" — the route tool
    plus find_places in one call. Builds the same street-graph shortest path
    `route` returns, then finds places whose nearest point on that path is
    within max_detour_m (default 1000m, capped at 5000m; larger values
    return a bad_request error rather than being silently clamped).

    Each result row is a find_places row plus two numbers: detour_m, the
    straight-line distance to the route doubled — an approximation of the
    round trip off and back on, not a re-routed detour — and along_m, how
    far along the route from the origin that place sits, so "roughly
    halfway" is answerable. Results are ordered by along_m (route order,
    reading as an itinerary) rather than by detour cost. When more than
    limit places are on the way, the response is an even sample spanning the
    whole route — never just the first limit, which would drop the far end
    of the journey — and carries "truncated": true saying so. It also
    carries {"route": {"distance_m", "duration_s", "mode"}} for the
    underlying route.

    category and name narrow the search exactly as they do in find_places
    (category matches Overture's taxonomy, e.g. 'coffee_shop'; name is a
    substring match) — worth passing on a long route, since an unfiltered
    corridor through a dense area can hold more places than the search
    considers, in which case the response carries "truncated": true and a
    note saying so.

    mode is "walk", "cycle", or "drive" (default), with the same cost model
    and the same straight-line-distance caps as `route`, and the same
    structured errors: route_too_long, no_graph_nearby, no_route,
    unsupported_mode, and bad_request for non-finite/out-of-range
    coordinates or an invalid max_detour_m.
    """
    for lat, lon in ((from_lat, from_lon), (to_lat, to_lon)):
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    try:
        result = routing.places_along_route(
            from_lat, from_lon, to_lat, to_lon,
            mode=mode, category=category, name=name,
            max_detour_m=max_detour_m, limit=limit,
        )
    except routing.UnsupportedMode:
        return {"error": "unsupported_mode", "supported": sorted(routing.MODE_CONFIG)}
    except routing.UpstreamUnavailable as e:
        return _upstream_error(e)
    except (routing.SchemaDegraded, overture.SchemaDegraded) as e:
        return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}
    except routing.NoGraphNearby as e:
        return {"error": "no_graph_nearby", "detail": e.detail}
    except routing.RouteTooLong as e:
        return {
            "error": "route_too_long",
            "detail": e.detail,
            "max_distance_m": e.max_distance_m,
        }
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    if "error" in result:
        return result
    payload = _with_degraded_fields(budget.apply_budget(result, "results"))
    return _with_category_hint(payload, category, widen_hint="widen max_detour_m")


@_tool("Best visiting order for stops")
def optimize_route(
    stops: list[dict],
    mode: str = "drive",
    roundtrip: bool = True,
    start_index: int = 0,
) -> dict:
    """Best order to visit several stops: multi-stop route ordering (a small TSP).

    Answers "I have these five errands, what order costs least" — stops is a
    list of 2-10 {"lat": ..., "lon": ..., "name": ... (optional)} points, and
    the answer is the cheapest visiting order over the real street graph, not
    a straight-line guess. Solved exactly (Held-Karp over the routed cost
    matrix), so it is the optimum, not a nearest-neighbour approximation.

    Returns {"order": [stop indices, in visiting order], "legs":
    [{"from_idx", "to_idx", "distance_m", "duration_s"}, ...],
    "total_distance_m", "total_duration_s", "mode", "roundtrip"} — indices
    refer to the input `stops` list, and there is no polyline/geometry — for
    a single pair's numbers on their own, call `route`.

    start_index (default 0) is fixed as the first stop. roundtrip=true (the
    default) returns to it; the closing leg is in "legs" but the start is not
    repeated in "order". roundtrip=false is an open path that ends wherever
    is cheapest. mode is "walk", "cycle" or "drive" (default), on the same
    cost model every routing tool uses; one-ways make the drive/cycle cost
    matrix asymmetric and that is solved for exactly. The objective minimized
    is total duration.

    If some pair of stops has no route between them (disconnected road data),
    the call still succeeds: that leg's numbers are a straight-line estimate,
    the leg carries "estimated": true, and the response carries
    "estimated": true plus a note naming the estimated legs — so a flagged
    approximation, never a crash.

    Errors are structured, not raised: fewer than 2 or more than 10 stops, a
    stop missing numeric lat/lon, or an out-of-range start_index return
    {"error": "bad_request"} naming the offending stop index; an unknown mode
    returns {"error": "unsupported_mode"}; a stop set whose two furthest-apart
    stops are further apart than the mode's straight-line cap (see `route`)
    returns {"error": "route_too_long"}; a stop with no usable street node near it returns
    {"error": "no_graph_nearby"} naming that stop's index.
    """
    if not isinstance(stops, list):
        return {"error": "bad_request", "detail": "stops must be a list of {lat, lon} points"}
    if not (routing.OPTIMIZE_MIN_STOPS <= len(stops) <= routing.OPTIMIZE_MAX_STOPS):
        return {
            "error": "bad_request",
            "detail": (
                f"stops must hold between {routing.OPTIMIZE_MIN_STOPS} and "
                f"{routing.OPTIMIZE_MAX_STOPS} points, got {len(stops)}"
            ),
        }
    points = []
    for idx, stop in enumerate(stops):
        try:
            lat, lon = float(stop["lat"]), float(stop["lon"])
        except (KeyError, TypeError, ValueError) as e:
            return {
                "error": "bad_request",
                "detail": f"stops[{idx}]: each stop needs numeric lat and lon: {e}",
            }
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            coord_error["detail"] = f"stops[{idx}]: {coord_error['detail']}"
            return coord_error
        points.append((lat, lon))
    if not isinstance(start_index, int) or isinstance(start_index, bool):
        return {"error": "bad_request", "detail": "start_index must be an integer"}
    if not 0 <= start_index < len(points):
        return {
            "error": "bad_request",
            "detail": f"start_index={start_index} is out of range for {len(points)} stops",
        }

    try:
        return routing.optimize_route(
            points, mode=mode, roundtrip=bool(roundtrip), start_index=start_index
        )
    except routing.UnsupportedMode:
        return {"error": "unsupported_mode", "supported": sorted(routing.MODE_CONFIG)}
    except routing.UpstreamUnavailable as e:
        return _upstream_error(e)
    except routing.SchemaDegraded as e:
        return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}
    except routing.NoGraphNearby as e:
        return {"error": "no_graph_nearby", "detail": e.detail}
    except routing.RouteTooLong as e:
        return {
            "error": "route_too_long",
            "detail": e.detail,
            "max_distance_m": e.max_distance_m,
        }
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}


@_tool("Data version")
def data_version() -> dict:
    """Which Overture Maps release backs the answers from every other tool.

    Reports the active release string, its date, and whether it came from
    live S3 discovery, an operator env override, or the pinned fallback
    baked into this build. Resolved once at process start and cached for
    the process lifetime — this tool just reports that cached value, it
    doesn't re-check upstream, so it's small and has no upstream DB
    dependency.

    The body is resources.data_version_payload(), shared verbatim with the
    placeroot://data-version MCP resource so the two surfaces cannot drift
    (issue #195); tests/test_resources.py asserts they stay equal.
    """
    return resources.data_version_payload()


# ---------------------------------------------------------------------------
# PLACEROOT_TOOLS=progressive: the meta surface (issue #210).
# ---------------------------------------------------------------------------


def _arg_summary(fn: Callable) -> str:
    """A tool's parameters as `required,optional?` — the catalog's arg column.

    Names only, no types: the catalog's budget is the whole point (28 tools
    have to fit in well under 1k tokens), and the names here are already
    self-describing (lat, radius_m, limit, category). A caller that guesses
    a type wrong gets placeroot_call's bad_request naming what the tool
    accepts, which is cheaper than paying for the types on every catalog
    read.
    """
    return ",".join(
        name if param.default is inspect.Parameter.empty else f"{name}?"
        for name, param in inspect.signature(fn).parameters.items()
    )


@functools.cache
def _arg_metadata(fn: Callable):
    """The tool's own argument model — the one the SDK validates calls against.

    `server.tool()(fn)` builds exactly this via func_metadata when the tool is
    registered, so validating dispatch through it is not a second, parallel
    notion of what the tool accepts: a value that reaches the function through
    placeroot_call has been through the same coercions (str "5" -> int 5,
    a JSON-string list -> list) as one that arrives over tools/call. Built
    once per function and cached, because the model construction is the
    expensive part and the tools are a fixed set.
    """
    return func_metadata(fn)


def _validation_detail(e: ValidationError) -> str:
    """A pydantic ValidationError as one line of advice per bad argument.

    The raw repr carries a URL, an input echo and a type tag per error —
    tokens a caller retrying the call cannot use. Field name plus message is
    what tells it which argument to fix and to what.
    """
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'args'}: {err['msg']}" for err in e.errors()
    )


def _catalog_entry(name: str, fn: Callable) -> str:
    """`name(args) one-line summary` for one tool.

    The summary is the first paragraph of the tool's own docstring — the
    same text whose full version is the tool's MCP description on the full
    surface — so the catalog cannot describe a tool differently from how
    the tool describes itself, and a reworded tool updates here for free.
    """
    doc = inspect.getdoc(fn) or ""
    summary = " ".join(doc.split("\n\n")[0].split())
    return f"{name}({_arg_summary(fn)}) {summary}"


@_tool("PlaceRoot capabilities", meta=True)
def placeroot_capabilities() -> dict:
    """List every PlaceRoot tool: what it answers and what arguments it takes.

    Read this once, then call anything it lists through placeroot_call.
    The catalog is generated from the tools themselves, so it is always the
    complete set for this build.
    """
    return {
        "tools": [_catalog_entry(name, fn) for name, fn in _TOOL_FUNCS.items()],
        "count": len(_TOOL_FUNCS),
        "usage": (
            "Call any of these with placeroot_call(tool=<name>, args={...}). "
            "A trailing ? marks an optional argument; everything else is required. "
            "Answers come back exactly as the tool would return them directly."
        ),
    }


@_tool("Call a PlaceRoot tool", annotations=_DISPATCH_ANNOTATIONS, meta=True)
def placeroot_call(tool: str, args: dict | None = None) -> dict:
    """Run one PlaceRoot tool by name, with its arguments.

    `tool` is a name from placeroot_capabilities; `args` is an object of that
    tool's arguments (omit it for the ones that take none). The answer is the
    tool's own, unchanged. An unknown name comes back as an error listing the
    valid ones.
    """
    fn = _TOOL_FUNCS.get(tool)
    if fn is None:
        return {
            "error": "unknown_tool",
            "detail": (
                f"{tool!r} is not a PlaceRoot tool. Call placeroot_capabilities "
                "for what each one does and what it takes."
            ),
            "valid_tools": sorted(_TOOL_FUNCS),
        }
    call_args = {} if args is None else args
    if not isinstance(call_args, dict):
        return {
            "error": "bad_request",
            "detail": (
                f"args must be an object of {tool}'s arguments, got "
                f"{type(call_args).__name__}; it accepts: {_arg_summary(fn)}"
            ),
        }
    # Bind before calling so a wrong/missing/misspelled argument is reported
    # as this dispatcher's bad_request. Calling fn(**args) blind would let
    # the same TypeError arrive from inside the tool's own body, where it
    # would surface as a crash rather than as advice about the arguments.
    # Binding is names-only, so the types go through the tool's own pydantic
    # model below — the same one the SDK validates a direct call against.
    try:
        inspect.signature(fn).bind(**call_args)
    except TypeError as e:
        return {"error": "bad_request", "detail": f"{tool}: {e}", "accepts": _arg_summary(fn)}
    try:
        validated = _arg_metadata(fn).validate_arguments(call_args)
    except ValidationError as e:
        return {
            "error": "bad_request",
            "detail": f"{tool}: {_validation_detail(e)}",
            "accepts": _arg_summary(fn),
        }
    return fn(**validated)


_UNSET = object()

# MCP 2026-07-28 caching hints (SEP-2549). The spec requires a `ttlMs` and a
# `cacheScope` on every `resultType: "complete"` listing result; the SDK's
# default is ttlMs=0 ("immediately stale"), which is valid but throws away the
# whole point for a server whose listings are frozen at build time.
#
# Why 24 hours: our listings are a pure function of the installed placeroot
# version and PLACEROOT_TOOLS. Nothing at runtime can change them — no tool is
# registered after startup, and we never send notifications/tools/list_changed
# — so the only event that invalidates a cached listing is the operator
# upgrading the package. TTL is therefore a bound on how long a client could
# keep showing a pre-upgrade tool list, and one day is the honest trade: it
# spares a re-fetch of a ~12.2k-token schema surface on every session within a
# day, while an upgrade is visible by the next one. A week would buy almost
# nothing extra (sessions cluster well inside a day) for seven times the
# staleness window; 0 is what we'd declare if the surface could move at
# runtime, and it can't.
#
# Why "public": these listings carry no caller-specific data. PlaceRoot is
# keyless, does no per-caller filtering, and returns the same bytes to every
# request on a given process, so a shared gateway may serve one caller's copy
# to another.
#
# Two of the six cacheable methods are deliberately left at the SDK default
# (ttlMs=0/private), for the same reason: their bodies carry the resolved
# Overture release, which is discovered from S3 at process start rather than
# baked into the build, so a day-long shared cache could outlive the value.
#   `resources/read` — placeroot://data-version reports the release directly.
#   `server/discover` — its DiscoverResult carries `instructions`, and main()
#       appends "Backed by Overture Maps release {release}." to those at
#       startup (the SDK's default handler reads them at call time). A 24h
#       public entry would keep serving the pre-restart release string — to
#       other callers too, under "public" — after an operator restarts onto a
#       new Overture release, and that string is model-visible grounding.
_LISTING_TTL_MS = 24 * 60 * 60 * 1000
_LISTING_CACHE_HINT = CacheHint(ttl_ms=_LISTING_TTL_MS, scope="public")
CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": _LISTING_CACHE_HINT,
    "prompts/list": _LISTING_CACHE_HINT,
    "resources/list": _LISTING_CACHE_HINT,
    "resources/templates/list": _LISTING_CACHE_HINT,
}


def build_server(spec=_UNSET) -> MCPServer:
    """An MCPServer with the PLACEROOT_TOOLS-selected subset registered.

    `spec` defaults to reading the env var, and is a parameter only so
    tests can build a server for a given selection without touching the
    process environment. See tool_profiles.py for the grammar; None means
    "unset", i.e. the full surface.
    """
    if spec is _UNSET:
        spec = os.environ.get("PLACEROOT_TOOLS")
    selected = tool_profiles.resolve(spec, set(_TOOL_FUNCS))
    # cache_hints are filled in by the SDK's response serializer and sieved
    # out again for pre-2026-07-28 clients, so an older client's tools/list is
    # byte-identical to what it got before this existed (asserted in
    # tests/test_caching.py).
    server = MCPServer("placeroot", instructions=BASE_INSTRUCTIONS, cache_hints=CACHE_HINTS)
    # One registry for the loop: `progressive` selects meta-tool names, every
    # other selection selects only real ones, so the two never mix in a
    # single server even though they are registered by the same code.
    for name, fn in {**_TOOL_FUNCS, **_META_TOOL_FUNCS}.items():
        if name in selected:
            # Title + hints are applied here, once, for every selected tool —
            # so a subset profile (PLACEROOT_TOOLS=core) is annotated exactly
            # like the full surface.
            server.tool(
                title=_TOOL_TITLES[name],
                annotations=_TOOL_ANNOTATIONS.get(name, _READ_ONLY_ANNOTATIONS),
            )(fn)
    # Resources are registered whatever the selection: they never appear in
    # tools/list, so they cost a subset install nothing, and
    # placeroot://data-version is worth having precisely when the
    # data_version tool was left out of the selection (#195).
    resources.register(server)
    # One line at startup naming what got registered. An empty or
    # whitespace-only PLACEROOT_TOOLS is legal and means "everything", which
    # is indistinguishable from a subset that silently didn't apply unless
    # the server says which it did.
    # Prompts are registered whatever the selection: they are not part of
    # tools/list, so they cost a subset install nothing, and a workflow is
    # still worth reading when one of its steps is unavailable. Each one
    # renders a note naming the tools this selection left out (#194).
    #
    # Under `progressive` nothing is left out — every tool is reachable
    # through placeroot_call — so the prompts are told the full surface is
    # available. Passing the three meta-tool names instead would put a note
    # on every prompt disowning the tools its own steps depend on.
    reachable = set(_TOOL_FUNCS) if selected & tool_profiles.PROGRESSIVE_TOOLS else selected
    prompts.register(server, reachable)
    requested = (spec or "").strip() or tool_profiles.ALL
    logger.info(
        "registered %d of %d tools (PLACEROOT_TOOLS=%s)",
        len(selected),
        len(_TOOL_FUNCS),
        requested,
    )
    return server


try:
    mcp = build_server()
except tool_profiles.InvalidToolSelection as e:
    # Fail fast, at import, with the message and nothing else — an operator
    # who typo'd a profile name gets told which names are valid rather than
    # a traceback, and never a server that quietly loaded everything.
    raise SystemExit(f"placeroot: {e}") from e


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
