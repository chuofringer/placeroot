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
import asyncio
import contextvars
import functools
import importlib.metadata
import inspect
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from placeroot import (
    addresses,
    area_score,
    area_suggest,
    budget,
    buildings,
    cache,
    categories,
    changes,
    db,
    divisions,
    elevation,
    errors,
    export,
    geo,
    geometry_ops,
    geometry_setops,
    gers,
    ground,
    home_region,
    honesty,
    infrastructure,
    land_use,
    mapexplain,
    mapview,
    meeting,
    output_schemas,
    overture,
    progress,
    prompts,
    release,
    resources,
    routing,
    simplify,
    tool_profiles,
    trace,
    transit,
    verdict,
    water,
)
from placeroot import claims as claim_checks
from placeroot import cursor as cursor_mod
from placeroot import geocode as geocoding
from placeroot import map_match as mapmatch
from placeroot import (
    preferences as preference_store,
)
from placeroot import (
    timezone as timezone_lookup,
)

logger = logging.getLogger(__name__)

BASE_INSTRUCTIONS = (
    "Grounds spatial questions in Overture Maps open data. "
    "Answers are compact and ranked; distances in meters. Responses "
    "that don't fit the token budget carry truncated: true — narrow "
    "the query (smaller radius, a category or name filter) instead of "
    "raising limit.\n\n"
    "TELL THIS SERVER WHERE. You know things it cannot: that "
    "\"san jose airport\" means San Jose, California, that the Eiffel "
    "Tower is in Paris, that a question about a school names a place in "
    "some particular suburb. This server knows only what exists at which "
    "coordinates in the current Overture release — it has no world "
    "knowledge to fall back on. So pass the location separately from the "
    "thing being looked for: near_lat/near_lon when you have "
    "coordinates, otherwise city (\"San Jose, CA\"). Sending one "
    "combined string forces the server to guess which words are the "
    "place, and it guesses from map data alone, where \"san\" names a "
    "division in Henan.\n"
    "A hint only bounds where the search looks; every row returned still "
    "comes from the data, so a wrong hint costs a retry, never a wrong "
    "answer. When a reply carries need: \"location\" it could not bound "
    "the search at all — retry with the city rather than rephrasing.\n"
    "Actionable place rows carry trust_note, a calibrated before-you-go "
    "clause from confidence and operating status. Composed itineraries add "
    "verify_before_going naming the 1–2 stops most worth checking — surface "
    "both. Users forgive missing data; they do not forgive a shuttered café.\n\n"
    "The first query over a new city is the slow one (a cold scan of "
    "public S3 data). Resolving a city starts background tile warming "
    "so later place searches over that metro read locally. Tiles are "
    "not a built street graph — the first walk still builds or loads "
    "the graph; later walks reuse it. Optional warmup_city pays the "
    "tile cost inline if you want to wait.\n\n"
    "Named walks and \"X near Y\" are one hop each: from_to() "
    "and find_near() accept place names. Do not chain "
    "geocode + route or resolve + find_places for those questions."
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
# preferences() writes or deletes a local JSON file. readOnlyHint is
# what clients gate auto-approval on, so a write must not claim a lookup.
# destructive_hint is true because clear=true unlinks the file.
# A repeated identical update leaves the same document, so
# idempotent_hint is true.
_PREFERENCES_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)

_TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {}

# Annotated aliases that put an enum + a stated default into the published
# schema without changing runtime validation: the type stays plain
# `str | None`, so a bad string still reaches the function and comes back
# as a structured unsupported_mode/bad_request error (see CONTRIBUTING
# design rule 2 — a Literal would reject it before the self-correcting
# error ever ran, and schema tokens are a budget so these are shared
# rather than repeated per tool).
_MODE_ENUM = sorted(preference_store.MODES)
_ModeArgWalkDefault = Annotated[
    str | None,
    Field(
        description="Travel mode. Default: stored preference, else walk.",
        json_schema_extra={"enum": _MODE_ENUM},
    ),
]
_ModeArgDriveDefault = Annotated[
    str | None,
    Field(
        description="Travel mode. Default: stored preference, else drive.",
        json_schema_extra={"enum": _MODE_ENUM},
    ),
]
_PreferArg = Annotated[
    str | None,
    Field(
        description="Grade preference. Default: none (plain-distance routing).",
        json_schema_extra={"enum": sorted(routing.SUPPORTED_PREFERENCES)},
    ),
]
# #425: the enum lives on `items` (this is an array), and the runtime type
# stays a plain list so an unsupported class reaches the function and comes
# back as a self-correcting bad_request naming the supported values.
_AvoidArg = Annotated[
    list | None,
    Field(
        description="Road classes to keep the route off. No toll or ferry option exists: "
        "Overture carries no toll attribute, and the graph is road-only. "
        "Default: none (no class avoided).",
        json_schema_extra={"items": {"type": "string", "enum": list(routing.AVOIDABLE_CLASSES)}},
    ),
]
# neighborhood_verdict doesn't consult stored preferences; its default is
# inferred from free-text context (no car -> walk, bike -> cycle, car ->
# drive), falling back to walk.
_ModeArgContextDefault = Annotated[
    str | None,
    Field(
        description="Travel mode override. Default: inferred from context, else walk.",
        json_schema_extra={"enum": _MODE_ENUM},
    ),
]
# compare_modes' subset (#459): enum on `items` like _AvoidArg, runtime type a
# plain list so an unknown mode comes back as a self-correcting bad_request.
_CompareModesArg = Annotated[
    list | None,
    Field(
        description="Modes to compare, in answer order. Default: walk, cycle, drive.",
        json_schema_extra={"items": {"type": "string", "enum": _MODE_ENUM}},
    ),
]
_ModeSetArg = Annotated[
    str | None,
    Field(
        description="Travel mode to store. Omit to leave it unchanged.",
        json_schema_extra={"enum": _MODE_ENUM},
    ),
]
# #410: no fixed enum — a language code is validated by shape (2-3 lowercase
# letters), not membership in a closed list the way mode is, since Overture's
# names.common keys are not a small fixed set.
_LangArg = Annotated[
    str | None,
    Field(
        description="Result-language code (2-3 lowercase letters, e.g. \"de\"). "
        "Overture-tagged name variants only — never transliterated or invented. "
        "Default: stored preference, else the primary name.",
    ),
]
_OperatingStatusArg = Annotated[
    str | None,
    Field(
        description="Business-lifecycle status filter (relabeled or raw Overture value, "
        "case-insensitive). Default: no filter.",
        json_schema_extra={"enum": overture.accepted_operating_status_values()},
    ),
]
_CursorArg = Annotated[
    str | None,
    Field(
        description="Continuation cursor from a previous truncated answer; valid for the "
        "same query on the same data release.",
    ),
]
_DETAIL_ENUM = ["ids", "compact", "full"]
_DetailArg = Annotated[
    str | None,
    Field(
        description="Row detail tier for find_places rows: 'ids' (id + distance_m only), "
        "'compact' (id/name/category/lat/lon/distance_m/trust), or 'full' (every field, "
        "incl. trust_note prose). Default: compact.",
        json_schema_extra={"enum": _DETAIL_ENUM},
    ),
]

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


def _with_within_extras(payload: dict, resolved_echo: dict | None, note: str | None) -> dict:
    """Attach find_places' `within` reachability-filter extras (roadmap §4.2):
    the compact "resolved" echo when `of` was an id/name, and a short
    honesty note that the answer was filtered against the street graph —
    joined onto any note already present, not clobbering it (same
    convention as _with_category_hint)."""
    if resolved_echo is not None:
        payload["resolved"] = resolved_echo
    if note:
        payload["note"] = "; ".join(filter(None, [payload.get("note"), note]))
    return payload


def _with_center_resolved(
    payload: dict, center_echo: dict | None, within_echo: dict | None
) -> dict:
    """Attach find_places' compact "resolved" echo for a `where` search center.

    "resolved" belongs to the center: that is the point the caller named,
    and the one whose ambiguity they need to see. `within.of` writes to the
    same key (_with_within_extras), so when both were given as an id/name
    the `of` match moves into the note rather than being dropped — two
    different points can't share one echo, and inventing a second key to
    hold a redundant `of` (it defaults to the center anyway) would cost
    every other answer nothing but schema.
    """
    if center_echo is None:
        return payload
    if within_echo is not None:
        label = within_echo.get("name") or f"({within_echo['lat']}, {within_echo['lon']})"
        payload["note"] = "; ".join(
            filter(None, [payload.get("note"), f"within.of resolved to {label}"])
        )
    payload["resolved"] = center_echo
    return payload


def _project_place_row(row: dict, detail: str) -> dict:
    """Shrink one find_places row to `detail`'s tier (roadmap §4.5).

    "full" is today's row, unchanged. "ids" is the cheap-chaining shape
    (id + distance_m, when present) — deliberately coordinate-free, since
    it exists purely to feed an id into a batch lookup or place_details.
    "compact" — the default — is {id, name, category, lat, lon,
    distance_m, trust}: it keeps lat/lon so the advertised find_places ->
    render_map composition (#386) still renders under the new default,
    per ROADMAP §5.3's output schema listing lat/lon as required row
    fields. `trust` is a tier string derived by honesty.trust_tier from
    the same signals as trust_note, so it can never disagree with the
    prose a "full" row would carry. distance_m is omitted entirely (not
    null) on division-polygon rows, which have no reference point to
    measure distance from — those rows still carry lat/lon, in every
    tier but "ids".
    """
    if detail == "full":
        return row
    if detail == "ids":
        out = {"id": row.get("id")}
        if "distance_m" in row:
            out["distance_m"] = row["distance_m"]
        return out
    out = {
        "id": row.get("id"),
        "name": row.get("name"),
        "category": row.get("category"),
    }
    if "lat" in row:
        out["lat"] = row["lat"]
    if "lon" in row:
        out["lon"] = row["lon"]
    if "distance_m" in row:
        out["distance_m"] = row["distance_m"]
    out["trust"] = honesty.trust_tier(row)
    return out


def _project_places(rows: list[dict], detail: str) -> list[dict]:
    return [_project_place_row(r, detail) for r in rows]


def _project_grouped_places(grouped: dict[str, list[dict]], detail: str) -> dict[str, list[dict]]:
    return {slug: _project_places(rows, detail) for slug, rows in grouped.items()}


def _with_detail_legend(payload: dict, detail: str) -> dict:
    """Attach the one-line trust-tier legend when compact rows carry a
    bare `trust` tier instead of full rows' self-explaining trust_note."""
    if detail == "compact":
        payload["trust_legend"] = honesty.TRUST_LEGEND
    return payload


def _with_categories_hint(
    payload: dict, categories: list[str] | None, widen_hint: str, list_key: str = "results"
) -> dict:
    """categories analog of _with_category_hint (#117 / roadmap §4.5): a
    non-fatal "note" when the scan matched none of the requested slugs at
    all. Only fires when EVERY requested category came back empty — at
    that point naming all of them is cheap and correct, since none of them
    could possibly have matched anything.
    """
    if not categories:
        return payload
    rows = payload.get(list_key)
    empty = not rows if isinstance(rows, list) else not any((rows or {}).values())
    if empty:
        slugs = ", ".join(repr(s) for s in categories)
        hint = (
            f"no places matched any of categories [{slugs}] here; if a slug may not "
            "be a valid Overture category slug, use search_categories to find the "
            f"right one, or {widen_hint} / drop the categories filter."
        )
        payload["note"] = "; ".join(filter(None, [payload.get("note"), hint]))
    return payload


def _with_name_fallback_note(
    payload: dict, name: str | None, rows: list[dict] | None = None
) -> dict:
    """Add a non-fatal "note" when a result came from #373's alt-name/fuzzy
    fallback tiers rather than a literal name match.

    overture.find_places (and, through it, resolve_place) tags a fallback
    row with "matched_by": "alt_name" | "fuzzy" but has no top-level object
    of its own to carry a note on — this is where that note gets attached,
    mirroring _with_category_hint's join-not-clobber convention so an
    existing truncation/category note survives alongside it.

    `rows` defaults to payload["results"], but find_places' detail
    projection (ROADMAP §4.5) can strip matched_by/name from that list
    before this runs — callers that project pass the pre-projection rows
    explicitly so the note still has something to read.
    """
    if rows is None:
        rows = payload.get("results") or []
    fallback = next((r for r in rows if r.get("matched_by")), None)
    if fallback is not None:
        via = (
            "an alternate spelling" if fallback["matched_by"] == "alt_name"
            else "the closest spelling match"
        )
        hint = f"no exact match for name {name!r}; showing {fallback['name']!r} via {via}."
        payload["note"] = "; ".join(filter(None, [payload.get("note"), hint]))
    return payload


# Roadmap §4, next tier: not_found from a name-resolution dead end names the
# next move rather than leaving the caller to guess. resolve_place carries
# its own "need"/"retry_with" sketch for the same situation (a plain
# `resolve_place()` call with no rows) — this "try" string is for the
# tools that resolve a name internally and cannot offer that structured
# retry, since they don't expose the intermediate resolve step for the
# caller to redo more specifically. core (the default profile) always
# carries resolve_place and geocode, so naming them here holds for the
# common case; a narrower PLACEROOT_TOOLS selection may not register one.
_NAME_NOT_FOUND_TRY = (
    "resolve_place with near_lat/near_lon or city to disambiguate; "
    "or geocode for street addresses"
)


def _resolve_named_place(query: str) -> dict:
    """A free-text name -> compact {name, lat, lon, id, type} or an error.

    Shared by from_to and find_near. Ambiguous same-score names return
    candidates instead of silently picking a city. An unresolvable name
    returns {"error": "not_found", "detail", "try"} — "try" names the next
    move (roadmap §4). A comma-qualified name whose qualifier resolved but
    held nothing (#427) says so by naming the qualifier it searched,
    instead of reporting a same-ish name from the other side of the world.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "bad_request", "detail": "place name must be a non-empty string"}
    query = query.strip()
    try:
        resolved = geocoding.resolve_named_place(query)
    except errors.AnchoredNotFound as e:
        return {"error": "not_found", "detail": e.detail, "try": _NAME_NOT_FOUND_TRY}
    except errors.AmbiguousPlace as e:
        return {
            "error": "ambiguous_place",
            "detail": e.detail,
            "query": e.query,
            "candidates": e.candidates,
        }
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    if resolved is None:
        return {
            "error": "not_found",
            "detail": f"no place matched {query!r}",
            "try": _NAME_NOT_FOUND_TRY,
        }
    return resolved


def _resolve_pair(a: str, b: str) -> tuple[dict, dict]:
    """Resolve two names in parallel, each on its own cursor.

    db.isolated_reads gives each worker a private cursor and lock so the
    two resolves genuinely overlap instead of serializing on the shared
    conn lock (#328's parallel-inside-the-compose requirement).

    Workers do not inherit contextvars, so copy the request context into
    each submit — otherwise progress.report from a cold resolve lands in
    a throwaway per-thread log and never reaches attach().
    """

    def _isolated(query: str) -> dict:
        with db.isolated_reads():
            return _resolve_named_place(query)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(contextvars.copy_context().run, _isolated, a)
        fb = pool.submit(contextvars.copy_context().run, _isolated, b)
        return fa.result(), fb.result()


# Real GERS ids are 32 lowercase hex characters (gers.py's module docstring
# and _validate_id's own comment). Deliberately stricter than gers.py's own
# ID_CHARSET_RE, which has to admit synthetic fixture ids like
# "gers-div-brooklyn" for gers_lookup's own tests — LocationRef needs the
# opposite bias: a free-text name must never be misread as an id, so this
# only recognizes the one shape a real id actually has. Case-insensitive
# since nothing here depends on it and rejecting a same-shape uppercase id
# would just cost the caller a confusing not_found.
_GERS_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)

_LOCATION_REF_BAD_REQUEST = {
    "error": "bad_request",
    "detail": (
        "location must be one of: {\"lat\": ..., \"lon\": ...} with numeric lat/lon in "
        "range, a GERS id string (32 hex characters), or a non-empty free-text place name"
    ),
}


def _resolve_location_ref(ref) -> tuple[dict | None, dict | None]:
    """A LocationRef ({lat,lon} | GERS id string | free-text name) -> (resolved, error).

    Exactly one of the two return values is not None. `resolved` always
    carries numeric lat/lon; a coordinate dict passes through untouched (no
    `id`/`name`/`matched_by` — nothing to echo back, on purpose: raw
    coordinate input must not grow the answer). A string input additionally
    carries `id`, `name`, and `matched_by` ("gers_id" or "name") so callers
    can build the compact `resolved` echo the roadmap calls for.

    `error` is a structured envelope ready to return as-is (bad_request,
    not_found, ambiguous_place, or an upstream/schema failure) — callers
    that resolve several refs prefix its `detail` with the failing index.
    A not_found also carries "try" naming the next move (roadmap §4).
    """
    if isinstance(ref, dict):
        lat, lon = ref.get("lat"), ref.get("lon")
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return None, _LOCATION_REF_BAD_REQUEST
        return {"lat": float(lat), "lon": float(lon)}, None
    if isinstance(ref, str):
        text = ref.strip()
        if not text:
            return None, _LOCATION_REF_BAD_REQUEST
        if _GERS_ID_RE.match(text):
            try:
                hit = gers.gers_lookup(text)
            except ValueError:
                hit = None
            except overture.UpstreamUnavailable as e:
                return None, _upstream_error(e)
            except overture.SchemaDegraded as e:
                return None, _schema_error(e)
            if hit is None or hit.get("lat") is None or hit.get("lon") is None:
                return None, {
                    "error": "not_found",
                    "detail": (
                        f"{text!r} looked like a GERS id; no feature has it in this release"
                    ),
                    "try": (
                        "resolve_place or geocode to find the right id; "
                        "or pass a {\"lat\", \"lon\"} location instead"
                    ),
                }
            return {
                "id": hit.get("id"),
                "name": hit.get("name"),
                "lat": hit["lat"],
                "lon": hit["lon"],
                "matched_by": "gers_id",
            }, None
        hit = _resolve_named_place(text)
        if "error" in hit:
            return None, hit
        item = {
            "id": hit.get("id"),
            "name": hit.get("name"),
            "lat": hit["lat"],
            "lon": hit["lon"],
            "matched_by": "name",
        }
        if hit.get("note"):
            # #427: the name carried a qualifier that resolved to nothing,
            # so the whole string was searched instead. Non-fatal, but the
            # caller stated something that was not honored and has to hear
            # it — the echo is where it stays visible.
            item["note"] = hit["note"]
        return item, None
    return None, _LOCATION_REF_BAD_REQUEST


def _resolve_location_refs(refs, param_name: str) -> tuple[list[dict] | None, dict | None]:
    """A list of LocationRefs -> (resolved list, error), same contract as above.

    Items that need network resolution (strings: GERS ids or names) resolve
    in parallel, mirroring _resolve_pair's ThreadPoolExecutor + contextvars
    pattern (workers do not inherit contextvars, so the request context is
    copied into each submit or progress.report from a cold resolve would
    never reach attach()). Plain {lat,lon} dicts need no network round-trip
    and are resolved inline.

    On any failure, returns the error for the lowest-indexed failing item
    (deterministic regardless of which worker finishes first), with
    `"index"` set and `detail` prefixed `f"{param_name}[{i}]: "` so the
    agent can retry that one argument instead of the whole call.
    """
    if not isinstance(refs, list):
        return None, {"error": "bad_request", "detail": f"{param_name} must be a list"}

    resolved: list[dict | None] = [None] * len(refs)
    failures: dict[int, dict] = {}
    network_idxs = [i for i, r in enumerate(refs) if isinstance(r, str)]

    def _isolated(i: int, ref):
        with db.isolated_reads():
            return i, _resolve_location_ref(ref)

    if len(network_idxs) > 1:
        with ThreadPoolExecutor(max_workers=min(len(network_idxs), 8)) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, _isolated, i, refs[i])
                for i in network_idxs
            ]
            for future in futures:
                i, (item, err) = future.result()
                if err is not None:
                    failures[i] = err
                else:
                    resolved[i] = item
        pending = [i for i in range(len(refs)) if i not in network_idxs]
    else:
        pending = list(range(len(refs)))

    for i in pending:
        item, err = _resolve_location_ref(refs[i])
        if err is not None:
            failures[i] = err
        else:
            resolved[i] = item

    if failures:
        idx = min(failures)
        err = failures[idx]
        detail = f"{param_name}[{idx}]: {err.get('detail', '')}"
        return None, {**err, "index": idx, "detail": detail}
    return resolved, None


def _location_ref_echo(item: dict) -> dict:
    """The compact {name, id, lat, lon, matched_by} block for a resolved string LocationRef.

    Only called for items that carry `matched_by` — coordinate inputs never
    reach this (see _resolve_location_ref's contract), which is what keeps
    the `resolved` echo absent for pure-coordinate calls. Plus `note` when
    the resolution has something non-fatal to disclose (#427).
    """
    echo = {
        "name": item.get("name"),
        "id": item.get("id"),
        "lat": item["lat"],
        "lon": item["lon"],
        "matched_by": item["matched_by"],
    }
    if item.get("note"):
        echo["note"] = item["note"]
    return echo


def _category_slug(category: str) -> str:
    """A slug, or the top search_categories hit for short free text.

    "coffee_shop" stays a slug. "coffee" / "coffee shops" / "playgrounds"
    map through the taxonomy (singularize, then the first word) so the
    phrases users actually type reach find_places.

    All candidate spellings are tried before settling: an exact taxonomy
    slug wins outright, otherwise the best-confidence search hit across
    every candidate is taken. Stopping at the first candidate that yields
    anything let the raw plural phrase's low-confidence lexical-fallback
    hit shadow the singularized exact slug ("grocery stores" resolved to
    rice_shop instead of grocery_store, #357).
    """
    raw = category.strip()
    if not raw:
        return raw
    candidates = [raw, raw.lower().replace(" ", "_")]
    lower = raw.lower()
    if lower.endswith("s") and len(lower) > 3:
        stem = lower[:-1].strip()
        candidates.append(stem)
        candidates.append(stem.replace(" ", "_"))
    first = raw.split()[0]
    if first.lower() != lower:
        candidates.append(first)
    seen: set[str] = set()
    ordered: list[str] = []
    for cand in candidates:
        key = cand.lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(cand)
    # First pass: any candidate that already is a taxonomy slug wins.
    for cand in ordered:
        slug = cand.lower().replace(" ", "_")
        if categories.hierarchy_for(slug):
            return slug
    # Second pass: best-confidence search hit across all candidates, so a
    # high tier (exact/prefix/substring/path) from a later candidate beats
    # an earlier candidate's lexical-fallback guess. Candidate order still
    # breaks confidence ties.
    best_slug: str | None = None
    best_conf = 0.0
    for cand in ordered:
        hits = categories.search_categories(cand, limit=1)
        if hits and hits[0]["confidence"] > best_conf:
            best_conf = hits[0]["confidence"]
            best_slug = hits[0]["slug"]
    if best_slug is not None:
        return best_slug
    return raw.lower().replace(" ", "_")


_FIND_NEAR_KEYS = (
    "id",
    "name",
    "category",
    "distance_m",
    "lat",
    "lon",
    "trust_note",
    "operating_status",
)


@_tool("Find places")
def find_places(
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = 1000,
    category: str | None = None,
    name: str | None = None,
    min_confidence: float | None = None,
    operating_status: _OperatingStatusArg = None,
    limit: int = 10,
    brand: str | None = None,
    has_website: bool | None = None,
    has_phone: bool | None = None,
    division_id: str | None = None,
    area: str | None = None,
    cursor: _CursorArg = None,
    detail: _DetailArg = None,
    categories: list[str] | None = None,
    group_by_category: bool = False,
    within: dict | None = None,
    confirm: bool = False,
    where: dict | str | None = None,
) -> dict:
    """Find named places, either near a point or inside an area's boundary.

    Three mutually exclusive modes:
    - Point + radius: pass lat and lon (radius_m defaults to 1000m), or the
      same center as `where` — a {"lat", "lon"} dict, a GERS id, or a
      free-text place name ("Alamo Square, SF"), resolved here, so a named
      search is one hop: no geocode()/resolve_place() call first. lat/lon
      or where, not both, and neither with division_id/area. An id/name
      `where` adds a compact "resolved": {"name", "id", "lat", "lon",
      "matched_by"} (absent for lat/lon or a {lat,lon} where); an ambiguous
      name returns {"error": "ambiguous_place", "candidates": [...]} rather
      than picking one. Results are nearest-first around that center.
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
    signal, NOT opening hours; this data has no open-now information —
    and a compact trust_note calibrated from confidence and that status.

    min_confidence (0.0-1.0) keeps only rows whose confidence score is at
    least that value; out-of-range values return a bad_request error.
    operating_status filters to a single status (see the schema's enum for
    accepted relabeled/raw values); "permanently closed"/"closed" also
    match Overture's separate "closed_permanently" raw value, since both
    relabel the same way. Unrecognized values return a bad_request error.

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

    name has two fallback tiers (point + radius mode only, #373) for when
    the literal substring search finds nothing: an alternate-spelling match
    ("Munich" -> a place named "München") then a typo-tolerant fuzzy match
    ("Startbucks" -> "Starbucks"). A row found either way carries
    "matched_by": "alt_name" | "fuzzy" (absent on an ordinary match), and a
    top-level "note" names the spelling actually matched.

    Returns {"results": [...]}, plus truncated/omitted_count if the answer
    didn't fit the token budget. Returns a structured {"error": "bad_request",
    ...} if no mode's inputs are given (or more than one is), {"error":
    "not_found", ...} if division_id/area doesn't match any known division, or
    a structured {"error": ...} if the upstream dataset is unavailable or
    missing columns this tool depends on. If a category filter was given and
    it matched nothing (in either mode), a non-fatal "note" field hints that
    the category slug may be wrong and points at search_categories.

    A truncated answer — either token-budget-trimmed or because more
    matching rows exist beyond limit — also carries "cursor"; pass it back
    unchanged, with every other argument identical, to fetch the next page.
    cursor is only ever issued for a literal or ambiguity-free match: an
    answer built from #373's alt-name/fuzzy name fallback never carries one
    (those pools are small; ROADMAP §4.4). A cursor for a different query,
    or one that's malformed, returns {"error": "bad_cursor", ...} naming
    the mismatch; a cursor issued against an older Overture release is
    honored anyway, against the current release, with a one-line "note"
    that rows may have shifted.

    detail picks how much of each row comes back (ROADMAP §4.5, roadmap
    feature 5): "compact" (the DEFAULT) is {id, name, category, lat, lon,
    distance_m, trust} — trust is a tier string ("strong"/"ok"/"weak"/
    "unknown") derived from the same confidence/operating-status signals
    as "full"'s trust_note, so the two can never disagree; the payload
    also carries one "trust_legend" line explaining the tiers. compact
    keeps lat/lon (unlike "ids") so a composed call feeding these rows
    into a map-rendering tool still has coordinates under the default
    tier. "ids" is just {id,
    distance_m} — deliberately coordinate-free, the cheapest shape, for
    chaining straight into a batch id lookup or place_details. "full" is
    every field, unchanged from before this param existed, including
    trust_note's prose. division-polygon rows have no distance_m at any
    tier (no reference point to measure from) — the key is omitted, never
    null; they still carry lat/lon at every tier but "ids". detail is
    presentation only: it does not affect which rows match or their
    order, is NOT part of a cursor's query identity, and a cursor issued
    under one detail continues correctly under a different one.
    Projection happens before the token budget is applied, so a smaller
    detail tier fits more rows per answer — that's the point of a tier
    smaller than "full". Unrecognized values return a bad_request error
    naming the accepted ones.

    categories (mutually exclusive with category — passing both is a
    bad_request) runs a checklist of up to 5 slugs in ONE scan instead of
    category's one slug, each matched with identical substring/prefix
    semantics. group_by_category=False (the default) merges every
    category's matches into one nearest-first list, same shape and cursor
    pagination as a single category (categories, sorted, is part of the
    cursor's query identity). group_by_category=True instead buckets the
    answer as {"results": {category: [rows...]}}, up to `limit` rows PER
    category — a category with zero matches is simply absent from the
    dict. Grouped answers carry no cursor at all (each category is already
    limit-bounded from a single scan; page a specific one further by
    re-running with category=<that slug> instead). More than 5 slugs, or
    categories together with category, both return a bad_request error;
    group_by_category=True together with a non-null cursor is also a
    bad_request (there is nothing to continue). The category-miss "note"
    (see above) also covers categories: it fires when the scan matched
    none of the requested slugs at all.

    within = {"minutes", "mode"?, "of"?} (roadmap §4.2) keeps only results
    truly reachable from `of` within `minutes` by street-graph `mode` —
    the real graph, not a radius guess; radius_m is ignored when set. `of`
    (a LocationRef) defaults to the search center — lat/lon or whatever
    `where` resolved to; required in division_id/area mode. An id/name `of`
    adds a "resolved" echo, unless `where` already claimed that key, in
    which case `of`'s match is named in the note. The answer gets a short
    reachability note. A cold graph returns
    {"error": "needs_confirm", ...} — retry with confirm=true once the
    user agrees to wait (5-25s).
    """
    point_given = lat is not None or lon is not None
    if where is not None and point_given:
        return {
            "error": "bad_request",
            "detail": "pass either lat and lon, or where — not both",
        }
    modes_given = sum([point_given or where is not None, division_id is not None, area is not None])
    if modes_given > 1:
        return {
            "error": "bad_request",
            "detail": "pass exactly one of lat/lon or where (+radius_m), division_id, or area",
        }
    if modes_given == 0:
        return {
            "error": "bad_request",
            "detail": "pass one of lat/lon or where (+radius_m), division_id, or area",
        }
    if detail is not None and detail not in _DETAIL_ENUM:
        return {
            "error": "bad_request",
            "detail": f"unrecognized detail {detail!r}; accepted values: "
            f"{', '.join(_DETAIL_ENUM)}",
        }
    effective_detail = detail or "compact"
    categories = categories or None
    if category is not None and categories is not None:
        return {
            "error": "bad_request",
            "detail": "pass category or categories, not both",
        }
    if categories is not None and len(categories) > overture.MAX_CATEGORIES:
        return {
            "error": "bad_request",
            "detail": f"categories accepts at most {overture.MAX_CATEGORIES} slugs",
        }
    if group_by_category:
        if categories is None:
            return {
                "error": "bad_request",
                "detail": "group_by_category requires categories",
            }
        if cursor is not None:
            return {
                "error": "bad_request",
                "detail": "group_by_category has no cursor; drop cursor or set "
                "group_by_category=false",
            }

    # `where` becomes lat/lon here, before within and params_key: the rest
    # of the point path — within's "of" default, the cursor's query
    # identity, the scan itself — then sees an ordinary numeric center and
    # needs no knowledge of how it was given. Resolving after the cheap
    # argument checks above keeps a malformed call from paying for a
    # network resolve first.
    center_resolved_echo = None
    if where is not None:
        item, ref_error = _resolve_location_ref(where)
        if ref_error is not None:
            return ref_error
        lat, lon = item["lat"], item["lon"]
        if "matched_by" in item:
            center_resolved_echo = _location_ref_echo(item)

    # within (roadmap §4.2's reachability filter) is resolved to a numeric
    # "of" point BEFORE params_key, below — not the raw LocationRef, which
    # could resolve differently across releases, but the cursor already
    # embeds the release; the numeric point is what makes "same query"
    # stable across a replay. within_minutes/within_mode/within_of_lat/
    # within_of_lon feed the confirm gate and the isochrone call further
    # down, once the cursor itself has been validated.
    within_minutes = None
    within_mode = None
    within_of_lat = None
    within_of_lon = None
    within_resolved_echo = None
    within_canonical = None
    if within is not None:
        if not isinstance(within, dict):
            return {
                "error": "bad_request",
                "detail": "within must be an object with minutes (and optional mode, of)",
            }
        extra_keys = set(within) - {"minutes", "mode", "of"}
        if extra_keys:
            return {
                "error": "bad_request",
                "detail": f"within has unrecognized keys: {sorted(extra_keys)}; "
                "accepted: minutes, mode, of",
            }
        raw_minutes = within.get("minutes")
        try:
            within_minutes = float(raw_minutes)
        except (TypeError, ValueError):
            return {
                "error": "bad_request",
                "detail": f"within.minutes must be numeric, got {raw_minutes!r}",
            }
        if not math.isfinite(within_minutes) or within_minutes <= 0 or within_minutes > 60:
            return {
                "error": "bad_request",
                "detail": "within.minutes must be greater than 0 and at most 60",
            }
        within_mode = preference_store.resolve_mode(
            within.get("mode"), preference_store.DEFAULT_MODE_ISOCHRONE
        )
        if within_mode not in routing.MODE_CONFIG:
            return {
                "error": "unsupported_mode",
                "detail": f"unsupported within.mode {within_mode!r}; supported: "
                f"{sorted(routing.MODE_CONFIG)}",
                "supported": sorted(routing.MODE_CONFIG),
            }
        of_ref = within.get("of")
        if of_ref is not None:
            of_item, of_error = _resolve_location_ref(of_ref)
            if of_error is not None:
                return of_error
            within_of_lat, within_of_lon = of_item["lat"], of_item["lon"]
            if "matched_by" in of_item:
                within_resolved_echo = _location_ref_echo(of_item)
        elif division_id is not None or area is not None:
            return {
                "error": "bad_request",
                "detail": "within needs a point to measure from — pass of",
            }
        else:
            if lat is None or lon is None:
                return {"error": "bad_request", "detail": "pass both lat and lon"}
            coord_error = _invalid_coord(lat, lon)
            if coord_error is not None:
                return coord_error
            within_of_lat, within_of_lon = float(lat), float(lon)
        within_canonical = {
            "minutes": within_minutes,
            "mode": within_mode,
            "of_lat": within_of_lat,
            "of_lon": within_of_lon,
        }

    # Everything that affects the result set/order — everything but `cursor`
    # itself, `limit` (a page-size knob, not part of the query identity),
    # `detail` (presentation, not query identity — see docstring), and
    # `group_by_category` (grouped answers never carry a cursor at all, so
    # it's moot for merged-mode cursors). Numeric args are normalized to
    # float: placeroot_call's coercion path (str/JSON dispatch, e.g. over
    # HTTP) and a native Python call can hand this function an equal-valued
    # int and float (radius_m=1000 vs 1000.0) for the same query — json.dumps
    # tells those apart, so an un-normalized hash would make a cursor issued
    # on one path unusable replayed on the other, even though nothing about
    # the query differs. categories is sorted so the same set in a
    # different order still hashes identically.
    params_key = {
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "radius_m": float(radius_m) if radius_m is not None else None,
        "category": category, "name": name,
        "categories": sorted(categories) if categories else None,
        "min_confidence": float(min_confidence) if min_confidence is not None else None,
        "operating_status": operating_status, "brand": brand,
        "has_website": has_website, "has_phone": has_phone,
        "division_id": division_id, "area": area,
        "within": within_canonical,
    }
    current_release = release.resolve_release()
    cursor_error, start_offset, cursor_note = cursor_mod.resolve_cursor(
        cursor, params_key, current_release
    )
    if cursor_error is not None:
        return cursor_error

    # Cold-graph confirm gate (#336's pattern) + the shed itself, computed
    # once regardless of mode — every mode below needs the same polygon.
    within_polygon = None
    within_note = None
    if within is not None:
        if not routing.isochrone_graph_is_cached(
            within_of_lat, within_of_lon, within_minutes, within_mode
        ) and not confirm:
            return _needs_confirm_graph(within_mode)
        try:
            iso = routing.isochrone(
                within_of_lat, within_of_lon, minutes=within_minutes, mode=within_mode
            )
        except routing.UnsupportedMode as e:
            return {
                "error": "unsupported_mode",
                "detail": e.detail,
                "supported": sorted(routing.MODE_CONFIG),
            }
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
        within_polygon = iso["polygon"]
        within_note = (
            f"reachability-filtered against the street graph "
            f"({within_minutes:g} min {within_mode})"
        )

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

    # Overfetch by one row beyond the effective (post-clamp) limit so a
    # scan that stops only because it hit limit — not because it ran out of
    # matches — is distinguishable from one that didn't (has_more below).
    effective_limit = max(0, min(int(limit), overture.MAX_ROWS))

    if division_id is not None:
        if group_by_category:
            try:
                grouped = overture.find_places_in_division_grouped_by_category(
                    division_id, categories, name,
                    min_confidence, operating_status, brand, has_website, has_phone,
                    effective_limit,
                    within_polygon=within_polygon,
                )
            except ValueError as e:
                return {"error": "bad_request", "detail": str(e)}
            except overture.UpstreamUnavailable as e:
                return _upstream_error(e)
            except overture.SchemaDegraded as e:
                return _schema_error(e)
            if grouped is None:
                return {
                    "error": "not_found",
                    "detail": f"no division matched division_id {division_id!r}",
                }
            grouped = _project_grouped_places(grouped, effective_detail)
            payload = _with_degraded_fields(
                budget.apply_budget_grouped({"results": grouped}, "results")
            )
            if resolved_area is not None:
                payload["area"] = resolved_area
            payload = _with_categories_hint(
                payload, categories, widen_hint="try a larger division"
            )
            payload = _with_detail_legend(payload, effective_detail)
            payload = _with_within_extras(payload, within_resolved_echo, within_note)
            return payload
        try:
            rows = overture.find_places_in_division(
                division_id, category, name,
                min_confidence, operating_status, brand, has_website, has_phone,
                effective_limit + 1, offset=start_offset,
                categories=categories,
                within_polygon=within_polygon,
            )
        except ValueError as e:
            return {"error": "bad_request", "detail": str(e)}
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        except overture.SchemaDegraded as e:
            return _schema_error(e)
        if rows is None:
            return {
                "error": "not_found",
                "detail": f"no division matched division_id {division_id!r}",
            }
        has_more = len(rows) > effective_limit
        rows = rows[:effective_limit]
        rows = _project_places(rows, effective_detail)
        payload = _with_degraded_fields(budget.apply_budget({"results": rows}, "results"))
        if resolved_area is not None:
            # Which division the name landed on — an agent that asked for
            # "Springfield" needs to see which one it got.
            payload["area"] = resolved_area
        payload = _with_category_hint(payload, category, widen_hint="try a larger division")
        payload = _with_categories_hint(payload, categories, widen_hint="try a larger division")
        payload = _with_detail_legend(payload, effective_detail)
        payload = _with_within_extras(payload, within_resolved_echo, within_note)
        payload = cursor_mod.attach_cursor(
            payload, "results", params_key, current_release, start_offset, has_more
        )
        if cursor_note:
            payload["note"] = "; ".join(filter(None, [payload.get("note"), cursor_note]))
        return payload

    if lat is None or lon is None:
        return {"error": "bad_request", "detail": "pass both lat and lon"}
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    if group_by_category:
        try:
            grouped = overture.find_places_grouped_by_category(
                lat, lon, radius_m, categories, name,
                min_confidence, operating_status, brand, has_website, has_phone,
                effective_limit,
                within_polygon=within_polygon,
            )
        except ValueError as e:
            return {"error": "bad_request", "detail": str(e)}
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        except overture.SchemaDegraded as e:
            return _schema_error(e)
        grouped = _project_grouped_places(grouped, effective_detail)
        payload = _with_degraded_fields(
            budget.apply_budget_grouped({"results": grouped}, "results")
        )
        payload = _with_categories_hint(payload, categories, widen_hint="widen radius_m")
        payload = _with_detail_legend(payload, effective_detail)
        payload = _with_within_extras(payload, within_resolved_echo, within_note)
        payload = _with_center_resolved(payload, center_resolved_echo, within_resolved_echo)
        return payload
    try:
        rows = overture.find_places(
            lat, lon, radius_m, category, name,
            min_confidence, operating_status, brand, has_website, has_phone,
            effective_limit + 1, offset=start_offset,
            categories=categories,
            within_polygon=within_polygon,
        )
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    # #373's alt-name/fuzzy fallback tiers run their own bounded pool query
    # (only at offset 0) rather than this call's offset-aware one, so a
    # cursor over that pool can't honestly promise the next page won't
    # overlap or skip — no cursor is issued when a row came from either tier.
    used_name_fallback = any(r.get("matched_by") for r in rows)
    has_more = len(rows) > effective_limit
    rows = rows[:effective_limit]
    fallback_rows = rows  # pre-projection: detail may strip matched_by/name
    rows = _project_places(rows, effective_detail)
    payload = _with_degraded_fields(budget.apply_budget({"results": rows}, "results"))
    payload = _with_name_fallback_note(payload, name, fallback_rows)
    payload = _with_category_hint(payload, category, widen_hint="widen radius_m")
    payload = _with_categories_hint(payload, categories, widen_hint="widen radius_m")
    payload = _with_detail_legend(payload, effective_detail)
    payload = _with_within_extras(payload, within_resolved_echo, within_note)
    payload = _with_center_resolved(payload, center_resolved_echo, within_resolved_echo)
    if not used_name_fallback:
        payload = cursor_mod.attach_cursor(
            payload, "results", params_key, current_release, start_offset, has_more
        )
    if cursor_note:
        payload["note"] = "; ".join(filter(None, [payload.get("note"), cursor_note]))
    return payload


@_tool("Summarize area")
def summarize_area(
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = 1000,
    where: dict | str | None = None,
) -> dict:
    """Summarize what's in an area: total places and top categories.

    Give the center as lat/lon, or as `where` — a {"lat", "lon"} dict, a
    GERS id, or a free-text place name — but not both (and not neither);
    either way returns {"error": "bad_request"} naming the choice. A
    `where` given as an id/name adds a compact "resolved": {"name", "id",
    "lat", "lon", "matched_by"} to the answer; absent for lat/lon or a
    {lat,lon} where.

    Returns a structured {"error": ...} instead of raising if upstream is
    unavailable or the dataset is missing columns this tool depends on.
    """
    have_lat, have_lon = lat is not None, lon is not None
    if where is not None and (have_lat or have_lon):
        return {
            "error": "bad_request",
            "detail": "pass either lat and lon, or where — not both",
        }
    if where is None and not (have_lat and have_lon):
        return {
            "error": "bad_request",
            "detail": "summarize_area needs lat and lon, or where",
        }
    resolved_echo = None
    if where is not None:
        item, ref_error = _resolve_location_ref(where)
        if ref_error is not None:
            return ref_error
        lat, lon = item["lat"], item["lon"]
        if "matched_by" in item:
            resolved_echo = _location_ref_echo(item)
    else:
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    try:
        result = overture.summarize_area(lat, lon, radius_m)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    result = _with_degraded_fields(budget.apply_budget(result, "top_categories"))
    if resolved_echo is not None:
        result["resolved"] = resolved_echo
    return result


@_tool("Place details")
def place_details(
    id: str | None = None,
    name: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = overture.DEFAULT_DETAILS_RADIUS_M,
    near_lat: float | None = None,
    near_lon: float | None = None,
    lang: _LangArg = None,
) -> dict:
    """One place, in full: addresses, websites, phones, socials, brand,
    source attribution, GERS id, confidence, operating status, and a
    compact trust_note.

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

    lang (#410) requests Overture's language-tagged name variant for this
    place, when the data has one: `name` becomes the variant and
    `name_primary` is added only when it differs. Default: the stored
    `preferences()` lang, else the primary name unchanged. Never invented
    or transliterated.
    """
    if lat is not None and lon is not None:
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    if near_lat is not None and near_lon is not None:
        coord_error = _invalid_coord(near_lat, near_lon)
        if coord_error is not None:
            return coord_error
    lang = preference_store.resolve_lang(lang)
    try:
        result = overture.place_details(
            id, name, lat, lon, radius_m, near_lat, near_lon, lang=lang,
        )
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
    lat: float | None = None,
    lon: float | None = None,
    *,
    max_distance_m: float,
    category: str | None = None,
    name: str | None = None,
    where: dict | str | None = None,
) -> dict:
    """Is the nearest place matching category/name within max_distance_m of (lat, lon)?

    Give the center as lat/lon, or as `where` — a {"lat", "lon"} dict, a
    GERS id, or a free-text place name — but not both (and not neither);
    either way returns {"error": "bad_request"} naming the choice. A
    `where` given as an id/name adds a compact "resolved": {"name", "id",
    "lat", "lon", "matched_by"} to the answer; absent for lat/lon or a
    {lat,lon} where.

    max_distance_m is required and must be a positive number of meters — a
    zero, negative, non-finite, or missing value returns {"error":
    "bad_request"} rather than silently searching a 0m (or omitted from a
    call entirely, in which case the schema itself rejects it before this
    tool ever runs) window and answering a confident-looking "false".

    Returns {"within": bool, "nearest": {...place row with id...} | None,
    "distance_m": float | None}. nearest is None if nothing matches within
    a search window capped at max_distance_m * 2 — a real match further out
    than that isn't found (documented, not a bug: keeps the search bounded).
    name is a literal substring match only — no alt-spelling or typo
    fallback applies here, so a misspelled name is an honest "no match",
    never a silent yes about a different name.
    Returns a structured {"error": ...} if upstream is unavailable or the
    dataset is missing columns this tool depends on.
    """
    if (
        not isinstance(max_distance_m, (int, float))
        or isinstance(max_distance_m, bool)
        or not math.isfinite(max_distance_m)
        or max_distance_m <= 0
    ):
        return {
            "error": "bad_request",
            "detail": "max_distance_m must be a positive number of meters",
        }
    have_lat, have_lon = lat is not None, lon is not None
    if where is not None and (have_lat or have_lon):
        return {
            "error": "bad_request",
            "detail": "pass either lat and lon, or where — not both",
        }
    if where is None and not (have_lat and have_lon):
        return {
            "error": "bad_request",
            "detail": "within_distance needs lat and lon, or where",
        }
    resolved_echo = None
    if where is not None:
        item, ref_error = _resolve_location_ref(where)
        if ref_error is not None:
            return ref_error
        lat, lon = item["lat"], item["lon"]
        if "matched_by" in item:
            resolved_echo = _location_ref_echo(item)
    else:
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    try:
        result = overture.within_distance(lat, lon, max_distance_m, category, name)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    result = _with_degraded_fields(result)
    if resolved_echo is not None:
        result["resolved"] = resolved_echo
    return result


def _resolve_matrix_side(points: list, param_name: str) -> tuple[list[dict] | None, dict | None]:
    """origins/destinations LocationRef list -> ([{"lat","lon",...}], error).

    A thin name for _resolve_location_refs at the matrix tools' call sites —
    a missing/non-numeric lat or lon on a plain {"lat", "lon"} dict already
    comes back as an indexed bad_request via _invalid_coord inside
    _resolve_location_ref, so no separate precheck is needed here (and
    adding one would let a malformed dict at a higher index preempt an
    unresolved string at a lower one, breaking "lowest index wins").
    """
    return _resolve_location_refs(points, param_name)


def _matrix_resolved_echo(origins: list, resolved_origins: list[dict],
                           destinations: list, resolved_destinations: list[dict]) -> dict | None:
    """The {"origins": [...], "destinations": [...]} resolved echo, string items only."""
    echo = {}
    o_echo = [
        {"index": i, **_location_ref_echo(r)}
        for i, r in enumerate(resolved_origins)
        if isinstance(origins[i], str)
    ]
    d_echo = [
        {"index": i, **_location_ref_echo(r)}
        for i, r in enumerate(resolved_destinations)
        if isinstance(destinations[i], str)
    ]
    if o_echo:
        echo["origins"] = o_echo
    if d_echo:
        echo["destinations"] = d_echo
    return echo or None


@_tool("Distance matrix")
def distance_matrix(origins: list[dict | str], destinations: list[dict | str]) -> dict:
    """Straight-line (great-circle) distance in meters between every origin and destination.

    origins and destinations are each a list of LocationRefs — a {"lat":
    ..., "lon": ...} dict, a GERS id, or a free-text place name, mixed
    freely — capped at 10 each (100 pairs max). This is a plain haversine
    calculation, not a routed distance or travel time, so it's cheap but it
    is NOT what Google/Mapbox distance-matrix APIs return: no roads, no
    turns, no travel time. For "how far can I get in N minutes" use
    isochrone() instead; for actual routed times/distances between several
    points use travel_time_matrix().

    An id/name that failed to resolve returns an indexed error
    (origins[i]: ... or destinations[i]: ...) with candidates on ambiguity
    — checked after the 10-point cap, so an over-cap list always fails on
    the cap first. Any origin/destination given by id/name adds "resolved":
    {"origins": [{"index", "name", "id", "lat", "lon", "matched_by"}, ...],
    "destinations": [...]} covering just those entries; each side is
    present only if it had a string entry, and the whole key is absent when
    every point was already coordinates.

    Returns {"elements": [{"origin_idx": 0, "dest_idx": 0, "distance_m":
    812}, ...]}, flat and origin-major (all destinations for origin 0,
    then origin 1, ...), budgeted like every other tool. Empty origins or
    destinations returns {"elements": []}. Returns a structured {"error":
    "bad_request", ...} instead of raising if either list exceeds 10
    points or a point is missing/non-numeric lat or lon.
    """
    if len(origins) > 10 or len(destinations) > 10:
        if len(origins) > 10 and len(destinations) > 10:
            detail = (
                "origins and destinations each accept at most 10 points, "
                f"got {len(origins)} origins and {len(destinations)} destinations"
            )
        elif len(origins) > 10:
            detail = f"origins accepts at most 10 points, got {len(origins)}"
        else:
            detail = f"destinations accepts at most 10 points, got {len(destinations)}"
        return {
            "error": "bad_request",
            "detail": detail,
        }
    if not origins or not destinations:
        return {"elements": []}
    resolved_origins, origin_error = _resolve_matrix_side(origins, "origins")
    if origin_error is not None:
        return origin_error
    resolved_destinations, dest_error = _resolve_matrix_side(destinations, "destinations")
    if dest_error is not None:
        return dest_error
    o_pts = [(r["lat"], r["lon"]) for r in resolved_origins]
    d_pts = [(r["lat"], r["lon"]) for r in resolved_destinations]
    elements = [
        {
            "origin_idx": oi,
            "dest_idx": di,
            "distance_m": round(geo.haversine_m(olat, olon, dlat, dlon)),
        }
        for oi, (olat, olon) in enumerate(o_pts)
        for di, (dlat, dlon) in enumerate(d_pts)
    ]
    result = budget.apply_budget({"elements": elements}, "elements")
    resolved_echo = _matrix_resolved_echo(
        origins, resolved_origins, destinations, resolved_destinations
    )
    if resolved_echo is not None:
        result["resolved"] = resolved_echo
    return result


# issue #304: priorities input cap and accepted "prefer" values.
_MAX_PRIORITIES = 6
_VALID_PREFER = ("more", "fewer")


def _validate_priorities(priorities) -> dict | None:
    """bad_request dict if priorities is malformed, else None."""
    if not isinstance(priorities, list):
        return {"error": "bad_request", "detail": "priorities must be a list"}
    if len(priorities) > _MAX_PRIORITIES:
        return {
            "error": "bad_request",
            "detail": f"priorities accepts at most {_MAX_PRIORITIES}, got {len(priorities)}",
        }
    for idx, p in enumerate(priorities):
        if not isinstance(p, dict):
            return {"error": "bad_request", "detail": f"priorities[{idx}] must be an object"}
        label = p.get("label")
        category = p.get("category")
        prefer = p.get("prefer")
        weight = p.get("weight", 1)
        if not isinstance(label, str) or not label.strip():
            return {
                "error": "bad_request",
                "detail": f"priorities[{idx}] needs a non-empty 'label'",
            }
        if not isinstance(category, str) or not category.strip():
            return {
                "error": "bad_request",
                "detail": f"priorities[{idx}] needs a non-empty 'category'",
            }
        if prefer not in _VALID_PREFER:
            return {
                "error": "bad_request",
                "detail": f"priorities[{idx}].prefer must be 'more' or 'fewer', got {prefer!r}",
            }
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            return {
                "error": "bad_request",
                "detail": f"priorities[{idx}].weight must be numeric, got {weight!r}",
            }
    return None


def _normalize_priorities(priorities: list[dict]) -> list[dict]:
    """Defaults applied, weight clamped to [0.1, 5] (issue #304)."""
    return [
        {
            "label": p["label"],
            "category": p["category"],
            "prefer": p["prefer"],
            "weight": max(0.1, min(5.0, float(p.get("weight", 1)))),
        }
        for p in priorities
    ]
# meeting_point's bounded radius around its computed center for candidate
# venues — wide enough to usually find something without turning into an
# unbounded scan; category narrows it further, same as find_places.
_MEETING_SEARCH_RADIUS_M = 1500.0

# Cap on total (candidate venue, origin) routed pairs, so a 5-origin
# request with a generous limit can't fan out into dozens of sequential
# routed calls: the candidate count is derived from this budget divided by
# the origin count (2 origins -> 8 candidates, 5 origins -> 3), however
# many `limit` asked for. A shared-graph redesign (build each origin's
# graph once and reuse it across candidates, which sit within ~1.5km of
# each other) would raise this budget cheaply; deliberately not done here.
_MEETING_MAX_PAIRS = 16

# Floor on candidates even at 5 origins — one candidate is a ranking of
# nothing, two is the minimum for "ranked fairest-first" to mean anything.
_MEETING_MIN_CANDIDATES = 2


def _meeting_travel_time_for(origin: tuple, dest_lat: float, dest_lon: float) -> dict | str:
    """One origin -> candidate leg, routed. A reason string if unroutable.

    Mirrors route()'s own exception handling, but folds the per-pair
    failure modes (unsupported mode already validated away, no usable
    street graph near either end, too far apart for the mode's cap, or two
    snapped points with nothing connecting them) into "this candidate
    doesn't work for this origin" — meeting_point drops the candidate
    rather than erroring the whole answer over one bad pair — returning
    the failure reason so the caller can word its empty-answer note
    honestly. Outages are different: routing.UpstreamUnavailable and
    routing.SchemaDegraded propagate for the caller to turn into the same
    structured {"error": ...} that route/isochrone return, never folded
    into "unroutable".
    """
    olat, olon, mode = origin
    try:
        result = routing.route(olat, olon, dest_lat, dest_lon, mode=mode)
    except routing.NoGraphNearby:
        return "no_graph_nearby"
    except routing.RouteTooLong:
        return "route_too_long"
    except (routing.UnsupportedMode, ValueError):
        return "bad_request"
    if "error" in result:  # no_route: both ends snapped, nothing connects them
        return result["error"]
    leg = {
        "mode": mode,
        "travel_time_min": round(result["duration_s"] / 60.0, 1),
        "distance_m": round(result["distance_m"]),
    }
    if result.get("truncated"):
        leg["truncated"] = True
    return leg


def _resolve_string_origins(
    origins: list, string_idxs: list[int]
) -> tuple[dict[int, dict], dict | None]:
    """Resolve just origins' string entries (by real index), in parallel.

    meeting_point's per-index validation loop mixes string LocationRefs
    with dict {"lat","lon","mode"} origins that need their own mode
    validation, so it can't just hand the whole `origins` list to
    _resolve_location_refs (that function would try to coordinate-validate
    the dict origins itself, with a different error shape than this tool
    documents). This resolves only the string entries — same
    ThreadPoolExecutor + contextvars.copy_context() + db.isolated_reads()
    pattern _resolve_location_refs uses, so 2-5 cold name/GERS resolutions
    run concurrently instead of serially eating into the question's time
    budget — and returns them keyed by their real position in `origins`,
    so the caller's per-index loop and error messages need no index
    remapping. On any failure, returns the lowest-indexed failure with
    `"index"` and `detail` prefixed `f"origins[{i}]: "`, same contract as
    _resolve_location_refs.
    """
    resolved: dict[int, dict] = {}
    failures: dict[int, dict] = {}

    def _isolated(i: int):
        with db.isolated_reads():
            return i, _resolve_location_ref(origins[i])

    if len(string_idxs) > 1:
        with ThreadPoolExecutor(max_workers=min(len(string_idxs), 8)) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, _isolated, i) for i in string_idxs
            ]
            for future in futures:
                i, (item, err) = future.result()
                if err is not None:
                    failures[i] = err
                else:
                    resolved[i] = item
    else:
        for i in string_idxs:
            item, err = _resolve_location_ref(origins[i])
            if err is not None:
                failures[i] = err
            else:
                resolved[i] = item

    if failures:
        idx = min(failures)
        err = failures[idx]
        detail = f"origins[{idx}]: {err.get('detail', '')}"
        return {}, {**err, "index": idx, "detail": detail}
    return resolved, None


@_tool("Meeting point")
def meeting_point(
    origins: list[dict | str],
    category: str | None = None,
    limit: int = 3,
    confirm: bool = False,
) -> dict:
    """Where several people should meet, fairly: candidate venues ranked by
    equalized travel time, not geometric distance.

    Fairness objective: minimize the MAXIMUM per-person travel time to the
    venue ("no one gets screwed"), tie-broken by the smaller spread
    (max - min across everyone), then by the smaller total. This is
    deliberately not "minimize the average" — that objective can strand
    one person with a long trip so two others get a short one.

    origins is 2-5 points, each a {"lat": ..., "lon": ..., "mode": ...}
    dict, a GERS id, or a free-text place name, mixed freely — mode is
    "walk", "cycle", or "drive", defaulting to "walk" when omitted (a
    string origin always gets the default mode; give a dict with "mode" to
    pick otherwise), and can differ per person (e.g. one driving, one
    walking). An id/name that failed to resolve returns an indexed error
    (origins[i]: ...) with candidates on ambiguity. Any origin given by
    id/name adds "resolved": [{"index", "name", "id", "lat", "lon",
    "matched_by"}, ...] for just those origins; absent when every origin
    was already coordinates. category optionally filters candidate venues
    to an Overture taxonomy slug (e.g. 'coffee_shop'); a wrong or
    unrecognized slug is a silent zero-match, not an error.

    Method: a seed center is computed from each origin's implied
    straight-line travel time (not raw distance, so a walking participant
    pulls the center toward them more than a driving one at the same
    distance), venues are searched for near that seed, and each
    candidate's real per-person times come from routing.route() — the
    exact routed number, not the seed's approximation. The total routed
    (candidate, origin) fan-out is capped at 16 pairs, regardless of
    `limit` — 8 candidates at 2 origins, down to 3 candidates at 5.

    Returns {"center": {"lat", "lon"}, "candidates": [{"id", "name",
    "category", "lat", "lon", "per_person": [{"origin_idx", "mode",
    "travel_time_min", "distance_m"}, ...], "max_travel_time_min",
    "spread_min"}, ...]}, ranked fairest-first, capped at `limit` (default
    3, max 5). per_person entries carry origin_idx aligned to the input
    origins list, one entry per origin — a candidate that can't be routed
    from every origin (no street graph nearby, or genuinely disconnected)
    is dropped from the ranking entirely rather than ranked on a partial,
    unfair comparison. A per_person leg whose street graph hit its
    internal size cap carries "truncated": true (as does its candidate,
    and the answer carries a note) — that leg's time may be off. An empty
    "candidates" list is a valid answer (nothing matched the category
    nearby, or nothing routed from every origin) — it carries a "note"
    explaining which, including when every pair was over the mode's
    straight-line routing cap (try a faster mode).

    confirm=true after the user agreed to wait for a first-time
    street-graph build (about 5–25 seconds; see `route`). Without it, a
    fan-out that would need a cold graph build returns {"error":
    "needs_confirm"} instead of silently blocking. Omit confirm unless you
    just asked and they said yes.

    A non-empty result also carries "map" (#369) — a render-ready payload,
    keyword-splattable straight into this server's map-rendering tool (its
    keys are exactly that tool's keyword arguments): pins every origin, the
    fairest candidate picked out by class, the rest, and the fairness seed
    center, plus a one-line summary naming the fairest venue and its
    numbers. Absent when "candidates" is empty.

    Returns a structured {"error": "bad_request", ...} if origins has
    fewer than 2 or more than 5 points, a point is missing/non-numeric
    lat or lon, or a mode isn't walk/cycle/drive; {"error": "bad_request",
    ...} with the offending coordinate if lat/lon is out of range; or a
    structured {"error": ...} if the upstream places or transportation
    dataset is unavailable or missing columns this tool depends on.
    """
    if not isinstance(origins, list) or not (2 <= len(origins) <= 5):
        got = len(origins) if isinstance(origins, list) else 0
        return {
            "error": "bad_request",
            "detail": f"origins must hold between 2 and 5 points, got {got}",
        }
    string_idxs = [i for i, o in enumerate(origins) if isinstance(o, str)]
    resolved_strings, resolve_error = _resolve_string_origins(origins, string_idxs)
    if resolve_error is not None:
        return resolve_error

    parsed: list[tuple[float, float, str]] = []
    resolved_echo: list[dict] = []
    for idx, o in enumerate(origins):
        if isinstance(o, str):
            item = resolved_strings[idx]
            olat, olon, mode = item["lat"], item["lon"], "walk"
            if "matched_by" in item:
                resolved_echo.append({"index": idx, **_location_ref_echo(item)})
        elif isinstance(o, dict):
            try:
                olat, olon = float(o["lat"]), float(o["lon"])
            except (KeyError, TypeError, ValueError) as e:
                return {
                    "error": "bad_request",
                    "detail": f"origins[{idx}]: each origin needs numeric lat and lon: {e}",
                }
            coord_error = _invalid_coord(olat, olon)
            if coord_error is not None:
                coord_error["detail"] = f"origins[{idx}]: {coord_error['detail']}"
                return coord_error
            mode = o.get("mode", "walk")
            if not isinstance(mode, str) or mode not in routing.MODE_CONFIG:
                return {
                    "error": "bad_request",
                    "detail": (
                        f"origins[{idx}]: mode must be one of "
                        f"{sorted(routing.MODE_CONFIG)}, got {mode!r}"
                    ),
                    "supported": sorted(routing.MODE_CONFIG),
                }
        else:
            return {
                "error": "bad_request",
                "detail": f"origins[{idx}] must be an object with lat and lon, or a place name",
            }
        parsed.append((olat, olon, mode))

    try:
        limit = max(1, min(int(limit), 5))
    except (TypeError, ValueError):
        return {"error": "bad_request", "detail": f"limit must be an integer, got {limit!r}"}

    center_lat, center_lon = meeting.find_center(parsed)

    max_candidates = max(_MEETING_MIN_CANDIDATES, _MEETING_MAX_PAIRS // len(parsed))
    fetch_n = min(2 * limit + 2, max_candidates)
    try:
        rows = overture.find_places(
            center_lat, center_lon, radius_m=_MEETING_SEARCH_RADIUS_M,
            category=category, limit=fetch_n,
        )
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)

    payload = {"center": {"lat": center_lat, "lon": center_lon}, "candidates": []}
    if resolved_echo:
        payload["resolved"] = resolved_echo
    if not rows:
        if category:
            payload["note"] = (
                f"no places matched category {category!r} near the fair meeting "
                "point; if that may not be a valid Overture category slug, use "
                "search_categories to find the right one, or drop the category filter."
            )
        else:
            payload["note"] = (
                "no places listed within "
                f"{round(_MEETING_SEARCH_RADIUS_M)} m of the fair meeting point; "
                "the center coordinate above is still the fair spot to meet near."
            )
        return payload

    rows = rows[:max_candidates]

    # Same cheap-reject gate as route() (#336), checked over the whole
    # fan-out before any routed call: a cold street graph anywhere in it
    # means a 5-25s build the user did not agree to wait for. Pairs beyond
    # the mode's straight-line cap are skipped — they are rejected before
    # any graph is built, so they never trigger one.
    if not confirm:
        for olat, olon, mode in parsed:
            for row in rows:
                if (
                    routing._haversine_m(olat, olon, row["lat"], row["lon"])
                    > routing.ROUTE_MAX_STRAIGHT_LINE_M[mode]
                ):
                    continue
                if not routing.route_graph_is_cached(
                    olat, olon, row["lat"], row["lon"], mode
                ):
                    return _needs_confirm_graph(mode)

    candidates = []
    drop_reasons: set[str] = set()
    try:
        for row in rows:
            per_person = []
            routable = True
            for origin_idx, origin in enumerate(parsed):
                leg = _meeting_travel_time_for(origin, row["lat"], row["lon"])
                if isinstance(leg, str):
                    drop_reasons.add(leg)
                    routable = False
                    break
                per_person.append({"origin_idx": origin_idx, **leg})
            if not routable:
                continue
            times = [p["travel_time_min"] for p in per_person]
            candidate = {
                "id": row["id"],
                "name": row["name"],
                "category": row.get("category"),
                "lat": row["lat"],
                "lon": row["lon"],
                "per_person": per_person,
                "max_travel_time_min": max(times),
                "spread_min": round(max(times) - min(times), 1),
            }
            if any(p.get("truncated") for p in per_person):
                candidate["truncated"] = True
            candidates.append(candidate)
    except routing.UpstreamUnavailable as e:
        return _upstream_error(e)
    except routing.SchemaDegraded as e:
        return _schema_error(e)

    candidates.sort(
        key=lambda c: meeting.fairness_key([p["travel_time_min"] for p in c["per_person"]])
    )
    payload["candidates"] = candidates[:limit]
    if not candidates:
        reasons = []
        if "route_too_long" in drop_reasons:
            reasons.append(
                "at least one origin is further from every candidate than its "
                "mode's straight-line routing cap — a faster mode (cycle or "
                "drive) has a larger cap and may route"
            )
        if drop_reasons - {"route_too_long"}:
            reasons.append(
                "no street graph nearby, or genuinely disconnected, for at "
                "least one origin"
            )
        payload["note"] = (
            "found candidate venues near the fair meeting point, but none could "
            "be routed to from every participant (" + "; ".join(reasons) + ")"
        )
        return payload
    if any(c.get("truncated") for c in payload["candidates"]):
        payload["note"] = (
            "some legs' street graphs hit the internal size cap (marked "
            "truncated) — those travel times may be based on a suboptimal or "
            "incomplete route"
        )
    payload = budget.apply_budget(payload, "candidates")
    origin_points = [{"lat": olat, "lon": olon} for olat, olon, _mode in parsed]
    map_payload = mapexplain.from_meeting_point_result(payload, origin_points)
    if map_payload is not None:
        payload["map"] = map_payload
    return payload
@_tool("Travel time matrix")
def travel_time_matrix(
    origins: list[dict | str], destinations: list[dict | str], mode: _ModeArgWalkDefault = None
) -> dict:
    """Routed travel time + distance between every origin and destination, by mode.

    origins and destinations are each a list of LocationRefs — a {"lat":
    ..., "lon": ...} dict, a GERS id, or a free-text place name, mixed
    freely — capped at 5 each (25 pairs max). Unlike distance_matrix's plain
    haversine, this is a real shortest-path search over Overture's open
    street graph — roads, one-ways, and each mode's own speed model, the
    same cost model route() uses for a single pair, one mode per call;
    omit mode to use the stored preferences mode, else walk.

    An id/name that failed to resolve returns an indexed error
    (origins[i]: ... or destinations[i]: ...) with candidates on ambiguity
    — checked after the 5-point cap. Any origin/destination given by
    id/name adds "resolved": {"origins": [{"index", "name", "id", "lat",
    "lon", "matched_by"}, ...], "destinations": [...]} covering just those
    entries; each side present only if it had a string entry, absent when
    every point was already coordinates.

    Reuses a single cached street graph across every origin and
    destination when every origin-destination pair fits the mode's
    straight-line cap and the whole point set fits one extraction circle,
    running one Dijkstra per origin against every destination at once
    rather than a search per pair — for a same-city matrix this costs
    about what a single isochrone does, not one route() call per pair.
    When the points are too spread out for one shared graph, falls back to
    a route() call per pair (up to 25).

    Returns {"mode", "elements": [{"origin_idx", "dest_idx", "duration_min",
    "distance_m"}, ...], "durations_note"}, flat and origin-major like
    distance_matrix. durations_note says these are speed-model estimates
    over the open street graph, not live traffic. An unroutable pair (off
    the street network, or on a disconnected fragment of it) gets
    {"duration_min": null, "distance_m": null, "note": "unroutable"}
    instead of failing the whole call; if every pair in the matrix is
    unroutable the response also carries a top-level "note" saying so.
    If the street graph hit its size cap the response carries "truncated":
    true plus a note — capped extractions may present reachable pairs as
    unroutable. Empty origins or destinations returns {"elements": []}.

    Returns a structured {"error": "bad_request", ...} instead of raising
    if either list exceeds 5 points, a point is missing/non-numeric lat or
    lon, or mode isn't walk/cycle/drive. If no street graph exists
    anywhere near every point in the matrix, returns {"error":
    "no_graph_nearby"} — the same top-level failure route() and
    optimize_route() give when nothing in the area is on the mapped
    network, rather than a matrix of nulls.
    """
    if len(origins) > 5 or len(destinations) > 5:
        if len(origins) > 5 and len(destinations) > 5:
            detail = (
                "origins and destinations each accept at most 5 points, "
                f"got {len(origins)} origins and {len(destinations)} destinations"
            )
        elif len(origins) > 5:
            detail = f"origins accepts at most 5 points, got {len(origins)}"
        else:
            detail = f"destinations accepts at most 5 points, got {len(destinations)}"
        return {"error": "bad_request", "detail": detail}
    mode = preference_store.resolve_mode(mode, "walk")
    if mode not in routing.MODE_CONFIG:
        return {
            "error": "bad_request",
            "detail": f"mode={mode!r} is not supported; use walk, cycle, or drive",
        }
    if not origins or not destinations:
        return {"elements": []}
    resolved_origins, origin_error = _resolve_matrix_side(origins, "origins")
    if origin_error is not None:
        return origin_error
    resolved_destinations, dest_error = _resolve_matrix_side(destinations, "destinations")
    if dest_error is not None:
        return dest_error
    o_pts = [(r["lat"], r["lon"]) for r in resolved_origins]
    d_pts = [(r["lat"], r["lon"]) for r in resolved_destinations]
    try:
        result = routing.travel_time_matrix(o_pts, d_pts, mode=mode)
    except routing.UpstreamUnavailable as e:
        return _upstream_error(e)
    except routing.SchemaDegraded as e:
        return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}
    except routing.NoGraphNearby as e:
        return {"error": "no_graph_nearby", "detail": e.detail}
    result = budget.apply_budget(result, "elements")
    resolved_echo = _matrix_resolved_echo(
        origins, resolved_origins, destinations, resolved_destinations
    )
    if resolved_echo is not None:
        result["resolved"] = resolved_echo
    return result


def _anchor_summary(a: dict) -> dict:
    return {"lat": a["lat"], "lon": a["lon"], "mode": a["mode"], "minutes": a["minutes"]}


@_tool("Suggest areas")
def suggest_areas(
    anchors: list[dict],
    requirements: list[str],
    limit: int = 5,
    confirm: bool = False,
) -> dict:
    """Where within reach: neighborhoods ranked by travel budget + amenities.

    The inverse of every other area tool — instead of "describe this place",
    "find me a place". anchors is 1-3 {"lat", "lon", "mode"?, "minutes"?}
    points (mode: walk/cycle/drive, default from stored preferences;
    minutes: default 15). requirements is 1-8 free-text amenity/character
    strings, scored the same way as area_score.score_locality — "parks",
    "groceries", "coffee shop" resolve against the Overture taxonomy;
    a subjective phrase ("quiet streets", "safe neighborhood", "good
    schools") comes back {"measurable": false} rather than a guessed score
    (see "honesty" in the response).

    Method: the same street-graph reach analysis behind PlaceRoot's other
    travel-time tools computes each anchor's reachable shed; with more than
    one anchor, the sheds are intersected (a candidate must be reachable
    within EVERY anchor's own time budget, not just one — "office" and
    "gym" both mean both). divisions.divisions_in_polygon (#348) partitions
    the (intersected) shed into candidate neighborhoods/localities; each
    candidate is scored against requirements the same way
    area_score.score_locality (#349) does. Returns {"anchors": [...],
    "results": [{"division_id", "name", "subtype", "overlap_fraction",
    "lat", "lon", "travel": [{"anchor_idx", "mode", "minutes_budget",
    "travel_time_min", "distance_m"} or {..., "note": "unroutable"/
    "no_graph_nearby"/...}, ...], "requirements": [...], "overall_score",
    "reason"}, ...], "honesty"}, ranked by overall_score (unmeasurable-only
    candidates sort last, never dropped) then overlap_fraction, capped at
    `limit` (1-10, default 5). division_id is a stable GERS id — chain a
    result into admin_lookup or summarize_area for more detail without
    re-running the search. No polygons in the response by default.

    An empty "results" list is a valid answer (e.g. two anchors' sheds don't
    overlap at all, or nothing in the reachable area is a neighborhood/
    locality) with a "note" saying which. A per-anchor travel leg that can't
    be routed (the polygon-approximated shed boundary occasionally includes
    a point routing itself can't reach) gets "note" instead of a time,
    without dropping the whole candidate.

    confirm=true after the user agreed to wait for a first-time street-graph
    build (about 5-25 seconds per anchor that needs one). Every anchor is
    checked before any graph is built, so a fan-out never starts some
    anchors and then stalls needing confirm on the next. Omit confirm
    unless you just asked and they said yes.

    Returns a structured {"error": "bad_request", ...} if anchors isn't 1-3
    points, a point is missing/non-numeric lat, lon, or minutes, minutes is
    not > 0, or a mode isn't walk/cycle/drive; likewise if requirements
    isn't 1-8 non-empty strings. Propagates the same structured errors as
    the underlying reach analysis (unsupported_mode, no_graph_nearby,
    radius_too_large) and divisions_in_polygon/score_locality (upstream_unavailable,
    schema_degraded) — a partial shortlist from a failed anchor or scan is
    never returned.
    """
    if not isinstance(anchors, list) or not (1 <= len(anchors) <= area_suggest.MAX_ANCHORS):
        got = len(anchors) if isinstance(anchors, list) else 0
        return {
            "error": "bad_request",
            "detail": (
                f"anchors must hold between 1 and {area_suggest.MAX_ANCHORS} points, got {got}"
            ),
        }
    parsed_anchors: list[dict] = []
    for idx, a in enumerate(anchors):
        if not isinstance(a, dict):
            return {"error": "bad_request", "detail": f"anchors[{idx}] must be an object"}
        try:
            alat, alon = float(a["lat"]), float(a["lon"])
        except (KeyError, TypeError, ValueError) as e:
            return {
                "error": "bad_request",
                "detail": f"anchors[{idx}]: needs numeric lat and lon: {e}",
            }
        coord_error = _invalid_coord(alat, alon)
        if coord_error is not None:
            coord_error["detail"] = f"anchors[{idx}]: {coord_error['detail']}"
            return coord_error
        mode = preference_store.resolve_mode(a.get("mode"), preference_store.DEFAULT_MODE_ISOCHRONE)
        if mode not in routing.MODE_CONFIG:
            return {
                "error": "bad_request",
                "detail": (
                    f"anchors[{idx}]: mode must be one of "
                    f"{sorted(routing.MODE_CONFIG)}, got {mode!r}"
                ),
                "supported": sorted(routing.MODE_CONFIG),
            }
        minutes = a.get("minutes", 15)
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            return {
                "error": "bad_request",
                "detail": f"anchors[{idx}]: minutes must be numeric, got {minutes!r}",
            }
        if not math.isfinite(minutes) or minutes <= 0:
            return {
                "error": "bad_request",
                "detail": f"anchors[{idx}]: minutes must be greater than 0",
            }
        parsed_anchors.append({"lat": alat, "lon": alon, "mode": mode, "minutes": minutes})

    if not isinstance(requirements, list) or not requirements:
        return {
            "error": "bad_request",
            "detail": "requirements must be a non-empty list of strings",
        }
    if len(requirements) > area_score.MAX_REQUIREMENTS:
        return {
            "error": "bad_request",
            "detail": (
                f"requirements accepts at most {area_score.MAX_REQUIREMENTS}, "
                f"got {len(requirements)}"
            ),
        }
    for req in requirements:
        if not isinstance(req, str) or not req.strip():
            return {
                "error": "bad_request",
                "detail": "every requirement must be a non-empty string",
            }

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"error": "bad_request", "detail": f"limit must be an integer, got {limit!r}"}
    limit = max(area_suggest.MIN_LIMIT, min(limit, area_suggest.MAX_LIMIT))

    # Same cheap-reject-before-any-build gate as meeting_point (#336): check
    # every anchor before extracting any graph, so a multi-anchor call never
    # builds anchor 0's graph and then stalls needing confirm on anchor 1.
    if not confirm:
        for a in parsed_anchors:
            if not routing.isochrone_graph_is_cached(a["lat"], a["lon"], a["minutes"], a["mode"]):
                return _needs_confirm_graph(a["mode"])

    sheds = []
    for idx, a in enumerate(parsed_anchors):
        try:
            iso = routing.isochrone(a["lat"], a["lon"], minutes=a["minutes"], mode=a["mode"])
        except routing.UnsupportedMode as e:
            return {
                "error": "unsupported_mode",
                "detail": e.detail,
                "supported": sorted(routing.MODE_CONFIG),
            }
        except routing.UpstreamUnavailable as e:
            return _upstream_error(e)
        except routing.SchemaDegraded as e:
            return _schema_error(e)
        except routing.NoGraphNearby as e:
            return {"error": "no_graph_nearby", "detail": f"anchors[{idx}]: {e.detail}"}
        except routing.RadiusTooLarge as e:
            return {
                "error": "radius_too_large",
                "detail": e.detail,
                "max_radius_m": e.max_radius_m,
            }
        except ValueError as e:
            return {"error": "bad_request", "detail": f"anchors[{idx}]: {e}"}
        sheds.append(iso["polygon"])

    parts = area_suggest.intersect_sheds(sheds)
    anchor_payload = [_anchor_summary(a) for a in parsed_anchors]
    if not parts:
        note = (
            "anchors' reachable sheds do not overlap at all within their given "
            "time budgets"
            if len(parsed_anchors) > 1
            else "the reachable shed has no area to search"
        )
        return {"anchors": anchor_payload, "results": [], "note": note}

    candidate_fetch = min(max(limit * 3, 10), overture.MAX_ROWS)
    by_id: dict[str, dict] = {}
    for part in parts:
        try:
            part_result = divisions.divisions_in_polygon(part, limit=candidate_fetch)
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        except overture.SchemaDegraded as e:
            return _schema_error(e)
        except ValueError as e:
            return {"error": "bad_request", "detail": str(e)}
        for row in part_result["results"]:
            existing = by_id.get(row["id"])
            if existing is None or row["overlap_fraction"] > existing["overlap_fraction"]:
                by_id[row["id"]] = row

    if not by_id:
        return {
            "anchors": anchor_payload,
            "results": [],
            "note": "no neighborhood/locality divisions intersect the reachable area",
        }

    candidates = sorted(
        by_id.values(), key=lambda r: r["overlap_fraction"], reverse=True
    )[:candidate_fetch]

    near_lat = sum(a["lat"] for a in parsed_anchors) / len(parsed_anchors)
    near_lon = sum(a["lon"] for a in parsed_anchors) / len(parsed_anchors)

    scored: list[dict] = []
    for cand in candidates:
        try:
            score = area_score.score_locality(
                requirements, division_id=cand["id"], near_lat=near_lat, near_lon=near_lon,
            )
        except area_score.LocalityNotFound:
            continue
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        except overture.SchemaDegraded as e:
            return _schema_error(e)
        except ValueError:
            continue

        loc = score["locality"]
        travel = []
        for anchor_idx, a in enumerate(parsed_anchors):
            leg = _meeting_travel_time_for((a["lat"], a["lon"], a["mode"]), loc["lat"], loc["lon"])
            entry = {"anchor_idx": anchor_idx, "mode": a["mode"], "minutes_budget": a["minutes"]}
            if isinstance(leg, str):
                entry["note"] = leg
            else:
                entry.update(leg)
            travel.append(entry)

        scored.append({
            "division_id": cand["id"],
            "name": cand["name"],
            "subtype": cand["subtype"],
            "overlap_fraction": cand["overlap_fraction"],
            "lat": loc["lat"],
            "lon": loc["lon"],
            "travel": travel,
            "requirements": score["requirements"],
            "overall_score": score["overall_score"],
            "reason": area_suggest.build_reason(score["requirements"]),
        })

    scored.sort(
        key=lambda r: (
            r["overall_score"] if r["overall_score"] is not None else -1.0,
            r["overlap_fraction"],
        ),
        reverse=True,
    )
    payload = {
        "anchors": anchor_payload,
        "results": scored[:limit],
        "honesty": area_score.HONESTY,
    }
    if not payload["results"]:
        payload["note"] = "no candidate locality could be scored"
    return budget.apply_budget(payload, "results")


@_tool("Compare areas")
def compare_areas(
    areas: list[dict | str], radius_m: float = 1000, priorities: list[dict] | None = None
) -> dict:
    """Compare 2-5 areas side by side: category mix, density, and what differs.

    areas is a list of centers sharing one radius_m, each a {"lat": ...,
    "lon": ...} dict, a GERS id, or a free-text place/area name, mixed
    freely — a named area compares the same radius_m circle around its
    resolved point as a coordinate would (not its actual boundary; that's a
    later feature). An id/name that failed to resolve returns an indexed
    error (areas[i]: ...) with candidates on ambiguity. Any area given by
    id/name adds "resolved": [{"index": i, "name", "id", "lat", "lon",
    "matched_by"}, ...] for just those areas; absent when every area was
    already coordinates. Returns per-area total_places, place density per
    km^2, and category_counts aligned across areas for the top ~10
    categories by combined count, plus "differentiators" — those categories
    ranked by how much they differ, relatively, between areas (the fastest
    way to answer "how is area A different from area B"). Returns a
    structured {"error": ...} if areas isn't 2-5 centers, or if upstream is
    unavailable or the dataset is missing columns this tool depends on for
    any area (a partial comparison is not returned).

    priorities (optional, up to 6) turns the comparison into a scored
    verdict: each entry is {"label": your own term for the criterion,
    e.g. "competition"; "category": an Overture taxonomy slug, or
    "__density__" for overall place density as a foot-traffic proxy;
    "prefer": "more" | "fewer"; "weight": 0.1-5, default 1}. Each area's raw
    measure per priority is that category's count (or density) within
    radius_m — matched exactly against the category taxonomy (slug plus its
    descendants, so "park" never counts parking garages) and counted
    explicitly even for categories outside the top-10 alignment above; the
    per-priority winner is whichever area is better on that raw measure (a
    tie has no winner for that priority); each area's verdict score is the
    weight-summed share of each priority normalized against the best area
    (measure/max for "more", min/measure for "fewer" — the best area always
    gets 1.0, and every area measuring 0 makes all shares 1.0), and the
    highest score wins overall (a tie leaves winner_idx null). Adds (never
    replaces) "verdict": {"winner_idx", "scores", "reasons" (one sentence
    per priority), "margin", and a fixed "measured_note"} — the note,
    always present when priorities are given, states plainly that these are
    open-data place counts/density, never revenue, rent, actual foot
    traffic, or demographics, and that "__density__" is only a proxy. If
    the dataset's category columns are all degraded, count-based priorities
    can't be measured and the verdict comes back with null winner_idx and
    scores plus "degraded": true rather than a fabricated score. Returns
    bad_request for more than 6 priorities or a malformed one (missing
    label/category, an unrecognized prefer, or a non-numeric weight).

    When priorities produced a verdict, the response also carries "map"
    (#369) — a render-ready payload, keyword-splattable straight into this
    server's map-rendering tool (its keys are exactly that tool's keyword
    arguments): a pin per area (the winner picked out by class), a cheap
    circle outline per area (radius_m, not a real boundary) labeled with
    its score, and a one-line summary restating the winner. Absent when
    priorities weren't given, or a verdict couldn't be scored.
    """
    if not isinstance(areas, list):
        return {"error": "bad_request", "detail": "areas must be a list of area centers"}
    resolved_areas, resolve_error = _resolve_location_refs(areas, "areas")
    if resolve_error is not None:
        return resolve_error
    centers = [(r["lat"], r["lon"]) for r in resolved_areas]
    resolved_echo = [
        {"index": i, **_location_ref_echo(r)}
        for i, r in enumerate(resolved_areas)
        if "matched_by" in r
    ]
    normalized_priorities = None
    if priorities is not None:
        priority_error = _validate_priorities(priorities)
        if priority_error is not None:
            return priority_error
        normalized_priorities = _normalize_priorities(priorities)
    try:
        result = overture.compare_areas(centers, radius_m, priorities=normalized_priorities)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    result = _with_degraded_fields(budget.apply_budget(result, "differentiators"))
    if "verdict" in result:
        map_payload = mapexplain.from_compare_areas_result(result, radius_m)
        if map_payload is not None:
            result["map"] = map_payload
    if resolved_echo:
        result["resolved"] = resolved_echo
    return result


@_tool("Admin hierarchy lookup")
def admin_lookup(lat: float, lon: float) -> dict:
    """Containing admin hierarchy for a point: neighborhood up to country.

    Point-in-polygon against Overture's divisions theme. Returns {"chain":
    [{"name": ..., "type": "locality", "id": ...}, ...]} smallest division
    first (e.g. neighborhood, then locality, county, region, country) — an
    empty chain means no division in the active dataset contains the
    point, which is a valid answer for remote areas, not an error. Chain
    rows also carry "country" (ISO 3166-1 alpha-2, e.g. "DE") and "region"
    (ISO 3166-2, e.g. "DE-BY") where the source row has them — omitted,
    not null-filled, when the dataset or row lacks a value. Returns
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


@_tool("Changes in area")
def changes_in_area(
    place: str | None = None,
    min_lon: float | None = None,
    min_lat: float | None = None,
    max_lon: float | None = None,
    max_lat: float | None = None,
    category: str | None = None,
    from_release: str | None = None,
    to_release: str | None = None,
    limit: int = changes.DEFAULT_DIGEST_TOP_N,
) -> dict:
    """What's opened or closed around here since a past Overture release.

    Use this for "what's new around here", "what's closed since spring",
    or any question with a time dimension — every other tool here answers
    against a single, current snapshot of the data; this is the only tool
    that compares two.

    Area, exactly one of:
    - place: a free-text area name ("Palo Alto"), resolved with this
      server's usual free-text area matching (prominence-ranked, "City,
      ST" suffix aware). The resolved division is echoed back as "area"
      on the response. A name
      matching several equally-ranked divisions returns {"error":
      "ambiguous_area", "candidates": [...]} rather than silently picking
      one; an unresolvable name returns {"error": "not_found"}. If the
      resolved division is too large to diff (bigger than this tool's
      per-side degree cap — countries, large regions), returns a
      {"error": "bad_request"} naming the area and suggesting a smaller
      one (a neighborhood or district instead of a whole city/region) or
      an explicit bbox.
    - min_lon/min_lat/max_lon/max_lat: an explicit bbox (all four
      together, or none) — for when the caller already has coordinates
      rather than a name. Same size cap as the named-area path.

    category, when given, filters both releases' scans identically before
    diffing — see diff_places; a place that changed OUT of the category
    between releases reads as "disappeared" from this filtered view, which
    is the correct reading of "restaurants that changed", not a bug.

    from_release/to_release: Overture release strings (YYYY-MM-DD.N). Omit
    both to diff the previous release against the ACTIVE one — to_release
    defaults to release.resolve_release(), the same release every other
    tool in this conversation queries (env pins included), and
    from_release defaults to the newest listed release older than it: an
    adjacent, recent window. Not the oldest release Overture still serves
    — years-old releases are schema-drifted enough that most compared
    columns NULL out and everything reads as "changed"; pass explicit
    releases for a wider window, and check "degraded_fields" on the
    response when you do. Pass both to pick a
    specific window; passing only one is a {"error": "bad_request"}. When
    the release listing is reachable, an explicit release not in it is
    also a {"error": "bad_request"} rather than a silent typo'd diff; when
    the listing itself is unreachable (network trouble), explicit releases
    are tried directly instead — they may still resolve even though the
    list couldn't be built.

    If neither release is given and no listed release is older than the
    active one (a listing failure, or a world with only one live release),
    this returns a structured {"error": "upstream_unavailable"} naming
    whatever releases WERE found — never an empty diff that would read as
    "nothing changed here" when the real answer is "the window couldn't be
    built".

    limit caps how many ranked rows each of appeared/disappeared/changed
    carries (default 8, hard cap 25 — the same per-answer row bound every
    other tool here uses); "counts" and the "*_by_category" breakdowns are
    never reduced by it.

    Returns a compact digest, not the full diff: headline "counts"
    (appeared/disappeared/changed/unchanged, always exact within the
    scanned bound), a `limit`-capped ranked slice of each of
    appeared/disappeared/changed (id, name, category, confidence, lat,
    lon — changed rows carry old_name/new_name and
    old_category/new_category instead of name/category), a small
    per-bucket "*_by_category" breakdown computed over everything the
    scans saw (same denominator as "counts", not the limit-capped slice),
    the "releases" window actually used, "degraded_fields" when either
    release's schema is missing a compared column (that field was NULL on
    that side, so treat "changed" with suspicion), and
    "truncated"/"omitted_count" when more exists than fits
    (omitted_count sums omissions across the three lists).

    Honest framing (issue #309): a "notes" list is attached whenever it
    applies — a disappearance may be delisting or data cleanup, not a
    closure; an appearance may be newly-mapped, not newly-opened. Neither
    claim is inferable from this data alone, so the digest says so rather
    than implying otherwise.

    Returns a structured {"error": ...} if upstream is unavailable or the
    active places dataset is missing the id/bbox columns this tool depends
    on, for either release.
    """
    place_given = place is not None
    bbox_fields = (min_lon, min_lat, max_lon, max_lat)
    any_bbox = any(v is not None for v in bbox_fields)
    all_bbox = all(v is not None for v in bbox_fields)
    if any_bbox and not all_bbox:
        return {
            "error": "bad_request",
            "detail": "pass all four of min_lon/min_lat/max_lon/max_lat together, or none",
        }
    modes_given = sum([place_given, all_bbox])
    if modes_given == 0:
        return {
            "error": "bad_request",
            "detail": "pass either place or all four of min_lon/min_lat/max_lon/max_lat",
        }
    if modes_given == 2:
        return {
            "error": "bad_request",
            "detail": "pass exactly one of place or a bbox, not both",
        }

    resolved_area = None
    if place_given:
        if not isinstance(place, str) or not place.strip():
            return {"error": "bad_request", "detail": "place must be a non-empty name"}
        try:
            resolved_area = geocoding.resolve_area(place)
        except errors.AmbiguousArea as e:
            return {
                "error": "ambiguous_area",
                "detail": e.detail,
                "candidates": e.candidates,
            }
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        if resolved_area is None:
            return {"error": "not_found", "detail": f"no division matched place {place!r}"}
        try:
            resolved_geometry = overture._resolve_division_geometry(resolved_area["division_id"])
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        except overture.SchemaDegraded as e:
            return _schema_error(e)
        if resolved_geometry is None:
            return {"error": "not_found", "detail": f"no division matched place {place!r}"}
        _wkb, xmin, xmax, ymin, ymax = resolved_geometry
        span = max(xmax - xmin, ymax - ymin)
        if span > changes.MAX_BBOX_SPAN_DEG:
            return {
                "error": "bad_request",
                "detail": (
                    f"{resolved_area['name']!r} spans {span:.2f} degrees, too large for "
                    f"changes_in_area (max {changes.MAX_BBOX_SPAN_DEG} degrees per side). "
                    "Pass a smaller area (a neighborhood or district rather than a whole "
                    "city/region/country), or pass an explicit bbox."
                ),
            }
        bbox = (xmin, ymin, xmax, ymax)
    else:
        coord_error = _invalid_coord(min_lat, min_lon) or _invalid_coord(max_lat, max_lon)
        if coord_error is not None:
            return coord_error
        bbox = (min_lon, min_lat, max_lon, max_lat)

    available = release.available_releases()
    if from_release is None and to_release is None:
        # Default to a window ending at the ACTIVE release — the one every
        # other tool in this conversation queries (resolve_release honors
        # PLACEROOT_OVERTURE_RELEASE pins; available[-1] would be whatever
        # is newest on S3, contradicting them) — starting from the newest
        # listed release older than it: adjacent and recent, not the
        # years-old, schema-drifted oldest release Overture still serves.
        # The active release itself need not appear in the listing (a
        # pinned or mirrored install): only "older than active" matters.
        to_release = release.resolve_release()
        to_key = release._numeric_patch_key(to_release)
        older = [r for r in available if release._numeric_patch_key(r) < to_key]
        if not older:
            found = ", ".join(available) if available else "none"
            return {
                "error": "upstream_unavailable",
                "detail": (
                    "could not establish a diff window: the active release is "
                    f"{to_release} and release.available_releases() found no "
                    f"older release (releases found: {found}). Pass "
                    "from_release and to_release explicitly if you know two "
                    "valid release names."
                ),
                "retry_advised": True,
            }
        from_release = older[-1]  # `available` is ascending: the previous release
    elif from_release is None or to_release is None:
        return {
            "error": "bad_request",
            "detail": "pass both from_release and to_release, or neither",
        }
    elif available and (from_release not in available or to_release not in available):
        unknown = [r for r in (from_release, to_release) if r not in available]
        return {
            "error": "bad_request",
            "detail": (
                f"{', '.join(unknown)} not in the available releases ({', '.join(available)})"
            ),
        }
    # else: available == [] (listing unreachable) -- try the given releases
    # directly, they may still resolve even though the listing couldn't be
    # built (see docstring).

    try:
        diff = changes.diff_places(bbox, from_release, to_release, category, limit)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except changes.UpstreamUnavailable as e:
        return _upstream_error(e)
    except changes.SchemaDegraded as e:
        return _schema_error(e)

    # limit drives the digest's per-bucket top_n (capped at the same
    # per-answer row bound every other tool uses), so "widen the limit"
    # actually widens the lists instead of stalling at the default 8.
    digest = changes.changes_digest(diff, top_n=min(limit, overture.MAX_ROWS))
    if resolved_area is not None:
        digest["area"] = resolved_area
    # Budget the three lists with ONE summed omitted_count: successive
    # apply_budget calls would each overwrite truncated/omitted_count with
    # only their own bucket's numbers, under-reporting omissions.
    omitted_total = 0
    for bucket in ("appeared", "disappeared", "changed"):
        digest = budget.apply_budget(digest, bucket)
        omitted_total += digest.pop("omitted_count", 0)
    if omitted_total:
        digest["omitted_count"] = omitted_total
    return digest


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


def _with_transit_notes(
    payload: dict, total_in_range: int, fallback_used: bool, class_missing: bool
) -> dict:
    """Add total_in_range, truncated/note, and the fallback/empty notes transit_stops_near needs.

    Unlike infrastructure_at's truncation-only helper, total_in_range is
    always present here (issue #453's response shape names it as a
    top-level field, not something that only shows up once the answer is a
    slice). truncated/omitted_count still only appear when fewer rows came
    back than matched, same convention as every other radius search.
    """
    shown = len(payload.get("results", []))
    payload["total_in_range"] = total_in_range
    truncated = shown < total_in_range
    if truncated:
        payload["truncated"] = True
        payload["omitted_count"] = total_in_range - shown

    notes = []
    if class_missing:
        notes.append(
            "this dataset has no `class` column, so stop-like rows cannot be told "
            "apart from bicycle parking or duplicate platform/stop_position records; "
            "returning no results rather than an unfiltered guess."
        )
    else:
        if fallback_used:
            notes.append(
                "no bus_stop/station-class row was within radius_m; these are "
                "platform/stop_position records instead — OSM's per-geometry "
                "re-tagging of the same physical stops, returned as the next-best "
                "signal."
            )
        elif shown == 0:
            notes.append(
                "no transit stop within radius_m. Base-theme coverage is "
                "OSM-derived and patchy, so this is a real finding, not proof "
                "there is no transit nearby."
            )
        if truncated:
            notes.append(
                f"showing the {shown} nearest of {total_in_range} matching stops; "
                "narrow further with a smaller radius or a more specific kind."
            )
    if notes:
        payload["note"] = " ".join(notes)
    return payload


@_tool("Transit stops near a point")
def transit_stops_near(
    lat: float,
    lon: float,
    radius_m: float = transit.DEFAULT_RADIUS_M,
    limit: int = transit.DEFAULT_LIMIT,
    kind: str | None = None,
) -> dict:
    """Nearest transit stops to a point, nearest first — bus, rail, subway, tram, ferry.

    A filtered view over Overture's base/infrastructure `subtype='transit'`
    rows (the same theme infrastructure_at reads), restricted to real,
    boardable stop classes: bus_stop, bus_station, railway_station,
    railway_halt, subway_station, tram_stop, ferry_terminal,
    aerialway_station. Unfiltered, that layer is dominated by
    bicycle_parking and by platform/stop_position — OSM's per-geometry
    re-tagging of the same physical stop — which this tool never returns
    by default; if zero stop-class rows are within radius_m it falls back
    to platform/stop_position rows instead of an empty answer, and says so
    in "note". `kind` restricts the search to exactly one class (any of
    the stop classes above, or "platform"/"stop_position" directly);
    an unrecognized kind returns a bad_request naming the accepted values.

    Returns {"center", "radius_m", "results": [{"id", "kind", "name",
    "distance_m"}, ...], "total_in_range"}, plus "truncated": true and an
    explanatory "note" whenever fewer rows came back than matched, and a
    "note" when the fallback fired or the search found nothing at all. id
    is the GERS id, usable with other GERS-keyed tools. No raw geometry and
    no schedules or live arrivals — Overture base/infrastructure is a
    static OpenStreetMap conflation, and neither exists in it even in
    principle; a caller wanting "next bus" needs a live transit API.

    An empty results list is a valid answer, not an error: base-theme
    coverage is OSM-derived and patchy, and "no stop within radius_m" is a
    real finding. radius_m echoes the effective radius, which may be lower
    than requested (large values are clamped).

    Returns a structured {"error": ...} if upstream is unavailable or the
    dataset is missing geometry/bbox, and {"error": "bad_request"} for a
    non-finite or out-of-range coordinate, or an unrecognized kind.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    if kind is not None and kind not in transit.ALLOWED_KINDS:
        return {
            "error": "bad_request",
            "detail": f"unrecognized kind {kind!r}; accepted values: "
            f"{', '.join(sorted(transit.ALLOWED_KINDS))}",
        }
    try:
        rows, effective_radius_m, total_in_range, fallback_used, class_missing = (
            transit.transit_stops_near(lat, lon, radius_m, limit, kind=kind)
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
    result = _with_transit_notes(result, total_in_range, fallback_used, class_missing)
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
def geocode(
    query: str, limit: int = 5, lang: _LangArg = None, country: str | None = None,
) -> dict:
    """Free-text place name -> ranked candidate locations, from Overture divisions and places.

    No Nominatim, no third-party geocoding API. Matches localities,
    neighborhoods, regions, and countries by name (exact > prefix >
    substring), falling back to named places if that doesn't fill `limit`.
    Returns {"results": [{name, type, lat, lon, id (GERS), admin_context,
    rank_score}, ...]}, budgeted like every other tool. Returns a structured
    {"error": ...} instead of raising if the remote scan fails.

    A trailing "City, ST"/"City, Region" suffix has resolved against a US
    state or another region since #46; a trailing "City, Country" suffix
    resolves the same way (#457) — "Cambridge, UK", "Cambridge, GB",
    "Cambridge, GBR" and "Cambridge, United Kingdom" all constrain the
    search to the UK's Cambridge, excluding any same-named division
    elsewhere. `country` (ISO 3166-1 alpha-2, also accepting alpha-3 and
    aliases like "UK"/"USA", case-insensitive) is the explicit form of the
    same constraint, for a caller that already knows the country instead
    of writing it into `query`. An unrecognized `country`, or one that
    disagrees with a country/region suffix parsed off `query` itself,
    returns {"error": "bad_request", ...} naming both. A comma-suffix that
    names neither a region nor a country still searches the name half
    alone rather than returning nothing, with a "note" naming the
    unrecognized qualifier; a recognized country/region that matches
    nothing degrades the same way, with a "note".

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

    lang (#410) requests Overture's language-tagged name variant instead
    of a division row's primary name, when the data has one for that row
    and language: `name` becomes the variant and `name_primary` is added
    only when it differs. Default: the stored `preferences()` lang, else
    the primary name unchanged. Never invented or transliterated — only a
    variant actually present in Overture's data is ever returned.
    `PLACEROOT_HOME=<city/area>` (#406) sets a home region once at startup;
    a bounded score bonus then nudges same-tier ambiguous namesakes (the
    "which Springfield" case) toward it — a bias, never a filter, so a
    distant result stays in the answer, just not first. Only when the bias
    actually changed the top result does a "note" say so, e.g. "ranked
    toward your configured home region (Seattle); pass a city/near hint to
    override". No home configured -> no bias, no note, behavior unchanged.
    """
    lang = preference_store.resolve_lang(lang)
    try:
        result = geocoding.geocode_detailed(query, limit, lang=lang, country=country)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    payload = budget.apply_budget({"results": result["results"]}, "results")
    if "note" in result:
        payload["note"] = result["note"]
    return payload


@_tool("Geocode names in batch")
def geocode_batch(
    queries: list[str], limit_per_query: int = 3, country: str | None = None,
) -> dict:
    """Geocode up to 20 free-text queries in one call, one best match each.

    Cuts N round-trips of geocode() into one and, more importantly,
    shares ONE local divisions name table across the batch (#329) so a
    two-name walk is not N cold S3 scans. For each query, keeps only
    the top candidate.
    Returns {"results": [{"query", "name", "type", "lat", "lon", "id"
    (GERS), "rank_score"}, ...]}, one row per query, in input order — a
    query with no match gets the standard error envelope {"query",
    "error": "not_found", "detail"} instead, and does not fail the rest of
    the batch. queries is capped at 20; a longer list returns a structured
    {"error": ...} rather than truncating silently. Budgeted like every
    other tool. Returns a structured {"error": ...} instead of raising if
    the remote scan itself fails.

    country (#457, ISO 3166-1 alpha-2, also accepting alpha-3 and aliases
    like "UK"/"USA", case-insensitive) applies the same "City, Country"
    constraint geocode() takes to every query in the batch — e.g.
    `geocode_batch(["Cambridge", "London"], country="CA")` answers with
    Ontario's London and a not_found row for Cambridge (no Canadian
    Cambridge in the dataset), rather than the UK/US namesakes each query
    would otherwise resolve to on its own. An unrecognized `country`, or a
    per-query suffix that disagrees with it, returns a structured
    {"error": "bad_request", ...} for the whole call.
    """
    if len(queries) > 20:
        return {
            "error": "bad_request",
            "detail": f"geocode_batch accepts at most 20 queries, got {len(queries)}",
        }
    try:
        rows = geocoding.geocode_batch(queries, limit_per_query, country=country)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    return budget.apply_budget({"results": rows}, "results")


@_tool("Search categories")
def search_categories(query: str, limit: int = 8) -> dict:
    """Free text -> valid Overture category slugs, for the `category` param
    the place-search and area-summary tools take.

    Lookup only — no geo filtering, no upstream dataset dependency; matches
    against a bundled snapshot of Overture's places taxonomy (pinned to
    schema v1.9.0). Ranks exact slug match > slug prefix > slug substring >
    a match on any taxonomy path segment, so close siblings like "cafe" vs
    "coffee_shop" both surface rather than one silently winning. If the
    whole query matches nothing, falls back to a lexical phrase-intent
    match against a curated synonym lexicon (e.g. "fix my cracked phone
    screen" -> mobile_phone_repair). Returns {"results": [{"slug", "path",
    "confidence"}, ...]} — path is the root-to-leaf taxonomy (e.g.
    ["eat_and_drink", "cafe", "coffee_shop"]), confidence is 0-1 and
    descending, budgeted like every other tool. An empty/whitespace query
    returns {"results": []}. limit is clamped to 0-50, matching every
    other tool's limit handling (out-of-range values are not an error).
    """
    limit = max(0, min(int(limit), 50))
    rows = categories.search_categories(query, limit)
    return budget.apply_budget({"results": rows}, "results")


@_tool("Resolve place to GERS id")
def resolve_place(
    query: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
    limit: int = 3,
    city: str | None = None,
    lang: _LangArg = None,
    country: str | None = None,
) -> dict:
    """Free-text place reference -> ranked, typed GERS ids to hold onto.

    Turns something like "the Whole Foods on Lamar" or "Travis County" into
    stable Overture ids: merges geocode()'s division matches (locality,
    region, county, country, ...) with a name-filtered find_places search
    (a business or POI), bbox-limited to near_lat/near_lon if given, else to
    the ~20km vicinity of the top division match.

    **Split the location out of the query, and pass `city`.** You know
    things this server does not: that "san jose airport" means San Jose,
    California, that the Eiffel Tower is in Paris, that a user asking about
    "BASIS Silicon Valley" means Sunnyvale. This server knows only what
    exists at which coordinates in the current Overture release. When the
    location arrives inside one string, it has to guess which words are the
    place — and it guesses from map data alone, where "san" names a division
    in Henan and "palo" names one in Leyte. Given `city="San Jose, CA"` and
    `query="airport"` there is nothing to guess.

    A wrong hint costs a miss and a retry, never a wrong answer: `city`
    only bounds where the search looks, and the returned rows still come
    from the data. Pass `near_lat`/`near_lon` instead when you have real
    coordinates — they are the strongest hint of all.

    When nothing resolves for want of a location, the reply carries
    `need: "location"` and a `retry_with` sketch rather than only prose,
    so the second call can be made without parsing English.

    Returns {"results": [{"id" (GERS), "kind": "division" | "place",
    "name", "lat", "lon", "match": "exact" | "prefix" | "substring" |
    "fuzzy", plus "admin_context" for a division or "category" for a
    place}, ...]}, ranked by match tier then prominence ("fuzzy" — reached
    by close spelling rather than by containing the query at all, #215 for
    divisions and #373 for places — ranking below every literal match).
    A place found through #373's alt-spelling/typo fallback additionally
    carries "matched_by": "alt_name" | "fuzzy", and a top-level "note"
    names the spelling actually matched. Budgeted like every other tool.
    An unresolvable query returns {"results": []} — not an error. Returns a
    structured {"error": ...} instead of raising if the remote scan fails
    or the places dataset is missing columns this tool depends on.

    lang (#410) requests Overture's language-tagged name variant, same as
    geocode() — but only for "kind": "division" rows; "kind": "place" rows
    (from find_places, out of scope for #410 this round) always carry
    their primary name. Default: the stored `preferences()` lang, else the
    primary name unchanged.
    Division candidates come from geocode() (#406), so a configured
    `PLACEROOT_HOME` nudges the same ambiguous-namesake ties this tool
    merges from — see geocode()'s docstring. resolve_place does not add its
    own disclosure note for that; its own ranking already leads with
    distance to `near_lat`/`near_lon`/`city` when one is given.

    country (#457, ISO 3166-1 alpha-2, also accepting alpha-3 and aliases
    like "UK"/"USA", case-insensitive) composes with `city`: it constrains
    the divisions half of the merge the same way it constrains geocode()
    itself — `resolve_place("Cambridge", country="GB")` resolves the UK
    Cambridge rather than whichever namesake ranks first unconstrained. An
    unrecognized `country`, or one that disagrees with a country/region
    suffix parsed off `query` itself, returns a structured
    {"error": "bad_request", ...} naming both.
    """
    if near_lat is not None and near_lon is not None:
        coord_error = _invalid_coord(near_lat, near_lon)
        if coord_error is not None:
            return coord_error
    lang = preference_store.resolve_lang(lang)
    try:
        rows = geocoding.resolve_place(
            query, near_lat, near_lon, limit, city=city, lang=lang, country=country,
        )
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.SchemaDegraded as e:
        return _schema_error(e)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    payload: dict = {"results": rows}
    if not rows and city is None and near_lat is None:
        # Machine-actionable instead of prose: the caller can retry without
        # parsing a sentence, and the thing it should add is the one thing
        # it already knows and we never will.
        payload["need"] = "location"
        payload["retry_with"] = {
            "query": query,
            "city": "<the city or region this is in, e.g. 'San Jose, CA'>",
        }
    # Note AFTER budgeting (#374), mirroring find_places: the note names a
    # concrete row, and attaching it first could leave it pointing at a row
    # apply_budget then dropped from the answer.
    return _with_name_fallback_note(budget.apply_budget(payload, "results"), query)


@_tool("Resolve GERS ids in batch")
def resolve_place_batch(gers_ids: list[str]) -> dict:
    """Resolve up to 25 GERS ids to compact place rows in one call.

    Collapses N place_details(id=...) round-trips into one: for each id,
    resolves it via the same lookup place_details uses and keeps only a
    compact row — {"gers_id", "name", "category", "lat", "lon"} — not the
    full place_details payload (addresses, websites, phones, socials,
    sources, brand, confidence, ...). Use place_details for full detail on
    a single id. Results are returned in input order; an id that doesn't
    resolve gets the standard error envelope {"gers_id", "error":
    "not_found", "detail"} instead and does not fail the rest of the
    batch. gers_ids is capped at 25; a longer list
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
                rows.append({
                    "gers_id": gers_id,
                    "error": "not_found",
                    "detail": f"no place matched GERS id {gers_id!r}",
                })
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

    The answer also carries top-level "country" (ISO 3166-1 alpha-2) and
    "region" (ISO 3166-2) beside admin_context, from the same nearest
    division the chain is built from — omitted, not null-filled, when the
    dataset or division lacks a value.
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
    that resolves to no boundary — or to something far larger than a city,
    like a state — returns an empty list plus a note rather than a scan. If a
    same-named runner-up in the same country supplies the boundary instead,
    the note names it — the answer is never silently about a different city,
    and never about one in another country. Check `anchor` (name, country,
    admin_context) to see which one it was. Street names match in either
    spelling (Parkway/Pkwy, West/W, NW/Northwest).

    Returns {"results": [{number, street, unit, postcode, country,
    distance_m, lat, lon}, ...], "anchor": {name, id, country,
    admin_context}, "match": "exact"|"nearest_number"|"street"},
    deduplicated to distinct number+street+postcode and nearest the city's
    own point first. More matches than `limit` adds "truncated",
    "distinct_in_range" and a note. `match` is absent only when no street
    was scanned at all (no street name, no city, or an unresolved anchor).

    A requested number with no address point is never interpolated: when the
    street has other numbered points, `results` holds the real nearest known
    numbers bracketing the miss instead (`match: "nearest_number"`, each row
    its own genuine coordinates, plus a note naming the miss and neighbors)
    — never a synthesized coordinate for the missing number. No usable
    numbers on the street falls to `match: "street"`, today's empty-plus-note.

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


@_tool("Find a street intersection")
def geocode_intersection(
    street_a: str,
    street_b: str,
    city: str,
) -> dict:
    """Locate where two named streets cross: "5th Avenue", "Main Street", "Portland".

    Resolves the city anchor, loads the street walk graph around its center
    (the city's extent, up to 5 km from its center), and finds the junctions
    inside it where an edge of one street meets an edge of the other.

    Street names match case-insensitively and through the same USPS abbreviation
    set as geocode_address (Avenue/Ave, Parkway/Pkwy, West/W, NW/Northwest,
    5th/Fifth), plus a directional suffix the caller left off ("Pennsylvania
    Avenue" finds "Pennsylvania Avenue NW").

    Returns {"results": [{"lat": ..., "lon": ..., "streets": [name_a, name_b]}, ...],
    "anchor": {"name": ..., "id": ..., "country": ..., "admin_context": [...]},
    "note": ...}. `streets` is the map's own spelling of the two streets at that
    crossing, in street_a/street_b order — not an echo of the inputs. When
    multiple crossings exist, results are ordered nearest to the city center
    first and capped at 5. If one or both streets do not resolve in the city,
    returns empty results and a note naming the unresolved street; "truncated":
    true means the street graph hit its size cap and may be missing some.
    """
    try:
        result = geocoding.geocode_intersection(
            street_a=street_a, street_b=street_b, city=city
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
    malformed point (missing/non-numeric lat/lon, or a lat/lon out of
    range) doesn't fail the whole batch — it yields the standard error
    envelope {"lat", "lon", "error": "bad_request", "detail"} in its slot
    instead.
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
                        "error": "bad_request",
                        "detail": "each point needs numeric 'lat' and 'lon'",
                    }
                )
                continue
            coord_error = _invalid_coord(lat, lon)
            if coord_error is not None:
                rows.append({
                    "lat": lat,
                    "lon": lon,
                    "error": "bad_request",
                    "detail": coord_error["detail"],
                })
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


# op -> the geometry_op() params that op needs set. Single source of truth
# for both the missing-params error message and the dispatch below, so the
# two can't drift on what an op requires.
_GEOMETRY_OP_REQUIRED: dict[str, tuple[str, ...]] = {
    "distance": ("point", "point2"),
    "bearing": ("point", "point2"),
    "destination": ("point", "bearing_deg", "distance_m"),
    "midpoint": ("point", "point2"),
    "area": ("geometry",),
    "length": ("geometry",),
    "bbox": ("geometry",),
    "centroid": ("geometry",),
    "buffer": ("point", "radius_m"),
    "convex_hull": ("points",),
    "point_in_polygon": ("points", "geometry"),
    "nearest_point": ("point", "points"),
    "nearest_point_on_line": ("point", "geometry"),
    "union": ("geometry", "geometry2"),
    "intersect": ("geometry", "geometry2"),
    "difference": ("geometry", "geometry2"),
}
_OpArg = Annotated[
    str,
    Field(
        description="Geometry operation; each takes a different subset of the "
        "other arguments — see below.",
        json_schema_extra={"enum": sorted(_GEOMETRY_OP_REQUIRED)},
    ),
]


def _point_coord_error(point, label: str) -> dict | None:
    """bad_request dict if point isn't a well-formed, in-range {"lat","lon"}, else None."""
    if not isinstance(point, dict):
        return {
            "error": "bad_request",
            "detail": f"{label} must be a {{'lat': ..., 'lon': ...}} object",
        }
    try:
        lat, lon = float(point.get("lat")), float(point.get("lon"))
    except (TypeError, ValueError):
        return {"error": "bad_request", "detail": f"{label} needs numeric 'lat' and 'lon'"}
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        coord_error["detail"] = f"{label}: {coord_error['detail']}"
        return coord_error
    return None


def _points_list_coord_error(points, label: str) -> dict | None:
    """bad_request dict if points isn't a non-empty, in-range, within-cap point list, else None."""
    if not isinstance(points, list) or not points:
        return {"error": "bad_request", "detail": f"{label} must be a non-empty list of points"}
    if len(points) > geometry_ops.MAX_BATCH_POINTS:
        return {
            "error": "bad_request",
            "detail": (
                f"{label} accepts at most {geometry_ops.MAX_BATCH_POINTS} points, "
                f"got {len(points)}"
            ),
        }
    for i, p in enumerate(points):
        coord_error = _point_coord_error(p, f"{label}[{i}]")
        if coord_error is not None:
            return coord_error
    return None


@_tool("Geometry operations")
def geometry_op(
    op: _OpArg,
    point: dict | None = None,
    point2: dict | None = None,
    points: list[dict] | None = None,
    geometry: dict | None = None,
    geometry2: dict | None = None,
    bearing_deg: float | None = None,
    distance_m: float | None = None,
    radius_m: float | None = None,
) -> dict:
    """Geometry math and predicates — one tool, many ops, no Overture scan.

    `op` selects the operation; pass only the params it needs (points are
    `{"lat": ..., "lon": ...}`; `geometry` is a GeoJSON object):

    - `distance(point, point2)` -> `{"distance_m"}` (great-circle haversine distance)
    - `bearing(point, point2)` -> `{"bearing_deg"}` (initial compass bearing)
    - `destination(point, bearing_deg, distance_m)` -> `{"point"}`
    - `midpoint(point, point2)` -> `{"point"}` (great-circle midpoint)
    - `area(geometry)` -> `{"area_m2", "area_km2"}` (Polygon/MultiPolygon)
    - `length(geometry)` -> `{"length_m"}` (LineString/MultiLineString)
    - `bbox(geometry)` -> `{"bbox": [xmin, ymin, xmax, ymax]}` (any geometry)
    - `centroid(geometry)` -> `{"point"}` (any geometry)
    - `buffer(point, radius_m)` -> `{"geometry"}` (Polygon, ~32-vertex circle approximation)
    - `convex_hull(points)` -> `{"geometry"}` (Polygon; points capped at 100)
    - `point_in_polygon(points, geometry)` -> `{"results": [bool, ...]}` (Polygon/MultiPolygon,
      holes honored; points capped at 100)
    - `nearest_point(point, points)` -> `{"index", "distance_m"}` (points capped at 100)
    - `nearest_point_on_line(point, geometry)` -> `{"point", "distance_m", "fraction"}` (LineString)
    - `union(geometry, geometry2)` -> `{"geometry", "area_km2"}` (Polygon/MultiPolygon, either slot)
    - `intersect(geometry, geometry2)` -> `{"geometry", "area_km2"}`, or `{"empty": true, "note"}`
      when the two inputs don't overlap
    - `difference(geometry, geometry2)` -> `{"geometry", "area_km2"}` (geometry minus geometry2),
      or `{"empty": true, "note"}` when geometry2 fully covers geometry

    `buffer`, `convex_hull`, and `union`/`intersect`/`difference` are the
    ops that return geometry; that output is simplified to fit the same
    token budget `simplify_geometry`'s own default targets, so there's no
    need to chain a second call. `union`/`intersect`/`difference` run via
    the DuckDB spatial extension already loaded for other tools (see
    geometry_setops.py) rather than geometry_ops.py's pure-Python math.

    An unknown op returns `{"error": "bad_request", ...}` listing valid ops.
    Missing/wrong-shaped params for the given op return `{"error":
    "bad_request", ...}` naming exactly what that op needs, e.g. "op=buffer
    needs point and radius_m". Point-like inputs are range-checked (lat in
    [-90, 90], lon in [-180, 180]); `geometry`/`geometry2` get structural
    validation only (right type, non-empty numeric coordinates) — see
    geometry_ops.py's module docstring for the accuracy notes behind
    area/centroid (a local meters projection, not a geodesic computation)
    and buffer/convex_hull (planar approximations, fine at city/regional
    scale).
    """
    if op not in _GEOMETRY_OP_REQUIRED:
        return {
            "error": "bad_request",
            "detail": f"unknown op {op!r}; valid ops: {', '.join(sorted(_GEOMETRY_OP_REQUIRED))}",
        }
    required = _GEOMETRY_OP_REQUIRED[op]
    provided = {
        "point": point,
        "point2": point2,
        "points": points,
        "geometry": geometry,
        "geometry2": geometry2,
        "bearing_deg": bearing_deg,
        "distance_m": distance_m,
        "radius_m": radius_m,
    }
    if any(provided[name] is None for name in required):
        return {"error": "bad_request", "detail": f"op={op} needs {' and '.join(required)}"}

    if "point" in required:
        coord_error = _point_coord_error(point, "point")
        if coord_error is not None:
            return coord_error
    if "point2" in required:
        coord_error = _point_coord_error(point2, "point2")
        if coord_error is not None:
            return coord_error
    if "points" in required:
        coord_error = _points_list_coord_error(points, "points")
        if coord_error is not None:
            return coord_error

    try:
        if op == "distance":
            result = geometry_ops.distance(point, point2)
        elif op == "bearing":
            result = geometry_ops.bearing(point, point2)
        elif op == "destination":
            result = geometry_ops.destination(point, bearing_deg, distance_m)
        elif op == "midpoint":
            result = geometry_ops.midpoint(point, point2)
        elif op == "area":
            result = geometry_ops.area(geometry)
        elif op == "length":
            result = geometry_ops.length(geometry)
        elif op == "bbox":
            result = geometry_ops.bbox(geometry)
        elif op == "centroid":
            result = geometry_ops.centroid(geometry)
        elif op == "buffer":
            result = geometry_ops.buffer(point, radius_m)
        elif op == "convex_hull":
            result = geometry_ops.convex_hull(points)
        elif op == "point_in_polygon":
            result = geometry_ops.point_in_polygon(points, geometry)
        elif op == "nearest_point":
            result = geometry_ops.nearest_point(point, points)
        elif op == "nearest_point_on_line":
            result = geometry_ops.nearest_point_on_line(point, geometry)
        elif op == "union":
            result = geometry_setops.union(geometry, geometry2)
        elif op == "intersect":
            result = geometry_setops.intersect(geometry, geometry2)
        else:  # difference
            result = geometry_setops.difference(geometry, geometry2)
    except geometry_ops.InvalidGeometryOp as e:
        return {"error": "bad_request", "detail": e.detail}
    except overture.UpstreamUnavailable as e:
        # Only the set ops can raise this: loading the spatial extension can
        # hit the network once on a cold install (geometry_setops).
        return _upstream_error(e)
    # point_in_polygon's "results" list is positional (one boolean per input
    # point) and already bounded by geometry_ops.MAX_BATCH_POINTS, so it is
    # never token-budgeted: truncating it would silently misalign results
    # with the input points.
    return result


@_tool("Render map", annotations=_WRITES_A_FILE_ANNOTATIONS)
def render_map(
    result: dict | list,
    title: str | None = None,
    inline: bool = False,
    summary: str | None = None,
    legend: dict | None = None,
) -> dict:
    """Render any result as a shareable one-pager: map, verdict, and stop list.

    Writes ONE self-contained HTML file — interactive SVG map (inline CSS/JS,
    vector markers with labels and click popups, polygon/line shapes
    including reachability output shaped {"polygon": ..., "stats": {...}}),
    a composed verdict, per-stop details, a scale bar, and required
    attribution. A shape feature's properties may carry "role": "shed"
    (soft translucent fill, dashed edge — for travel-time sheds) or
    "role": "outline" (no fill, strong edge — for a compared-area boundary);
    any other/absent role keeps the default style. Properties may also carry
    a short "label" and one-line "callout", rendered as a text chip over the
    shape (capped ~40/~80 chars); for the reachability payload, set
    role/label/callout at the payload's top level. No CDN, no tile server,
    no API key, zero
    network requests when opened — a local file the user can send as-is.
    Pass `summary` for
    the verdict you want on the page (the sentence you'd tell a spouse,
    co-founder, or landlord); when omitted a short fallback is composed
    from the payload. Written to PLACEROOT_ARTIFACT_DIR (default: alongside
    the tile cache directory). The file itself is the artifact; this tool's
    response stays small on purpose. Returns {"path", "bytes",
    "features_rendered", "skipped_features"} (plus "truncated": True when
    applicable) — skipped_features counts rows/features that couldn't be
    rendered (missing coordinates, malformed geometry, or dropped past
    mapview.MAX_RENDER_VERTICES) rather than failing the call outright. Pass
    inline=true to also get the HTML back in the response when it's small
    enough to be worth it.

    A point in `result` carrying a "class" property gets a contrasting
    marker dot when `legend` maps that class to {"label": str, "color":
    str?} — pass e.g. {"open": {"label": "Open now"}, "closed": {"label":
    "Closed", "color": "#d55e00"}}. A missing color is assigned from a
    fixed color-blind-safe palette; an invalid one (not #rgb/#rrggbb hex)
    is dropped rather than used. Classes actually present get a legend box
    on the page; a class not in `legend` keeps the default dot and is
    reported in the response's "note". Omitting `legend` (or a result with
    no "class" properties) renders exactly as before.
    """
    return mapview.write_artifact(
        result, title=title, inline=inline, summary=summary, legend=legend
    )

@_tool("Reachable area (isochrone)")
def isochrone(
    lat: float | None = None,
    lon: float | None = None,
    minutes: float = 15,
    mode: _ModeArgWalkDefault = None,
    speed_m_s: float | None = None,
    radius_m: float | None = None,
    where: dict | str | None = None,
) -> dict:
    """Isochrone: the area reachable from a point within `minutes`, by mode.

    Give the point as lat/lon, or as `where` — a {"lat", "lon"} dict, a
    GERS id, or a free-text place name — but not both (and not neither);
    either way returns {"error": "bad_request"} naming the choice. A
    `where` given as an id/name adds a compact "resolved": {"name", "id",
    "lat", "lon", "matched_by"} to the answer; absent for lat/lon or a
    {lat,lon} where.

    Builds a street graph from Overture's transportation theme and runs
    Dijkstra out to the time budget. Each mode
    excludes its own set of unusable road classes (e.g.
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
    have_lat, have_lon = lat is not None, lon is not None
    if where is not None and (have_lat or have_lon):
        return {
            "error": "bad_request",
            "detail": "pass either lat and lon, or where — not both",
        }
    if where is None and not (have_lat and have_lon):
        return {
            "error": "bad_request",
            "detail": "isochrone needs lat and lon, or where",
        }
    resolved_echo = None
    if where is not None:
        item, ref_error = _resolve_location_ref(where)
        if ref_error is not None:
            return ref_error
        lat, lon = item["lat"], item["lon"]
        if "matched_by" in item:
            resolved_echo = _location_ref_echo(item)
    else:
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    mode = preference_store.resolve_mode(mode, preference_store.DEFAULT_MODE_ISOCHRONE)
    try:
        result = routing.isochrone(
            lat, lon, minutes=minutes, mode=mode, speed_m_s=speed_m_s, radius_m=radius_m
        )
        if resolved_echo is not None:
            result["resolved"] = resolved_echo
        return result
    except routing.UnsupportedMode as e:
        return {
            "error": "unsupported_mode",
            "detail": e.detail,
            "supported": sorted(routing.MODE_CONFIG),
        }
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
    from_lat: float | None = None,
    from_lon: float | None = None,
    to_lat: float | None = None,
    to_lon: float | None = None,
    mode: _ModeArgDriveDefault = None,
    include_path: bool = False,
    include_elevation: bool = False,
    prefer: _PreferArg = None,
    avoid: _AvoidArg = None,
    confirm: bool = False,
    from_: str | dict | None = None,
    to: str | dict | None = None,
) -> dict:
    """Route: shortest-path distance and duration between two points, by mode.

    Give the two ends as from_lat/from_lon and to_lat/to_lon, or as
    from/to — each a {"lat", "lon"} dict, a GERS id, or a free-text place
    name — but not both (and not neither); either way returns
    {"error": "bad_request"} naming the choice. Do not call geocode(),
    resolve_place(), or geocode_batch() first: names and ids resolve in
    parallel inside this call, and the "from"/"to" blocks come back
    carrying whatever each end resolved to.

    Compact directions, not turn-by-turn: builds a street graph from
    Overture's transportation theme around the two points and returns
{"distance_m", "duration_s", "mode", "from", "to", "export"} for the
    fastest path — no polyline unless you ask for one. export is the
    pocket handoff: Google/Apple Maps directions URLs built from the same
    two coordinates (URL schemes only — no Maps API, no extra network), a
    GPX 1.1 document, and a printable stop list. Same cost model every routing tool
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
    islands of road data), returns {"error": "no_route", "try": ...}
    rather than raising — "try" is a mode-tuned next move (roadmap §4). If
    the extraction graph hit its internal size cap, the result
    carries "truncated": true — the route may be suboptimal or incomplete.

    include_path=true adds "path", a GeoJSON LineString from the origin's
    snapped node to the destination's that follows the streets' own
    geometry (curves included), simplified to fit the token budget, with
    "path_max_deviation_m" bounding how far it strays from the exact
    street path. Off by default (the polyline dwarfs the rest of the
    answer) — ask for it only to draw or trace the route. If even a fully
    simplified line won't fit, you get "path_omitted": true instead of a
    line that stops short of the destination.

    include_elevation=true adds "elevation": a compact climb profile from
    the same keyless Copernicus GLO-30 DEM reader as point elevation lookups
    use, sampled along the route —
    "total_climb_m", "total_descent_m", "max_grade_pct", and a small
    "samples" array of [distance_along_m, elevation_m] points, thinned to
    fit the token budget. Off by default. Where the DEM has no coverage
    along part or all of the route, the affected numbers are never faked as
    0.0 — you get a "note" saying so instead (and no climb/descent/grade
    keys at all if there's no coverage anywhere on the route). If even the
    note-only form can't fit the budget, you get "elevation_omitted": true.

    prefer="flat" asks the router to trade distance for climb — steeper
    detours cost more than gentler ones, so a longer-but-gentler path can
    win over a shorter-but-steeper one. Only meaningful for mode="walk" or
    "cycle" (returns {"error": "bad_request"} for mode="drive"); needs
    per-node elevations for the extracted street graph, fetched from the
    same Copernicus source (bounded — see routing.FLAT_MAX_ELEVATION_NODES),
    so if that data isn't reachable or has no coverage here, the route falls
    back to plain-distance routing and says so in "prefer_note" rather than
    silently ignoring the preference. IMPORTANT: prefer="flat" minimizes
    elevation grade only — it is NOT a step-free, stroller-, or
    wheelchair-accessible mode. Overture's transportation data (as read
    here) carries no step-count, kerb-ramp, or surface attributes, so a
    flight of stairs classified as ordinary walkway geometry can still
    appear on a "flat" route if it's short and roughly level. Don't offer
    this as an accessibility guarantee to the user; it isn't one.

    avoid=["motorway"] (and/or "trunk") is the "no highways" ask: those
    classes and their on/off ramps are dropped from the street graph before
    the search, and the answer echoes "avoid". Those two values are the
    whole vocabulary — anything else is a bad_request listing them. There is
    no toll or ferry option, deliberately: Overture's road data carries no
    toll attribute at all, and this graph is road-only so ferries are never
    routed over. Tell the user that rather than approximating either with
    avoid=["motorway"]. On walk and cycle it is a no-op (both already
    exclude those classes) and says so in "avoid_note" instead of erroring.
    An avoiding route is a different graph, so the first one in an area can
    need its own confirm even where a plain route is warm; if the avoided
    roads were the only link, the usual no_route comes back with "try"
    naming avoid.

    confirm=true after the user agreed to wait for a first-time street-graph
    build (about 5–25 seconds). Pass it only after a needs_confirm reply
    and they said yes. A warm or cached graph never needs it.
    Omit confirm unless you just asked and they said yes.

    A from/to name matching several equally-ranked places returns
    {"error": "ambiguous_place", "candidates": [...]} instead of picking a
    city; an unresolvable name or id returns {"error": "not_found"}; a
    malformed one (empty string, dict missing lat/lon, wrong type) returns
    {"error": "bad_request"} — the offending side is named in "field".
    Ends that resolve a city apart return {"error": "too_far"} with both
    ends and the mode cap, before any graph is built. from_to is this same
    routing with a walk default.
    """
    have_ref = from_ is not None or to is not None
    have_scalar = any(v is not None for v in (from_lat, from_lon, to_lat, to_lon))
    if have_ref and have_scalar:
        return {
            "error": "bad_request",
            "detail": (
                "pass either from_lat/from_lon and to_lat/to_lon, or from and to — not both"
            ),
        }
    if have_ref:
        if from_ is None or to is None:
            return {
                "error": "bad_request",
                "detail": "route needs both from and to",
                "field": "from" if from_ is None else "to",
            }
        return _route_between_refs(
            from_,
            to,
            mode=mode,
            default_mode=preference_store.DEFAULT_MODE_ROUTE,
            include_path=include_path,
            include_elevation=include_elevation,
            prefer=prefer,
            avoid=avoid,
            confirm=confirm,
            tool="route",
        )
    if not all(v is not None for v in (from_lat, from_lon, to_lat, to_lon)):
        return {
            "error": "bad_request",
            "detail": "route needs from_lat, from_lon, to_lat and to_lon, or from and to",
        }
    for lat, lon in ((from_lat, from_lon), (to_lat, to_lon)):
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    mode = preference_store.resolve_mode(mode, preference_store.DEFAULT_MODE_ROUTE)
    if mode not in routing.MODE_CONFIG:
        return {
            "error": "unsupported_mode",
            "detail": f"unsupported mode {mode!r}; supported: {sorted(routing.MODE_CONFIG)}",
            "supported": sorted(routing.MODE_CONFIG),
        }
    if prefer is not None and prefer not in routing.SUPPORTED_PREFERENCES:
        return {
            "error": "bad_request",
            "detail": (
                f"unsupported prefer={prefer!r}; supported: "
                f"{sorted(routing.SUPPORTED_PREFERENCES)}"
            ),
        }
    if prefer == routing.PREFER_FLAT and mode not in routing.FLAT_PREFERENCE_MODES:
        return {
            "error": "bad_request",
            "detail": (
                "prefer='flat' is only supported for mode in "
                f"{sorted(routing.FLAT_PREFERENCE_MODES)}, got mode={mode!r}"
            ),
        }
    try:
        avoid_classes = routing.normalize_avoid(avoid)
    except ValueError as e:
        return {
            "error": "bad_request",
            "detail": str(e),
            "supported": list(routing.AVOIDABLE_CLASSES),
        }
    straight_m = routing._haversine_m(from_lat, from_lon, to_lat, to_lon)
    cap_m = routing.ROUTE_MAX_STRAIGHT_LINE_M[mode]
    if straight_m > cap_m:
        return {
            "error": "route_too_long",
            "detail": (
                f"straight-line distance {straight_m:.0f}m exceeds the "
                f"{cap_m:.0f}m cap for this mode"
            ),
            "max_distance_m": cap_m,
        }
    cached = routing.route_graph_is_cached(
        from_lat, from_lon, to_lat, to_lon, mode, want_shapes=include_path, avoid=avoid_classes
    )
    if not cached and not confirm:
        return _needs_confirm_graph(mode)
    if cached:
        progress.report(f"Routing a {mode}…")
    try:
        result = _run_route(
            from_lat, from_lon, to_lat, to_lon,
            mode=mode, include_path=include_path,
            include_elevation=include_elevation, prefer=prefer, avoid=avoid_classes,
            cap_confirm_build=confirm and not cached,
        )
    except routing.UnsupportedMode as e:
        return {
            "error": "unsupported_mode",
            "detail": e.detail,
            "supported": sorted(routing.MODE_CONFIG),
        }
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
    if "error" not in result:
        result["export"] = export.from_route_result(result)
    # Middleware attach()s again; idempotent (same request log, status not clobbered).
    return progress.attach(result)


@_tool("Elevation at a point")
def elevation_at(lat: float, lon: float) -> dict:
    """Ground elevation in meters at a point, from Copernicus GLO-30 (~30 m resolution).

    Reads the Copernicus DEM directly from AWS Open Data (no API key, no
    third-party elevation service) — the same open-data pattern every other
    tool here uses, just a different bucket than Overture's. Nearest-cell
    sampling, not interpolated: at ~30 m ground resolution the answer is
    "the elevation of the DEM cell containing this point", which can be off
    by a few meters from the exact spot on a steep slope.

    Returns {"elevation_m": <float>}. No coverage at this point — open
    ocean, or a tile the Copernicus release excludes from public
    distribution — is a real, non-error answer: {"elevation_m": null,
    "note": "..."} explaining why. Returns a structured {"error": ...} for
    an out-of-range coordinate, or if the DEM tile can't be fetched
    (network/upstream failure).

    Attribution: Copernicus DEM © DLR/ESA, accessed via AWS Open Data.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        return elevation.elevation_at(lat, lon)
    except elevation.ElevationFormatError as e:
        return {"error": "upstream_unavailable", "detail": e.detail, "retry_advised": False}
    except errors.UpstreamUnavailable as e:
        return _upstream_error(e)


def _map_match_geometry(latlon: list[tuple[float, float]]) -> dict:
    """The matched polyline as a GeoJSON LineString, budget-simplified the
    same way geometry_op's geometry-returning ops are (geometry_ops.py's
    _budget_simplify) — reused rather than re-implemented so map_match and
    geometry_op can't drift on what "fits the token budget" means.

    RFC 7946 requires a LineString to carry two or more positions, so 0 or
    1 stitched vertices (nothing matched, or a single isolated anchor)
    normalizes to EMPTY coordinates — the same shape the all-unmatched
    answer carries — rather than emitting a one-position LineString no
    GeoJSON consumer is required to accept. The single anchor's position
    isn't lost to the caller: it's the snapped position of the one matched
    point, and map_match's note explains the situation.
    """
    if len(latlon) < 2:
        return {"geometry": {"type": "LineString", "coordinates": []}}
    geometry = {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in latlon]}
    fitted = simplify.simplify_geometry(geometry, geometry_ops.GEOMETRY_MAX_TOKENS)
    out = {"geometry": fitted["geometry"]}
    if fitted["kept_points"] < fitted["original_points"]:
        out["max_deviation_m"] = fitted["max_deviation_m"]
        out["original_points"] = fitted["original_points"]
        out["kept_points"] = fitted["kept_points"]
    return out


@_tool("Snap a GPS trace to streets")
def map_match(points: list[dict], mode: _ModeArgWalkDefault = None) -> dict:
    """Map-match a GPS trace: snap points onto the street graph, stitch a route.

    points is an ordered list of {"lat": ..., "lon": ...} — up to 100 of
    them (Mapbox's Map Matching tool takes the same cap; anything past it
    returns {"error": "bad_request"} rather than truncating the trace
    silently). A malformed point (missing/non-numeric lat or lon, or a
    coordinate out of range) returns {"error": "bad_request"} naming the
    offending index as "points[i]: ...".

    Returns {"matched_length_m", "roads", "confidence", "geometry",
    "unmatched_points"}: matched_length_m is the summed routed (not
    straight-line) distance across every stitched leg; roads lists the
    street names crossed in travel order, deduplicated only where they
    repeat back to back; geometry is the stitched polyline as a GeoJSON
    LineString, simplified to fit the same token budget every geometry-
    returning tool here targets; unmatched_points lists the input
    indices that never made it into the stitched route — points too far
    from any street, and points the stitching's outlier guard rejected
    (a routed detour wildly out of proportion to the straight-line gap,
    e.g. a trace point that jumped to a different block). confidence
    (0..1) blends how much of the trace stitched against how far the
    matched points sat from their snapped street position.

    A trace that matches nothing is a real answer, not an error:
    matched_length_m 0, empty roads/geometry, every index in
    unmatched_points, with a "note" explaining it. Do not treat that shape
    as a failure — it means the trace is genuinely off the mapped street
    network here, which is itself useful to know. A trace where only ONE
    point matches gets the same empty-geometry shape with its own note: a
    single position can't make a LineString (RFC 7946 requires two) or a
    route.

    Offline graph work, same as route()/isochrone(): builds one street
    graph from Overture's transportation theme over the trace's own padded
    bounding circle (no network beyond the existing tile/graph path) and
    reuses route()'s per-mode class exclusions, so e.g. a drive trace never
    snaps onto a footway. Omit mode to use the stored preferences mode,
    else walk. An unrecognized mode string returns
    {"error": "unsupported_mode"}. If no usable street graph exists near
    the trace at all, returns {"error": "no_graph_nearby"}; a trace whose
    padded bounding circle exceeds the mode's extraction cap (e.g. a walk
    trace spanning well past ~10 km) returns {"error": "radius_too_large",
    "max_radius_m": ...} — try the trace in segments, or a faster mode; a
    scan that can't reach the upstream Overture data returns
    {"error": "upstream_unavailable", "retry_advised": true}; missing
    essential source columns returns {"error": "schema_degraded"}.

    Attribution: street geometry and names from Overture Maps
    transportation data.
    """
    if not isinstance(points, list):
        return {
            "error": "bad_request",
            "detail": "points must be a list of {lat, lon} points",
        }
    if len(points) > mapmatch.MAX_TRACE_POINTS:
        return {
            "error": "bad_request",
            "detail": (
                f"points must hold at most {mapmatch.MAX_TRACE_POINTS} points, "
                f"got {len(points)}"
            ),
        }
    parsed_points: list[dict] = []
    for idx, p in enumerate(points):
        if not isinstance(p, dict):
            return {"error": "bad_request", "detail": f"points[{idx}] must be an object"}
        try:
            plat, plon = float(p["lat"]), float(p["lon"])
        except (KeyError, TypeError, ValueError) as e:
            return {
                "error": "bad_request",
                "detail": f"points[{idx}]: needs numeric lat and lon: {e}",
            }
        coord_error = _invalid_coord(plat, plon)
        if coord_error is not None:
            coord_error["detail"] = f"points[{idx}]: {coord_error['detail']}"
            return coord_error
        parsed_points.append({"lat": plat, "lon": plon})

    mode = preference_store.resolve_mode(mode, preference_store.DEFAULT_MODE_ROUTE)
    if mode not in routing.MODE_CONFIG:
        return {
            "error": "unsupported_mode",
            "detail": f"unsupported mode {mode!r}; supported: {sorted(routing.MODE_CONFIG)}",
            "supported": sorted(routing.MODE_CONFIG),
        }

    try:
        matched = mapmatch.match_trace(parsed_points, mode=mode)
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    except routing.UnsupportedMode as e:
        return {
            "error": "unsupported_mode",
            "detail": e.detail,
            "supported": sorted(routing.MODE_CONFIG),
        }
    except errors.UpstreamUnavailable as e:
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

    result = {
        "matched_length_m": matched.matched_length_m,
        "roads": matched.road_names,
        "confidence": matched.confidence,
        **_map_match_geometry(matched.geometry),
        "unmatched_points": matched.unmatched_indices,
    }
    # A single stitched vertex is as routeless as none for the caller's
    # purposes: no roads, no line — so both get a note, and
    # _map_match_geometry has already normalized the <2-vertex geometry to
    # an empty (RFC 7946-valid) LineString.
    if not matched.road_names and len(matched.geometry) < 2:
        if len(matched.geometry) == 1:
            result["note"] = (
                "Only one point matched the street graph, which is not "
                "enough to stitch a route — matched_length_m is 0 and "
                "geometry is empty. This is a real answer, not a failure."
            )
        else:
            result["note"] = (
                "Nothing in this trace matched the street graph within "
                "snapping range — every point is in unmatched_points. This "
                "is a real answer: the trace may be off the mapped street "
                "network here, or too far from it for this mode."
            )
    return result


@_tool("Timezone at a point")
def timezone_at(lat: float, lon: float) -> dict:
    """IANA timezone and current local time at a point, fully offline.

    Looks up the tzdb zone containing (lat, lon) from timezone-boundary-
    builder polygons (via tzfpy, bundled — no network, no third-party
    timezone API) and derives the rest from stdlib zoneinfo against the
    current instant.

    Returns {"tzid": "America/Los_Angeles", "utc_offset": "-07:00",
    "dst_active": true, "local_time": "2026-08-30T14:05:00-07:00",
    "abbreviation": "PDT"}. Open ocean generally still resolves — to a
    fixed-offset, no-DST "Etc/GMT±N" nautical zone rather than a named
    tzdb zone — but a point with no resolvable zone at all is a real,
    non-error answer: {"tzid": null, "note": "..."}. Returns a structured
    {"error": ...} for an out-of-range coordinate.

    Attribution: IANA tzdb via tzfpy / timezone-boundary-builder.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    return timezone_lookup.timezone_at(lat, lon)


@_tool("Named-place route")
def from_to(
    from_: str | dict,
    to: str | dict,
    mode: _ModeArgWalkDefault = None,
    include_path: bool = False,
    include_elevation: bool = False,
    prefer: _PreferArg = None,
    avoid: _AvoidArg = None,
    confirm: bool = False,
) -> dict:
    """Shortest-path walk, cycle, or drive between two places.

    from_to is route() with LocationRef ends and a walk default; route is
    the canonical routing tool and is growing the same from/to ends, so
    prefer route(from=..., to=...) once it takes them.

    Pass each of from/to as a free-text place name, a {"lat", "lon"} dict,
    or a GERS id — mixed freely. Do not call geocode(), resolve_place(), or
    geocode_batch() first. Plain names resolve in parallel exactly as
    before; coordinates pass through untouched. Builds one street graph and
    returns distance, duration, export maps/gpx/text, and a "from"/"to"
    block carrying whatever the input resolved to (name/id when it was a
    name or GERS id, lat/lon always).

    A comma qualifies: "Alamo Square, SF" searches inside SF only.

    If a name matches several equally-ranked places, returns
    {"error": "ambiguous_place", "candidates": [...]} instead of picking
    a city. If the two ends resolve a city apart, returns
    {"error": "too_far"} with the resolved ends and the mode cap rather
    than extracting a continent graph. Same per-mode straight-line caps
    as a coordinate route (walk ~7.5 km, cycle ~23.5 km, drive ~95.5 km).
    An unresolvable name or GERS id returns {"error": "not_found"}; a
    malformed from/to (empty string, dict missing lat/lon, wrong type)
    returns {"error": "bad_request"} — either way the offending side is
    named in "field": "from" | "to". Omit mode to use the stored
    preferences mode, else walk.

    include_path, include_elevation, prefer, and avoid pass straight through
    to route() — see that tool's docstring for what each returns/means
    ("elevation" climb profile, prefer="flat" grade-avoiding preference and
    its honest step-free/accessibility caveats, avoid=["motorway"|"trunk"]
    class avoidance and why no toll or ferry option exists). avoid needs
    mode="drive": the walk default already excludes those classes.

    confirm=true after the user agreed to wait for a first-time street-graph
    build (about 5–25 seconds). Pass it only after a needs_confirm reply
    and they said yes. A warm or cached graph never needs it.
    Omit confirm unless you just asked and they said yes.
    """
    return _route_between_refs(
        from_,
        to,
        mode=mode,
        default_mode="walk",
        include_path=include_path,
        include_elevation=include_elevation,
        prefer=prefer,
        avoid=avoid,
        confirm=confirm,
        tool="from_to",
    )


def _route_between_refs(
    from_,
    to,
    *,
    mode,
    default_mode: str,
    include_path: bool,
    include_elevation: bool,
    prefer,
    avoid=None,
    confirm: bool,
    tool: str,
) -> dict:
    """Resolve two LocationRefs, then route between them — from_to's body, shared.

    route() reaches this when it is called with from/to instead of the four
    scalars (#419); from_to() is this call with a walk default. The two
    differ only in `default_mode` and the tool named in the too_far detail,
    so the resolution semantics — parallel resolve of plain-name pairs,
    per-side "field", the too_far guard before any graph is extracted, and
    the resolved name/id echoed onto the answer's "from"/"to" — are one
    implementation rather than two that can drift.
    """
    origin, dest, end_error = _resolve_route_ends(from_, to)
    if end_error is not None:
        return end_error
    mode = preference_store.resolve_mode(mode, default_mode)
    if mode not in routing.MODE_CONFIG:
        return {
            "error": "unsupported_mode",
            "detail": f"unsupported mode {mode!r}",
            "supported": sorted(routing.MODE_CONFIG),
        }
    straight_m = geo.haversine_m(origin["lat"], origin["lon"], dest["lat"], dest["lon"])
    cap_m = routing.ROUTE_MAX_STRAIGHT_LINE_M[mode]
    if straight_m > cap_m:
        origin_label = origin.get("name") or f"({origin['lat']}, {origin['lon']})"
        dest_label = dest.get("name") or f"({dest['lat']}, {dest['lon']})"
        return {
            "error": "too_far",
            "detail": (
                f"{origin_label!r} and {dest_label!r} are {round(straight_m)} m "
                f"apart; {tool} stays inside one city ({mode} cap {round(cap_m)} m)"
            ),
            "from": origin,
            "to": dest,
            "distance_m": round(straight_m, 1),
            "max_distance_m": cap_m,
            "mode": mode,
        }
    result = route(
        origin["lat"], origin["lon"], dest["lat"], dest["lon"], mode=mode,
        include_path=include_path, include_elevation=include_elevation, prefer=prefer,
        avoid=avoid, confirm=confirm,
    )
    for key, place in (("from", origin), ("to", dest)):
        point = result.get(key)
        if isinstance(point, dict):
            for field in ("name", "id", "type", "admin_context"):
                if place.get(field) is not None:
                    point[field] = place[field]
    if "error" not in result:
        # Intentional overwrite: route() already attached export; rebuild so
        # maps/gpx/text use the named endpoints rather than bare coordinates.
        result["export"] = export.from_route_result(result)
    return result


def _resolve_route_ends(from_, to) -> tuple[dict | None, dict | None, dict | None]:
    """Resolve a routing call's two LocationRef ends — (origin, dest, error).

    Exactly one of the two shapes comes back: (origin, dest, None) with both
    ends resolved to dicts carrying lat/lon (plus name/id/type/admin_context
    when the input was a name or GERS id), or (None, None, error) where the
    error dict names the offending side in "field": "from" | "to". Shared by
    _route_between_refs (route/from_to) and compare_modes so the end
    semantics — empty-string bad_request, the byte-identical parallel
    _resolve_pair fast path for two plain names, per-side field — are one
    implementation rather than two that can drift.
    """
    if isinstance(from_, str) and not from_.strip():
        return None, None, {
            "error": "bad_request",
            "detail": "from must be a non-empty place name",
            "field": "from",
        }
    if isinstance(to, str) and not to.strip():
        return None, None, {
            "error": "bad_request",
            "detail": "to must be a non-empty place name",
            "field": "to",
        }
    # Both ends still plain names (not GERS ids): the original, byte-
    # identical path — same parallel _resolve_pair call, same
    # ambiguous_place/not_found shape as before this feature existed.
    if (
        isinstance(from_, str) and isinstance(to, str)
        and not _GERS_ID_RE.match(from_.strip()) and not _GERS_ID_RE.match(to.strip())
    ):
        origin, dest = _resolve_pair(from_, to)
        if "error" in origin:
            return None, None, {**origin, "field": "from"}
        if "error" in dest:
            return None, None, {**dest, "field": "to"}
        return origin, dest, None
    origin, origin_error = _resolve_location_ref(from_)
    if origin_error is not None:
        return None, None, {**origin_error, "field": "from"}
    dest, dest_error = _resolve_location_ref(to)
    if dest_error is not None:
        return None, None, {**dest_error, "field": "to"}
    return origin, dest, None


# compare_modes' row order and the verbs its deterministic summary uses.
_COMPARE_MODES_DEFAULT = ("walk", "cycle", "drive")
_COMPARE_MODES_VERB = {"walk": "walking", "cycle": "cycling", "drive": "driving"}
# Keys a route() answer carries that a compact compare_modes row must not:
# geometry, export, endpoint echoes (the call carries one from/to), and the
# progress middleware's per-call status lines.
_COMPARE_MODES_DROP = frozenset(
    {"from", "to", "export", "path", "path_max_deviation_m", "path_omitted",
     "elevation", "elevation_omitted", "progress", "status", "timing", "mode"}
)


def _compare_modes_row(mode: str, origin: dict, dest: dict, *, include_elevation: bool,
                       confirm: bool) -> dict:
    """One compact per-mode row for compare_modes; failures stay inline."""
    straight_m = geo.haversine_m(origin["lat"], origin["lon"], dest["lat"], dest["lon"])
    cap_m = routing.ROUTE_MAX_STRAIGHT_LINE_M[mode]
    if straight_m > cap_m:
        origin_label = origin.get("name") or f"({origin['lat']}, {origin['lon']})"
        dest_label = dest.get("name") or f"({dest['lat']}, {dest['lon']})"
        return {
            "mode": mode,
            "error": "too_far",
            "detail": (
                f"{origin_label!r} and {dest_label!r} are {round(straight_m)} m "
                f"apart; compare_modes stays inside one city ({mode} cap {round(cap_m)} m)"
            ),
            "max_distance_m": cap_m,
        }
    result = route(
        origin["lat"], origin["lon"], dest["lat"], dest["lon"], mode=mode,
        include_elevation=include_elevation, confirm=confirm,
    )
    if "error" in result:
        row = {"mode": mode}
        row.update({k: v for k, v in result.items() if k not in _COMPARE_MODES_DROP})
        return row
    row = {
        "mode": mode,
        "distance_m": result.get("distance_m"),
        "duration_s": result.get("duration_s"),
    }
    duration_s = result.get("duration_s")
    if isinstance(duration_s, (int, float)) and not isinstance(duration_s, bool):
        row["duration_min"] = int(round(duration_s / 60))
    if "truncated" in result:
        row["truncated"] = result["truncated"]
    elevation_block = result.get("elevation") if include_elevation else None
    if isinstance(elevation_block, dict):
        for key in ("total_climb_m", "total_descent_m"):
            if key in elevation_block:
                row[key] = elevation_block[key]
    return row


def _compare_modes_summary(rows: list[dict], fastest: str | None) -> str:
    """Deterministic one-or-two-sentence read of the rows — no LLM, no randomness."""
    ok = [r for r in rows if "error" not in r]
    failed = [r for r in rows if "error" in r]
    verb = _COMPARE_MODES_VERB
    if not ok:
        reasons = "; ".join(f"{r['mode']} — {r['error']}" for r in failed)
        return f"No mode could be routed: {reasons}."
    if len(ok) == 1:
        (only,) = ok
        text = (
            f"Only {verb[only['mode']]} could be routed: {only['duration_min']} min "
            f"over {round(only['distance_m'])} m."
        )
    else:
        lead = next(r for r in ok if r["mode"] == fastest)
        others = [r for r in ok if r["mode"] != fastest]
        others_text = ", ".join(f"{verb[r['mode']]} {r['duration_min']} min" for r in others)
        text = (
            f"{verb[lead['mode']].capitalize()} is fastest at {lead['duration_min']} min; "
            f"{others_text}."
        )
    if any(r["mode"] == "drive" for r in ok):
        text += " Driving time is a posted-speed model with no live traffic."
    if failed:
        text += " Not routed: " + ", ".join(f"{r['mode']} ({r['error']})" for r in failed) + "."
    return text


@_tool("Compare walk / cycle / drive")
def compare_modes(
    from_: str | dict,
    to: str | dict,
    modes: list | None = None,
    include_elevation: bool = False,
    confirm: bool = False,
) -> dict:
    """Walk vs cycle vs drive between two places, in one call.

    "Should I walk, bike or drive from A to B?" is three route() calls;
    this is one. The ends resolve exactly once and every mode routes
    between the same two coordinates, so the rows are comparable. Use it
    when the user is choosing a mode; when the mode is already known,
    route() or from_to() gives the fuller answer (export maps/gpx/text,
    optional polyline).

    Pass each of from/to as a free-text place name, a {"lat", "lon"} dict,
    or a GERS id — mixed freely, exactly as from_to takes them.
    Do not call geocode(), resolve_place(), or geocode_batch() first. modes
    is an optional subset of ["walk", "cycle", "drive"]; omitted means all three
    in that order (stored mode preferences are not consulted — every mode
    asked for is computed). A mode outside that vocabulary, or an empty
    list, returns {"error": "bad_request"}; duplicates collapse, order kept.

    Returns {"from", "to", "modes", "fastest", "shortest", "summary"}.
    "from"/"to" carry what each end resolved to (name/id when the input was
    a name or GERS id, lat/lon always). "modes" is one compact row per
    requested mode, in the requested order: {"mode", "distance_m",
    "duration_s", "duration_min"} — no path, no export, no per-row
    endpoints; with include_elevation=true a row also carries
    "total_climb_m"/"total_descent_m" where the DEM covered the route
    (never faked as 0). "fastest"/"shortest" name the mode with the lowest
    duration/distance among the rows that routed, or null if none did.
    "summary" is a short deterministic sentence or two built from the rows.

    A mode that cannot be routed fails inline in its own row — {"mode",
    "error", "detail"} — and never aborts the call: "too_far" when the ends
    exceed that mode's straight-line cap (walk ~7.5 km, cycle ~23.5 km,
    drive ~95.5 km — so a cross-town pair often gives a drive row and a
    too_far walk row), "needs_confirm" with its eta/eta_s when that mode's
    street graph is cold and confirm is false, or whatever error route()
    returned (no_graph_nearby, no_route, ...). Only end resolution fails
    the whole call: {"error": "ambiguous_place" | "not_found" |
    "bad_request"} with "field": "from" | "to", as from_to does.

    Same cost model as route(): walk 1.4 m/s, cycle 4.2 m/s, drive from
    posted speed limits or a class default — a posted-speed model with no
    live traffic, which the summary says whenever drive routed.

    confirm=true after the user agreed to wait for a first-time street-graph
    build (about 5–25 seconds per cold mode — each mode is its own graph).
    Pass it only after a needs_confirm row and they said yes. A warm or
    cached graph never needs it. Omit confirm unless you just asked and
    they said yes.
    """
    if modes is None:
        wanted = list(_COMPARE_MODES_DEFAULT)
    else:
        if not isinstance(modes, list) or not modes:
            return {
                "error": "bad_request",
                "detail": "modes must be a non-empty list drawn from "
                f"{list(_COMPARE_MODES_DEFAULT)}, or omitted for all three",
                "supported": list(_COMPARE_MODES_DEFAULT),
            }
        wanted = []
        for mode in modes:
            if not isinstance(mode, str) or mode not in routing.MODE_CONFIG:
                return {
                    "error": "bad_request",
                    "detail": f"unsupported mode {mode!r} in modes; supported: "
                    f"{list(_COMPARE_MODES_DEFAULT)}",
                    "supported": list(_COMPARE_MODES_DEFAULT),
                }
            if mode not in wanted:
                wanted.append(mode)
    origin, dest, end_error = _resolve_route_ends(from_, to)
    if end_error is not None:
        return end_error
    rows = [
        _compare_modes_row(
            mode, origin, dest, include_elevation=include_elevation, confirm=confirm
        )
        for mode in wanted
    ]
    ok = [r for r in rows if "error" not in r]
    fastest = min(ok, key=lambda r: r["duration_s"])["mode"] if ok else None
    shortest = min(ok, key=lambda r: r["distance_m"])["mode"] if ok else None
    return {
        "from": origin,
        "to": dest,
        "modes": rows,
        "fastest": fastest,
        "shortest": shortest,
        "summary": _compare_modes_summary(rows, fastest),
    }


@_tool("Find places near a name")
def find_near(
    category: str,
    near: str,
    radius_m: float = 1000,
    limit: int = 10,
    cursor: _CursorArg = None,
) -> dict:
    """Places of a category near a named place or city.

    Prefer find_places(where=..., category=...): it is the canonical form of
    this search — the same one hop, plus every find_places filter, detail
    tier, and mode. find_near stays as a thin alias.

    Pass the user's place name as near. Do not call geocode(),
    resolve_place(), or geocode_batch() first. One hop for a category
    near a named landmark. Resolves near, then searches like a point
    find. Returns compact rows (name, category, distance, trust_note)
    plus the resolved near (name and coordinates).

    A comma qualifies: "Le Marais, Paris" searches inside Paris only.

    If near matches several equally-ranked places, returns
    {"error": "ambiguous_place", "candidates": [...]} instead of picking
    a city. An unresolvable name returns {"error": "not_found"}; empty
    category or near returns {"error": "bad_request"}. radius_m and
    limit follow the same clamps as a point search.

    A truncated answer carries "cursor" (delegated straight through from
    find_places); pass it back with the same category/near/radius_m/limit
    to continue. See find_places' docstring for the bad_cursor/release-
    mismatch details — they apply here unchanged.
    """
    if not isinstance(category, str) or not category.strip():
        return {"error": "bad_request", "detail": "category must be a non-empty slug or phrase"}
    if not isinstance(near, str) or not near.strip():
        return {"error": "bad_request", "detail": "near must be a non-empty place name"}
    if (
        not isinstance(radius_m, (int, float))
        or isinstance(radius_m, bool)
        or not math.isfinite(radius_m)
    ):
        return {"error": "bad_request", "detail": "radius_m must be a finite number"}
    if not isinstance(limit, (int, float)) or isinstance(limit, bool):
        return {"error": "bad_request", "detail": "limit must be an integer"}
    slug = _category_slug(category)
    pin = _resolve_named_place(near)
    if "error" in pin:
        return {**pin, "field": "near"}
    payload = find_places(
        lat=pin["lat"],
        lon=pin["lon"],
        radius_m=float(radius_m),
        category=slug,
        limit=int(limit),
        cursor=cursor,
        # find_near does its own projection to _FIND_NEAR_KEYS below (a
        # different, smaller shape than any find_places detail tier) — it
        # needs the full row to project from, not find_places' new
        # detail="compact" default (roadmap §4.5). find_near does not gain
        # its own `detail` param in this PR; see the PR body for that as
        # explicit follow-up scope.
        detail="full",
    )
    if "error" in payload:
        return payload
    rows = []
    for row in payload.get("results") or []:
        compact = {k: row[k] for k in _FIND_NEAR_KEYS if k in row}
        rows.append(compact)
    out: dict = {"near": pin, "category": slug, "results": rows}
    if slug != category.strip():
        out["category_resolved_from"] = category.strip()
    for key in ("truncated", "omitted_count", "note", "degraded_fields", "cursor"):
        if key in payload:
            out[key] = payload[key]
    pre_rows = len(out["results"])
    out = budget.apply_budget(out, "results")
    # find_places already computed `cursor`'s offset counting the rows it
    # delivered — but this second apply_budget pass (over the compacted
    # _FIND_NEAR_KEYS rows, which are smaller and shouldn't normally trim
    # further) can itself drop rows the caller never actually receives. If
    # it does, the cursor's offset would overcount and the next page would
    # silently skip those rows — rewind it by exactly how many were lost so
    # "never skip a row" holds regardless of how many trimming passes a
    # payload goes through (see cursor.rewind_cursor).
    dropped_here = pre_rows - len(out["results"])
    if dropped_here > 0 and out.get("cursor"):
        rewound = cursor_mod.rewind_cursor(out["cursor"], dropped_here)
        if rewound is not None:
            out["cursor"] = rewound
    return out


@_tool("Ground a location")
def ground_location(
    lat: float | None = None,
    lon: float | None = None,
    minutes: float = 15,
    mode: _ModeArgWalkDefault = None,
    where: dict | str | None = None,
) -> dict:
    """One-hop location grounding: where, surroundings, reach, notable.

    Answers "orient me at this point" in a single call instead of chaining
    a reverse lookup, an area summary, a reachable-area scan, and a
    nearby-places search. Give the point as lat/lon, or as `where` — a
    {"lat", "lon"} dict, a GERS id, or a free-text place name — but not
    both (and not neither); either way returns {"error": "bad_request"}
    naming the choice. A `where` given as an id/name adds a compact
    "resolved": {"name", "id", "lat", "lon", "matched_by"} to the answer
    (a separate key from the answer's own "where" section below); absent
    for lat/lon or a {lat,lon} where. Returns:
    - where: reverse_geocode's answer for the point (address/divisions
      chain, or a "divisions_only" degrade).
    - surroundings: total places and the top few categories within a fixed
      500m radius, plus density_per_km2.
    - reach: reachable-area stats only for (minutes, mode) —
      {reachable_nodes, max_radius_m, area_km2}. Never includes the
      reachable-area polygon; this tool returns no geometry, ever.
    - notable: the nearest 2-3 named places, no category filter.

    Each section is independent: if its underlying call fails or comes
    back empty, that section is dropped and a short line explaining why is
    added to "notes" instead — the call only fails outright if every
    section failed, returning a structured {"error":
    "upstream_unavailable", ...}.

    minutes must be > 0 and <= 60; omit mode to use the stored
    preferences mode, else walk.
    Both, plus out-of-range coordinates, return {"error": "bad_request"}.
    No confirm gate: the reach scan runs with the requested minutes/mode
    as-is (it self-caps its graph extraction radius; no nearby street
    graph just degrades the reach section to a note).
    """
    have_lat, have_lon = lat is not None, lon is not None
    if where is not None and (have_lat or have_lon):
        return {
            "error": "bad_request",
            "detail": "pass either lat and lon, or where — not both",
        }
    if where is None and not (have_lat and have_lon):
        return {
            "error": "bad_request",
            "detail": "ground_location needs lat and lon, or where",
        }
    resolved_echo = None
    if where is not None:
        item, ref_error = _resolve_location_ref(where)
        if ref_error is not None:
            return ref_error
        lat, lon = item["lat"], item["lon"]
        if "matched_by" in item:
            resolved_echo = _location_ref_echo(item)
    else:
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    if (
        not isinstance(minutes, (int, float))
        or isinstance(minutes, bool)
        or not math.isfinite(minutes)
        or minutes <= 0
        or minutes > 60
    ):
        return {
            "error": "bad_request",
            "detail": f"minutes={minutes!r} must be > 0 and <= 60",
        }
    mode = preference_store.resolve_mode(mode, preference_store.DEFAULT_MODE_ISOCHRONE)
    if not isinstance(mode, str) or mode not in routing.MODE_CONFIG:
        return {
            "error": "bad_request",
            "detail": f"mode={mode!r} is not supported; supported: {sorted(routing.MODE_CONFIG)}",
        }
    result = ground.ground_location(lat, lon, float(minutes), mode)
    if resolved_echo is not None and "error" not in result:
        result["resolved"] = resolved_echo
    return result


@_tool("Places along a route")
def places_along_route(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    mode: _ModeArgDriveDefault = None,
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
    underlying route. A composed itinerary also carries
    verify_before_going when any stop is low-confidence or listed closed,
    naming the 1–2 places most worth checking.

    category and name narrow the search exactly as they do in find_places
    (category matches Overture's taxonomy, e.g. 'coffee_shop'; name is a
    substring match) — worth passing on a long route, since an unfiltered
    corridor through a dense area can hold more places than the search
    considers, in which case the response carries "truncated": true and a
    note saying so.

    Omit mode to use the stored preferences mode, else drive. Same cost model
    and the same straight-line-distance caps as `route`, and the same
    structured errors: route_too_long, no_graph_nearby, no_route,
    unsupported_mode, and bad_request for non-finite/out-of-range
    coordinates or an invalid max_detour_m.
    """
    for lat, lon in ((from_lat, from_lon), (to_lat, to_lon)):
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    mode = preference_store.resolve_mode(mode, preference_store.DEFAULT_MODE_ROUTE)
    try:
        result = routing.places_along_route(
            from_lat, from_lon, to_lat, to_lon,
            mode=mode, category=category, name=name,
            max_detour_m=max_detour_m, limit=limit,
        )
    except routing.UnsupportedMode as e:
        return {
            "error": "unsupported_mode",
            "detail": e.detail,
            "supported": sorted(routing.MODE_CONFIG),
        }
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
    # Compute the verify line from pre-truncation rows. apply_budget may
    # strip confidence first, after which every survivor reads as low.
    # Attaching first also puts the line in the envelope so its tokens
    # are accounted for.
    honesty.attach_verify_line(result)
    payload = _with_degraded_fields(budget.apply_budget(result, "results"))
    return _with_category_hint(payload, category, widen_hint="widen max_detour_m")


@_tool("Neighborhood verdict")
def neighborhood_verdict(
    lat: float,
    lon: float,
    context: str = "",
    radius_m: float | None = None,
    minutes: float | None = None,
    mode: _ModeArgContextDefault = None,
) -> dict:
    """Life-decision neighborhood verdict, not a data dump.

    Accepts a point plus free-form life context (household, mobility,
    priorities) and returns a ranked verdict: strengths, weak points, and
    the one thing to verify in person. Empty context still answers a
    generic walk-first daily-needs check and says what was assumed.
    Optional radius_m / minutes / mode override what the context implies
    (no car / walk-first -> walk, bike -> cycle, car -> drive; default
    walk, 15 minutes). Does not call out to extra remote APIs; the
    "transit" need additionally reads base/infrastructure transit stops
    (bus_stop, subway_station, ferry_terminal, ...) alongside the
    places-theme categories, since places alone under-reports bus stops
    (#454).

    Returns a structured {"error": ...} for bad coordinates, an unknown
    mode, a radius past the mode cap, upstream failure, or a degraded
    schema. Missing street graph degrades to straight-line times with a
    note rather than failing the verdict.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        result = verdict.neighborhood_verdict(
            lat, lon, context=context or "",
            radius_m=radius_m, minutes=minutes, mode=mode,
        )
    except routing.UnsupportedMode as e:
        return {
            "error": "unsupported_mode",
            "detail": e.detail,
            "supported": sorted(routing.MODE_CONFIG),
        }
    except routing.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except (routing.SchemaDegraded, overture.SchemaDegraded) as e:
        return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}
    except routing.RadiusTooLarge as e:
        return {
            "error": "radius_too_large",
            "detail": e.detail,
            "max_radius_m": e.max_radius_m,
        }
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    return _with_degraded_fields(budget.apply_budget(result, "checklist"))


@_tool("Verify listing claims")
def verify_claims(lat: float, lon: float, claims: list[dict]) -> dict:
    """Grade spatial listing claims ("8 min to the metro", "shops on the doorstep",
    "green space nearby") against real routing and places data.

    Free-text claim parsing needs an LLM — this tool takes already-decomposed
    structured checks; the verify_listing_claims prompt teaches an agent how
    to turn listing text into them. Each of claims (max 8; max 5 of kind
    travel_time, since each costs a routed call) is one of:

    - {"kind": "travel_time", "to_category": str|None, "to_name": str|None,
      "mode": "walk"|"cycle"|"drive" (default walk), "claimed_minutes": number}
      Finds the nearest place matching to_category (an Overture taxonomy
      slug) and/or to_name (a substring match), then routes to it and
      compares the routed minutes against claimed_minutes.
    - {"kind": "count_nearby", "category": str|None, "name": str|None,
      "radius_m": number (default 500, capped at 2000), "claimed_at_least": int}
      Counts matching places within radius_m and compares against
      claimed_at_least.
    - {"kind": "distance", "to_category": str|None, "to_name": str|None,
      "claimed_max_m": number}
      Straight-line distance (haversine, not routed) to the nearest match,
      compared against claimed_max_m.

    Every kind needs at least one of its category/name fields; giving
    neither is a bad_request. A category is an Overture taxonomy slug,
    matched exactly (including its taxonomy descendants), never as a
    substring — "park" does not match a parking garage; a name is a
    substring match.

    Verdict per claim: "confirmed" when the measured number is within the
    claimed number x1.15 (count_nearby: measured count >= claimed),
    "stretched" within x1.5 (count_nearby: count >= half the claim, floor
    1), otherwise "false". A claim asserting a place exists at all, when
    none is found within the search bound, is "false" with a note —
    absence is a verdict, not an error. A claim the measurement cannot
    decide is "unverifiable" instead of "false": a travel_time claim whose
    place is found but cannot be routed to (no street graph nearby, or the
    network doesn't connect the two points), or whose failing measurement
    came from a size-cap-truncated street graph, and a count_nearby claim
    whose claimed_at_least exceeds the row cap the count stopped at.

    Returns {"results": [{"claim": <echo of the input>, "verdict":
    "confirmed"|"stretched"|"false"|"unverifiable", "measured": {...
    kind-appropriate minutes/count/distance_m, plus the matched place's id
    and name when there is one}, "note": optional}, ...], "verdict_rule":
    a one-line summary of the thresholds above}.

    Returns a structured {"error": "bad_request", ...} for anything
    malformed in claims (unknown/missing kind, more than 8 claims, more
    than 5 travel_time claims, a missing or non-numeric claimed value,
    neither target field given, or an unsupported mode), {"error":
    "bad_request", ...} for invalid lat/lon, or a structured {"error": ...}
    if the upstream dataset is unavailable or missing columns this tool
    depends on.
    """
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        return coord_error
    try:
        result = claim_checks.verify_claims(lat, lon, claims)
    except claim_checks.ClaimError as e:
        return {"error": "bad_request", "detail": str(e)}
    except routing.UnsupportedMode as e:
        return {
            "error": "unsupported_mode",
            "detail": e.detail,
            "supported": sorted(routing.MODE_CONFIG),
        }
    except routing.UpstreamUnavailable as e:
        return _upstream_error(e)
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except (routing.SchemaDegraded, overture.SchemaDegraded) as e:
        return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}
    except ValueError as e:
        return {"error": "bad_request", "detail": str(e)}
    return _with_degraded_fields(budget.apply_budget(result, "results"))


@_tool("Best visiting order for stops")
def optimize_route(
    stops: list[dict | str],
    mode: _ModeArgDriveDefault = None,
    roundtrip: bool = True,
    start_index: int = 0,
    keep_order: bool = False,
    confirm: bool = False,
) -> dict:
    """Best order to visit several stops: multi-stop route ordering (a small TSP).

    Answers "I have these five errands, what order costs least" — stops is a
    list of 2-10 points, each a {"lat": ..., "lon": ..., "name": ...
    (optional)} dict, a GERS id, or a free-text name, mixed freely. The
    answer is the cheapest visiting order over the real street graph, not a
    straight-line guess. Solved exactly (Held-Karp over the routed cost
    matrix), so it is the optimum, not a nearest-neighbour approximation.
    Any stop given by id/name that failed to resolve returns an indexed
    error (stops[i]: ...) with candidates on ambiguity — the whole call
    fails, not silently drops that stop.

    Returns {"order": [stop indices, in visiting order], "legs":
    [{"from_idx", "to_idx", "distance_m", "duration_s"}, ...],
    "total_distance_m", "total_duration_s", "mode", "roundtrip", "export"}
    — indices refer to the input `stops` list, and there is no
    polyline/geometry — for a single pair's numbers on their own, call
    `route`. export is the pocket handoff: a multi-stop Google/Apple Maps
    directions URL (coordinates only — no Maps API), a GPX 1.1 document
    with every stop as a waypoint, and a printable list that keeps any
    names the caller passed. If a stop already carries confidence or
    operating_status (from a prior place lookup), the response adds
    verify_before_going naming the 1–2 weakest. Any stop given as an id or
    name adds "resolved": [{"stop": i, "name", "id", "lat", "lon",
    "matched_by"}, ...] for just those stops — plain {lat,lon} stops need
    no echo and the key is absent when every stop was already coordinates.

    keep_order=true visits the stops in the order you gave and never
    reorders them: the itinerary is the caller's, and this tool supplies
    routed (not straight-line) per-leg numbers, the totals, and the export
    for it — one street-graph build for the whole run instead of chaining
    `route` per leg. Use it whenever the order came from the user ("first
    the bank, then the school, then home"); leave it off to be told the
    cheapest order. The response echoes "keep_order": true, and "order" is
    then just 0..n-1. start_index must stay 0 with it (there is nothing to
    fix — the given order already starts where it starts), and roundtrip
    still chooses whether the last leg closes back to the first stop, so a
    one-way itinerary wants roundtrip=false.

    start_index (default 0) is fixed as the first stop. roundtrip=true (the
    default) returns to it; the closing leg is in "legs" but the start is not
    repeated in "order". roundtrip=false is an open path that ends wherever
    is cheapest. Omit mode to use the stored preferences mode, else drive. Same
    cost model every routing tool uses; one-ways make the drive/cycle cost
    matrix asymmetric and that is solved for exactly. The objective minimized
    is total duration.

    If some pair of stops has no route between them (disconnected road data),
    the call still succeeds: that leg's numbers are a straight-line estimate,
    the leg carries "estimated": true, and the response carries
    "estimated": true plus a note naming the estimated legs — so a flagged
    approximation, never a crash.

    confirm=true after the user agreed to wait for a first-time street-graph
    build (about 5–25 seconds). Pass it only after a needs_confirm reply
    and they said yes. One gate for the whole call — every stop rides the
    same graph, so it asks once, never once per leg. A warm or cached graph
    never needs it. Omit confirm unless you just asked and they said yes.

    Errors are structured, not raised: fewer than 2 or more than 10 stops, a
    stop that is not a valid location reference, an out-of-range
    start_index, or keep_order=true with a non-zero start_index return
    {"error": "bad_request"} naming the offending stop
    index; an unresolvable name/id returns {"error": "not_found"} or
    {"error": "ambiguous_place", "candidates": [...]}, indexed the same way;
    an unknown mode returns {"error": "unsupported_mode"}; a stop set whose
    two furthest-apart stops are further apart than the mode's straight-line
    cap (see `route`) returns {"error": "route_too_long"}; a stop with no
    usable street node near it returns {"error": "no_graph_nearby"} naming
    that stop's index.
    """
    if not isinstance(stops, list):
        return {
            "error": "bad_request",
            "detail": "stops must be a list of {lat, lon} points, GERS ids, or place names",
        }
    if not (routing.OPTIMIZE_MIN_STOPS <= len(stops) <= routing.OPTIMIZE_MAX_STOPS):
        return {
            "error": "bad_request",
            "detail": (
                f"stops must hold between {routing.OPTIMIZE_MIN_STOPS} and "
                f"{routing.OPTIMIZE_MAX_STOPS} points, got {len(stops)}"
            ),
        }
    resolved_stops, resolve_error = _resolve_location_refs(stops, "stops")
    if resolve_error is not None:
        return resolve_error
    points = [(r["lat"], r["lon"]) for r in resolved_stops]
    resolved_echo = [
        {"stop": i, **_location_ref_echo(r)}
        for i, r in enumerate(resolved_stops)
        if "matched_by" in r
    ]
    if not isinstance(start_index, int) or isinstance(start_index, bool):
        return {"error": "bad_request", "detail": "start_index must be an integer"}
    if not 0 <= start_index < len(points):
        return {
            "error": "bad_request",
            "detail": f"start_index={start_index} is out of range for {len(points)} stops",
        }
    keep_order = bool(keep_order)
    if keep_order and start_index != 0:
        return {
            "error": "bad_request",
            "detail": (
                "keep_order=true visits the stops as given, so start_index must be 0, "
                f"got {start_index}"
            ),
        }

    mode = preference_store.resolve_mode(mode, preference_store.DEFAULT_MODE_ROUTE)
    # One gate for the whole call (#336's pattern, #423): every stop shares
    # a single extraction, so the cold-build question is asked once here —
    # before any graph is built — not once per leg.
    if not confirm and routing.stops_graph_needs_cold_build(points, mode):
        return _needs_confirm_graph(mode)

    try:
        result = routing.optimize_route(
            points,
            mode=mode,
            roundtrip=bool(roundtrip),
            start_index=start_index,
            keep_order=keep_order,
        )
    except routing.UnsupportedMode as e:
        return {
            "error": "unsupported_mode",
            "detail": e.detail,
            "supported": sorted(routing.MODE_CONFIG),
        }
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
    if "error" not in result:
        # Names never enter the TSP; they ride through here so the
        # printable list / GPX waypoints say "bakery" instead of "Stop 3".
        # A resolved id/name stop's canonical name is used when the caller
        # didn't also pass its own "name" override.
        named = []
        for point, resolved, raw in zip(points, resolved_stops, stops):
            row = {"lat": point[0], "lon": point[1]}
            name = raw.get("name") if isinstance(raw, dict) else resolved.get("name")
            if isinstance(name, str) and name.strip():
                row["name"] = name.strip()
            named.append(row)
        result["export"] = export.from_optimize_result(named, result)
        # Caller-supplied stop fields (name/confidence/operating_status) are
        # already in hand — no extra lookup. routing.py is untouched (#312).
        # verify_before_going skips bare {lat, lon} stops — those are not
        # place lookups and must not become "low confidence" warnings. A
        # string stop (id/name) is not a place-lookup Mapping either, so it
        # is swapped for {} here rather than handed to a `field in place`
        # check that expects a Mapping — index alignment is preserved.
        line = honesty.verify_before_going([s if isinstance(s, dict) else {} for s in stops])
        if line:
            result = {**result, "verify_before_going": line}
        if resolved_echo:
            result["resolved"] = resolved_echo
    return result


@_tool("Persistent preferences", annotations=_PREFERENCES_ANNOTATIONS)
def preferences(
    mode: _ModeSetArg = None,
    pace: str | None = None,
    household: list[str] | None = None,
    note: str | None = None,
    lang: _LangArg = None,
    clear: bool = False,
) -> dict:
    """Travel defaults.

    State "I bike everywhere, I have a dog" once. Routing tools use the
    stored mode when you omit theirs; an explicit argument always wins.
    pace and household are stored for later features and do not change
    answers yet. lang (#410) is the stored result-language preference: the
    name-lookup tools that accept their own `lang` use this one when
    theirs is omitted, returning an Overture-tagged name variant (e.g.
    "Munich" for "München" with lang="en") — a per-call `lang` always
    wins. The same document is the placeroot://preferences resource.

    Call with no arguments to read. Pass mode, pace, household tags, a
    free-text note, or lang to merge those fields.
    clear=true deletes the file and cannot be combined with other fields.
    Nothing is sent off this machine.
    """
    fields = (mode, pace, household, note, lang)
    if clear and any(value is not None for value in fields):
        return {
            "error": "bad_request",
            "detail": "clear=true cannot be combined with other fields",
        }
    try:
        if clear:
            return preference_store.clear()
        if any(value is not None for value in fields):
            if mode is not None and str(mode).strip().lower() not in preference_store.MODES:
                return {
                    "error": "bad_request",
                    "detail": f"mode={mode!r} is not supported",
                    "supported": sorted(preference_store.MODES),
                }
            if lang is not None and not preference_store.is_valid_lang(lang):
                return {
                    "error": "bad_request",
                    "detail": f"lang={lang!r} must be 2-3 lowercase letters",
                }
            return preference_store.update(
                mode=mode, pace=pace, household=household, note=note, lang=lang,
            )
        return preference_store.payload()
    except preference_store.PreferencesError as exc:
        return exc.as_dict()


def _confirm_graph_cap_s() -> float:
    """2x the advertised graph-build upper bound. Warm cache hits stay uncapped."""
    return 2.0 * float(progress.GRAPH_BUILD_S[1])


def _eta_exceeded_graph() -> dict:
    lo, hi = progress.GRAPH_BUILD_S
    return {
        "error": "eta_exceeded",
        "eta": progress.format_eta(lo, hi),
        "eta_s": [int(lo), int(hi)],
        "limit_s": int(_confirm_graph_cap_s()),
        "detail": (
            "The street-graph build exceeded twice the advertised wait "
            f"({progress.format_eta(lo, hi)}). Try a smaller area or a warm cache."
        ),
    }


def _run_route(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    *,
    mode: str,
    include_path: bool,
    include_elevation: bool = False,
    prefer: str | None = None,
    avoid: tuple[str, ...] = (),
    cap_confirm_build: bool,
) -> dict:
    """routing.route, with a 2x-ETA cap on a confirmed cold graph build."""
    if not cap_confirm_build:
        return routing.route(
            from_lat, from_lon, to_lat, to_lon, mode=mode, include_path=include_path,
            include_elevation=include_elevation, prefer=prefer, avoid=avoid,
        )
    limit_s = _confirm_graph_cap_s()
    # Install a log list before copy so worker report() appends are visible
    # to attach() on this thread (copy_context snapshots the list reference).
    progress._ensure_log()
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(
            ctx.run,
            routing.route,
            from_lat, from_lon, to_lat, to_lon,
            mode=mode, include_path=include_path,
            include_elevation=include_elevation, prefer=prefer, avoid=avoid,
        )
        try:
            return fut.result(timeout=limit_s)
        except TimeoutError:
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())
            return _eta_exceeded_graph()
    finally:
        # Do not join the worker — that would turn the cap back into a hang.
        pool.shutdown(wait=False, cancel_futures=True)


def _needs_confirm_graph(mode: str) -> dict:
    """Cheap reject before a cold street-graph extract (#336)."""
    lo, hi = progress.GRAPH_BUILD_S
    return {
        "error": "needs_confirm",
        "eta": progress.format_eta(lo, hi),
        "eta_s": [int(lo), int(hi)],
        "detail": (
            f"First {mode} in this city builds the street graph. "
            "Ask the user if they want to wait, then call the same tool "
            "again with confirm=true."
        ),
    }


def _needs_confirm_warmup() -> dict:
    lo, hi = progress.WARMUP_S
    return {
        "error": "needs_confirm",
        "eta": progress.format_eta(lo, hi),
        "eta_s": [int(lo), int(hi)],
        "detail": (
            "First warmup in this city copies map tiles into the local cache. "
            "Ask the user if they want to wait, then call the same tool "
            "again with confirm=true."
        ),
    }


def _warmup_is_cached(lat: float, lon: float, radius_m: float) -> bool:
    """True if warmup would not COPY — cache off, or both themes already on disk."""
    if not cache.enabled():
        return True
    radius_m = min(max(float(radius_m), 0.0), MAX_WARMUP_RADIUS_M)
    bbox = geo.bbox_around(lat, lon, radius_m)
    rel = release.resolve_release()
    for theme, type_ in _WARMUP_THEMES:
        glob = overture.upstream_glob(theme=theme, type_=type_)
        if not cache.bbox_is_cached(rel, theme, bbox, glob):
            return False
    return True


DEFAULT_WARMUP_RADIUS_M = 8000.0
MAX_WARMUP_RADIUS_M = 25_000.0

# Themes the first real question typically hits. places first ("what's
# around downtown"); transportation tiles next (the routing graph is
# still built on the first route). Buildings stay out: a metro bbox
# fans into too many 0.0625° tiles.
_WARMUP_THEMES: tuple[tuple[str, str], ...] = (
    ("places", "place"),
    ("transportation", "segment"),
)


def _prewarm_region(lat: float, lon: float, radius_m: float) -> dict:
    """Materialize existing-cache tiles for a metro bbox.

    Shared by warmup_city, _warm_start, and autowarm (first city-scale
    resolve). Same cache.py tiles — no second cache, no extra remote API.
    Tiles are not a built street graph.
    """
    radius_m = min(max(float(radius_m), 0.0), MAX_WARMUP_RADIUS_M)
    if not cache.enabled():
        return {
            "lat": lat,
            "lon": lon,
            "radius_m": radius_m,
            "status": "cache_disabled",
            "themes": [],
            "note": (
                "The tile cache is off (PLACEROOT_CACHE=off), so there is "
                "nothing to pre-warm. Repeat queries will still hit upstream."
            ),
        }
    bbox = geo.bbox_around(lat, lon, radius_m)
    themes = []
    # Do not hold conn_lock across the warmup. prewarm_bbox COPYs run
    # on new_connection() cursors (the same path background fetches
    # use), so other tools can keep answering between tiles/themes.
    # Holding the lock here was a server-wide stall at 25 km.
    for theme, type_ in _WARMUP_THEMES:
        with db.conn_lock:
            con = db.shared_conn()
        glob = overture.upstream_glob(theme=theme, type_=type_)
        themes.append(
            cache.prewarm_bbox(
                con,
                release.resolve_release(),
                theme,
                bbox,
                glob,
                db.new_connection,
            )
        )
    statuses = {row["status"] for row in themes}
    graph = progress.format_eta(*progress.GRAPH_BUILD_S)
    coverage = (
        "Places and transportation tiles are cached; buildings are not. "
        f"The first route still builds the street graph ({graph})."
    )
    if statuses <= {"already_warm"}:
        status = "already_warm"
        note = (
            "This area is already cached. Later place searches over it "
            f"should be fast. {coverage}"
        )
    elif "partial" in statuses and not (
        statuses & {"upstream_unavailable", "too_large"}
    ):
        status = "partial"
        note = (
            "Some tiles for this area are cached; a heavy theme stopped "
            "at the inline-tile cap so warmup would not monopolize the "
            f"server. {coverage}"
        )
    elif statuses <= {"warmed", "already_warm"}:
        status = "warmed"
        note = (
            "Places and transportation tiles for this area are cached. "
            "Place searches over this city should now be fast. "
            f"{coverage}"
        )
    elif "upstream_unavailable" in statuses and statuses & {
        "warmed",
        "already_warm",
        "partial",
    }:
        status = "partial"
        note = "Some themes cached; others could not reach upstream."
    elif "too_large" in statuses:
        status = "too_large"
        note = "The radius covers too many tiles; try a smaller radius_m."
    else:
        status = "failed"
        note = (
            "Warmup could not cache this area; the next query will scan upstream."
        )
    return {
        "lat": lat,
        "lon": lon,
        "radius_m": radius_m,
        "status": status,
        "themes": themes,
        "note": note,
    }


@_tool("Get to know my city")
def warmup_city(
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = DEFAULT_WARMUP_RADIUS_M,
    confirm: bool = False,
) -> dict:
    """Pre-cache a city.

    Copies places and transportation tiles into the same local cache later
    queries read. Does not build the routing graph (the first route still
    pays that cost) and does not pre-cache buildings. The warmup call is
    the slow one; later place searches over the area read locally.

    radius_m defaults to 8000 (a city core) and is capped at 25 km so a
    warmup cannot fan into a planet-sized tile fetch.

    confirm=true after the user agreed to wait for a first-time tile
    warmup (about 5–25 seconds). Pass it only after a needs_confirm reply
    and they said yes. An already-cached city never needs it.
    Omit confirm unless you just asked and they said yes.
    """
    point_given = lat is not None or lon is not None
    if point_given and city is not None:
        return {
            "error": "bad_request",
            "detail": "pass city, or lat and lon, not both",
        }
    if (
        not isinstance(radius_m, (int, float))
        or isinstance(radius_m, bool)
        or not math.isfinite(radius_m)
    ):
        return {"error": "bad_request", "detail": "radius_m must be a finite number"}
    resolved = None
    if city is not None:
        if not str(city).strip():
            return {"error": "bad_request", "detail": "city must be a non-empty name"}
        try:
            result = geocoding.geocode_detailed(city, limit=1)
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        rows = result["results"]
        if not rows:
            return {"error": "not_found", "detail": f"no place matched city {city!r}"}
        top = rows[0]
        lat = float(top["lat"])
        lon = float(top["lon"])
        resolved = {
            "name": top.get("name"),
            "id": top.get("id"),
            "lat": lat,
            "lon": lon,
        }
    elif lat is not None and lon is not None:
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return coord_error
    else:
        return {"error": "bad_request", "detail": "pass city, or both lat and lon"}
    if not confirm and not _warmup_is_cached(float(lat), float(lon), float(radius_m)):
        return _needs_confirm_warmup()
    if confirm:
        progress.report(
            "Caching map tiles for this city",
            eta_s=progress.WARMUP_S,
        )
    try:
        payload = _prewarm_region(float(lat), float(lon), float(radius_m))
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    except Exception as e:  # noqa: BLE001 - warmup must return structured errors
        return {"error": "upstream_unavailable", "detail": str(e), "retry_advised": True}
    if resolved is not None:
        payload["city"] = resolved
    return progress.attach(payload)


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

    Names only, no types: the catalog's budget is the whole point (46 tools
have to fit in about 1.1k tokens), and the names here are already
    self-describing (lat, radius_m, limit, category). A caller that guesses
    a type wrong gets placeroot_call's bad_request naming what the tool
    accepts, which is cheaper than paying for the types on every catalog
    read.
    """
    summary = ",".join(
        name if param.default is inspect.Parameter.empty else f"{name}?"
        for name, param in inspect.signature(fn).parameters.items()
    )
    # from is reserved in Python; from_to's public name is still from.
    # Alias only that exact token — a substring replace would turn
    # route / places_along_route from_lat into fromlat.
    return ",".join(
        {"from_": "from", "from_?": "from?"}.get(part, part) for part in summary.split(",")
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
    if (
        tool in ("from_to", "route", "compare_modes")
        and isinstance(call_args, dict)
        and "from" in call_args
    ):
        call_args = {**call_args, "from_": call_args["from"]}
        call_args.pop("from", None)
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
# spares a re-fetch of a ~33k-token schema surface on every session within a
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


async def _progress_middleware(ctx, call_next):
    """Narrate slow tool calls via MCP progress notifications.

    A cold query (first over a new area) legitimately spends tens of
    seconds in S3 scans and tile COPYs; without this, the client shows a
    silent spinner indistinguishable from a hang. When the caller attached
    a progressToken to its tools/call, this installs a request-scoped
    reporter (progress.set_reporter) that the query layer's phase
    boundaries feed — the start of a direct upstream scan, each tile COPY
    — and the client renders as live status. Every tools/call also starts
    a request-scoped log so attach() can put the same line on the JSON
    answer when the client never sent a token. Non-tool requests pass
    through untouched.

    The reporter is called from the worker thread the SDK runs a sync tool
    on, so the async send is scheduled onto the event loop with
    run_coroutine_threadsafe, fire-and-forget: progress must never block or
    fail the query it narrates (see progress.py's contract), and per the
    spec a progress send for a completed request is dropped harmlessly.
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)
    log_token = progress.begin()
    token = (ctx.meta or {}).get("progress_token")
    if token is None:
        try:
            result = await call_next(ctx)
            return progress.attach(result)
        finally:
            progress.reset_log(log_token)

    loop = asyncio.get_running_loop()
    session, request_id = ctx.session, ctx.request_id
    # The spec requires progress to increase with every notification on a
    # token. Call sites report per-phase counts that reset between phases
    # (tile 1..N for places, then 1..M for each base theme), so the wire
    # value is a per-request monotonic sequence instead; the human-facing
    # counts live in the message, which is what clients render anyway.
    seq = 0
    seq_lock = threading.Lock()

    def reporter(message: str, current: float | None, total: float | None) -> None:
        nonlocal seq
        # Scheduling happens under the same lock as the increment so the
        # wire order matches the sequence order even if two threads ever
        # report concurrently — a later value must not reach the loop first.
        with seq_lock:
            seq += 1
            future = asyncio.run_coroutine_threadsafe(
                session.send_progress_notification(
                    token, seq, None, message, related_request_id=request_id,
                ),
                loop,
            )
        # Consume the eventual result: a failed or cancelled send is already
        # best-effort (progress.py's contract) and must not surface as an
        # exception-was-never-retrieved warning at GC time.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())

    reset_token = progress.set_reporter(reporter)
    try:
        result = await call_next(ctx)
        return progress.attach(result)
    finally:
        progress.reset(reset_token)
        progress.reset_log(log_token)


async def _trace_middleware(ctx, call_next):
    """Record where a tool call spent its time, and let a slow one say so.

    Every latency investigation here has started with a user reporting "that
    took a minute" and ended with a number the server already knew while it
    was running — which phase, which scan, whether it was bounded. This
    middleware records that for every tools/call (trace.py), logs it under
    PLACEROOT_TRACE=1, and, when the call took longer than
    PLACEROOT_TRACE_SLOW_S, attaches the breakdown to the response as
    `timing` so the agent that waited gets the explanation with the answer.

    Attached only to dict responses and only when slow: a fast call's
    payload is unchanged, byte for byte, and a tool returning a list or a
    scalar is left alone rather than being reshaped to carry telemetry.
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)

    token = trace.start()
    started = time.perf_counter()
    try:
        result = await call_next(ctx)
    finally:
        elapsed = time.perf_counter() - started
        try:
            trace.log_summary(getattr(ctx, "tool_name", None) or "tools/call", elapsed)
        except Exception:  # noqa: BLE001 - telemetry must not fail the call
            logger.debug("trace summary failed", exc_info=True)

    threshold = trace.slow_threshold_s()
    if threshold and elapsed >= threshold and isinstance(result, dict) and "timing" not in result:
        rows = trace.summary()
        if rows:
            result["timing"] = {
                "total_s": round(elapsed, 1),
                "phases": rows[:8],
                "note": (
                    "This call was slow enough to explain itself. Scans marked "
                    "bounded:false read everything they touch."
                ),
            }
    trace.reset(token)
    return result


def _from_alias_base():
    """The ArgModelBase subclass that maps a published `from` back to `from_`.

    ArgModelBase.model_dump_one_level keys its kwargs by alias, which would
    call the tool with from=... — a syntax error waiting to happen. This
    keys them by field name instead, for every declared field, so a
    parameter can never be dropped from the dump by being forgotten in a
    hand-written dict (the #328/#395 bug class).

    Imported lazily: placeroot.server imports without mcp installed
    (test_import_hardening), and only server construction needs this.
    """
    from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase

    class _FromAliasArguments(ArgModelBase):
        def model_dump_one_level(self) -> dict:
            return {name: getattr(self, name) for name in type(self).model_fields}

    return _FromAliasArguments


def _from_to_arg_model():
    """from_to's published argument model: every parameter, `from_` as `from`."""
    from pydantic import ConfigDict, Field

    class FromToArguments(_from_alias_base()):
        model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)
        from_: str | dict = Field(alias="from")
        to: str | dict
        mode: _ModeArgWalkDefault = None
        include_path: bool = False
        include_elevation: bool = False
        prefer: _PreferArg = None
        avoid: _AvoidArg = None
        confirm: bool = False

    return FromToArguments


def _compare_modes_arg_model():
    """compare_modes' published argument model: `from_` as `from`, the rest verbatim (#459)."""
    from pydantic import ConfigDict, Field

    class CompareModesArguments(_from_alias_base()):
        model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)
        from_: str | dict = Field(alias="from")
        to: str | dict
        modes: _CompareModesArg = None
        include_elevation: bool = False
        confirm: bool = False

    return CompareModesArguments


def _route_arg_model():
    """route's published argument model: the four scalars, from/to, and the rest (#419)."""
    from pydantic import ConfigDict, Field

    class RouteArguments(_from_alias_base()):
        model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)
        from_lat: float | None = None
        from_lon: float | None = None
        to_lat: float | None = None
        to_lon: float | None = None
        mode: _ModeArgDriveDefault = None
        include_path: bool = False
        include_elevation: bool = False
        prefer: _PreferArg = None
        avoid: _AvoidArg = None
        confirm: bool = False
        from_: str | dict | None = Field(default=None, alias="from")
        to: str | dict | None = None

    return RouteArguments


def _publish_from_keyword(mcp_server) -> None:
    """Advertise from_to's, route's and compare_modes' origin as `from` — a reserved word in Python.

    The implementation parameter is from_ on all three tools. The public schema
    and the validator both use from so the agent never sees the underscore.

    Each model is checked against the function's real signature before it is
    published: a hand-written arg model that forgets a parameter silently
    deletes it from the published schema, which is exactly what #328/#395
    shipped and had to be fixed twice.

    Patches mcp 2.0.0 private internals (pinned in uv.lock). If those
    move, fail with a clear assertion rather than a raw AttributeError.
    """
    for name, fn, build in (
        ("from_to", from_to, _from_to_arg_model),
        ("route", route, _route_arg_model),
        ("compare_modes", compare_modes, _compare_modes_arg_model),
    ):
        try:
            tool = mcp_server._tool_manager.get_tool(name)
            if tool is None:
                continue
            model = build()
            missing = set(inspect.signature(fn).parameters) - set(model.model_fields)
            assert not missing, f"{name} schema patch drops {sorted(missing)}"
            tool.fn_metadata = tool.fn_metadata.model_copy(update={"arg_model": model})
            tool.parameters = model.model_json_schema(by_alias=True)
        except (AttributeError, ImportError) as e:
            raise AssertionError(f"{name} schema patch failed; mcp internals changed") from e


class _PermissiveOutput(BaseModel):
    """A pydantic model that accepts any dict, unchanged, as extra fields.

    `FuncMetadata.convert_result` (mcp/server/mcpserver/utilities/
    func_metadata.py:110-144) is the *real* runtime gate: once
    `fn_metadata.output_schema` is non-None it asserts `output_model is not
    None` and calls `output_model.model_validate(result)`, then ships
    `model_dump(mode="json", by_alias=True)` as `structuredContent`. A
    spec-compliant client requires exactly that — confirmed empirically:
    `mcp.client.session.ClientSession.validate_tool_result` (session.py:
    1080-1100) raises `RuntimeError` on any tool whose declared outputSchema
    has no matching `structured_content`. So structured output cannot be
    faked at the publication layer alone (see `_publish_output_schemas`);
    this model is what actually produces it, deliberately never rejecting a
    real answer: no declared fields, `extra="allow"`, so
    `model_validate(any_dict)` always succeeds and `model_dump` round-trips
    it byte-for-byte. Real validation happens client-side, against the
    precise schema `_publish_output_schemas` shadows onto `Tool.output_schema`
    below — decoupled on purpose, so the schema tools/list advertises can be
    richer than what this pass-through model would derive on its own.
    """

    model_config = ConfigDict(extra="allow")


def _publish_output_schemas(mcp_server) -> None:
    """Attach a declared `outputSchema` to every registered tool (roadmap §4.3 / §5.3).

    Every tool here returns a bare `dict` — the SDK's own schema derivation
    (func_metadata, driven by return-type annotations) gives nothing for
    that (a bare `dict` return type carries no field types to derive from),
    so the schemas in output_schemas.py are hand-written instead.

    Runtime-safety finding, in two parts:

    1. `Tool.output_schema` (mcp/server/mcpserver/tools/base.py:53-55) is a
       `functools.cached_property` that *defaults* to reading
       `self.fn_metadata.output_schema`, and tools/list publishes exactly
       that cached_property (mcp/server/mcpserver/server.py:490,
       `output_schema=info.output_schema`). `cached_property` stores its
       computed value in the instance's own `__dict__`; setting that key
       directly (confirmed empirically) permanently shadows the descriptor,
       so tools/list can advertise our own richer, hand-authored schema —
       decoupled from whatever `fn_metadata.output_schema` says.
    2. `FuncMetadata.convert_result` (utilities/func_metadata.py:110-144) —
       the actual runtime gate a `tools/call` goes through — reads a
       *different* attribute: `self.fn_metadata.output_schema`, the
       `FuncMetadata` field, not the `Tool` cached_property. Originally
       that field is None (bare-`dict` return, no `structured_output=`), so
       `convert_result` never touches `output_model` at all and every
       existing answer is untouched. Initially this function left that
       field alone entirely, on the theory that a schema which never
       drives validation can never break a call — but a real
       spec-compliant client rejects that: `mcp.client.session.
       ClientSession.validate_tool_result` (session.py:1080-1100) raises
       `RuntimeError` the moment a tool's declared outputSchema has no
       matching `structuredContent` on the response (confirmed by running
       tests/test_http.py's real `mcp.client.client.Client` against a
       tool with only the publication-layer patch applied — it failed).
       So `fn_metadata.output_schema`/`output_model` are patched too, via
       `_PermissiveOutput` (above) — a model that accepts and round-trips
       any dict, never rejecting a real answer regardless of which shape it
       takes. `wrap_output=False` because our tools already return a bare
       dict, not a primitive needing `{"result": ...}` wrapping.

    Net effect: every tools/call now also carries `structuredContent`
    (additive — `content`'s text block, computed from the same raw `result`
    before `output_model` ever sees it, is byte-identical to before), and
    what a client validates that structuredContent against is the precise,
    additionalProperties-true, drift-tolerant schema from output_schemas.py
    — honest enough by construction that every real answer satisfies it.

    Patches mcp 2.0.0 private internals (pinned in uv.lock). If those move —
    the cached_property's storage mechanism, or `convert_result`'s
    reliance on `fn_metadata.output_schema`/`output_model` — fail with a
    clear assertion rather than a raw AttributeError or a silently
    unpublished/unvalidated schema.
    """
    try:
        for tool in mcp_server._tool_manager.list_tools():
            schema = output_schemas.OUTPUT_SCHEMAS.get(tool.name)
            assert schema is not None, (
                f"{tool.name} has no declared outputSchema; add it to "
                "output_schemas.OUTPUT_SCHEMAS (FIRST_WAVE for a precise "
                "shape, or _GENERIC_TOOLS otherwise)"
            )
            tool.fn_metadata = tool.fn_metadata.model_copy(update={
                "output_schema": {"type": "object"},
                "output_model": _PermissiveOutput,
                "wrap_output": False,
            })
            tool.__dict__["output_schema"] = schema
            assert tool.output_schema is schema, "cached_property shadow did not take"
    except AttributeError as e:
        raise AssertionError("output schema publish failed; mcp internals changed") from e


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
    try:
        version = importlib.metadata.version("placeroot")
    except importlib.metadata.PackageNotFoundError:
        # Running from a source tree that was never installed (no dist-info);
        # an empty version is what clients saw before this was wired at all.
        version = ""
    server = MCPServer(
        "placeroot", version=version,
        instructions=BASE_INSTRUCTIONS, cache_hints=CACHE_HINTS,
        middleware=[_progress_middleware, _trace_middleware],
    )
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
    _publish_from_keyword(server)
    _publish_output_schemas(server)
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
    this — the call itself is synchronous (cache.prewarm_bbox force_sync),
    since this already only runs once, at startup, specifically to
    materialize the home region's tiles before real traffic arrives.
    """
    spec = os.environ.get("PLACEROOT_WARM_REGION")
    if not spec or not cache.enabled():
        return
    parsed = cache.parse_warm_region(spec)
    if parsed is None:
        logger.warning("PLACEROOT_WARM_REGION=%r is malformed, expected 'lat,lon,radius_m'", spec)
        return
    lat, lon, radius_m = parsed
    try:
        _prewarm_region(lat, lon, radius_m)
    except Exception as e:  # noqa: BLE001 - warm-on-start must never break startup
        logger.warning("PLACEROOT_WARM_REGION pre-warm failed (continuing): %s", e)


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


def _warm_home_async() -> None:
    """Kick off #406's home-region resolution (PLACEROOT_HOME today; MCP
    roots stubbed, see home_region.resolve_home_from_roots) on a daemon
    thread at startup, mirroring _warm_divisions_async.

    Resolving here — rather than waiting for the first geocode/resolve_place
    call to do it lazily — both warms geocode.py's ranking bias ahead of
    real traffic and, when it resolves, schedules the same background tile
    warm a city-scale resolve gets (autowarm.py). Never blocks startup;
    schedule_autowarm itself already no-ops when PLACEROOT_CACHE=off, so no
    extra gating is needed here.
    """
    threading.Thread(target=home_region.kick_home_autowarm, daemon=True).start()


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
    _warm_home_async()
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
