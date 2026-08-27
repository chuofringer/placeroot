"""Hand-authored MCP `outputSchema` declarations (roadmap §4 feature 3 / §5.3).

Every placeroot tool returns a bare `dict` — a heterogeneous success shape
OR a `{"error", "detail", ...}` envelope — so the SDK's own return-type
schema derivation (`func_metadata`, driven by return annotations) produces
nothing for any of them: a bare `dict` return type carries no field types
to derive from. These are hand-written instead and attached to each tool
after registration by `_publish_output_schemas` in server.py.

Runtime safety has two parts, both empirical (see server.py's
`_publish_output_schemas` docstring for the file:line evidence):

1. A spec-compliant client (confirmed against the SDK's own ClientSession)
   REQUIRES structuredContent whenever a tool declares an outputSchema, and
   validates it against exactly that schema — so these schemas must be
   honest enough that every real answer (success, error envelope, degraded
   variant) actually validates. `additionalProperties: true` everywhere and
   `required` limited to always-present keys is what makes that hold by
   construction rather than by exhaustively modeling every field.
2. Older protocol revisions restrict `outputSchema` to `type: "object"` at
   the root: mcp_types' base `Tool.output_schema` docstring
   (`_types.py:1424-1430`) says so "through 2025-11-25," loosening to "any
   valid JSON Schema 2020-12" only on 2026-07-28 (`_v2026_07_28/
   __init__.py:1074-1084`'s `OutputSchema` — `extra="allow"`, no type
   restriction at all). This server negotiates 2026-07-28
   (test_caching.py pins `LATEST_PROTOCOL_VERSION`), so the restriction no
   longer binds it — but keeping `"type": "object"` alongside `"anyOf"`
   costs nothing (JSON Schema evaluates both: the instance must be an
   object AND match one arm) and keeps every schema here valid under the
   older, stricter revision too, for any client that still enforces it.

Pattern per tool: `_anyof(<success shape>)`, i.e. `{"type": "object",
"anyOf": [<success shape>, ERROR_SCHEMA]}`. ERROR_SCHEMA is one shared
envelope (object, required ["error"], additionalProperties true — errors
carry try/supported/candidates/index/field/eta/... beyond the two
documented keys). Every success shape is also `additionalProperties: true`
(drift-tolerant — a field this schema doesn't yet know about is not a
validation failure) with `required` limited to keys that are always present
on success. No `description`s anywhere: the tool docstrings already explain
semantics, so the schema carries shape/typing only — that's what keeps the
token cost down.
"""

ERROR_SCHEMA: dict = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {"type": "string"},
        "detail": {"type": "string"},
    },
    "additionalProperties": True,
}


def _anyof(success: dict) -> dict:
    return {"type": "object", "anyOf": [success, ERROR_SCHEMA]}


# A resolved LocationRef echo — {"name"?, "id"?, "lat", "lon", "matched_by"} —
# attached whenever an id/name argument resolved rather than arriving as
# bare coordinates. Referenced by, not required within, several schemas.
_RESOLVED_ECHO = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "id": {"type": "string"},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
        "matched_by": {"type": "string"},
    },
    "additionalProperties": True,
}

# One find_places/find_near row, any detail tier (compact/ids/full) — the
# tiers are a strict field subset of each other (server.py's
# _project_place_row), so one drift-tolerant shape covers every tier rather
# than three separate schemas. "id" is the only key present at every tier.
_PLACE_ROW = {
    "type": "object",
    "required": ["id"],
    "properties": {
        "id": {"type": ["string", "null"]},
        "name": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"]},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
        "distance_m": {"type": "number"},
        "trust": {"type": "string"},
        "trust_note": {"type": "string"},
        "operating_status": {"type": ["string", "null"]},
        "confidence": {"type": ["number", "null"]},
        "brand": {"type": ["string", "null"]},
        "has_website": {"type": "boolean"},
        "has_phone": {"type": "boolean"},
        "matched_by": {"type": "string"},
    },
    "additionalProperties": True,
}

_EXPORT = {
    "type": "object",
    "properties": {
        "maps_link": {
            "type": "object",
            "properties": {
                "google": {"type": "string"},
                "apple": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "gpx": {"type": "string"},
        "text": {"type": "string"},
    },
    "additionalProperties": True,
}

# ---------------------------------------------------------------------------
# First wave: precise properties (roadmap §5.3).
# ---------------------------------------------------------------------------

FIND_PLACES_SCHEMA = _anyof({
    "type": "object",
    "required": ["results"],
    "properties": {
        # group_by_category=True answers {category: [rows...]} instead of
        # a flat list — both shapes are real, so "results" is a union type.
        "results": {"type": ["array", "object"], "items": _PLACE_ROW},
        "area": {"type": "object"},
        "truncated": {"type": "boolean"},
        "omitted_count": {"type": "integer"},
        "cursor": {"type": "string"},
        "note": {"type": "string"},
        "degraded_fields": {"type": "array"},
        "trust_legend": {"type": "string"},
        "category_resolved_from": {"type": "string"},
        "resolved": {"type": "object"},
    },
    "additionalProperties": True,
})

FIND_NEAR_SCHEMA = _anyof({
    "type": "object",
    "required": ["near", "category", "results"],
    "properties": {
        "near": {"type": "object"},
        "category": {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "name": {"type": ["string", "null"]},
                    "category": {"type": ["string", "null"]},
                    "distance_m": {"type": "number"},
                    "lat": {"type": "number"},
                    "lon": {"type": "number"},
                    "trust_note": {"type": "string"},
                    "operating_status": {"type": ["string", "null"]},
                },
                "additionalProperties": True,
            },
        },
        "category_resolved_from": {"type": "string"},
        "truncated": {"type": "boolean"},
        "omitted_count": {"type": "integer"},
        "note": {"type": "string"},
        "degraded_fields": {"type": "array"},
        "cursor": {"type": "string"},
    },
    "additionalProperties": True,
})

_GEOCODE_ROW = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
        "id": {"type": ["string", "null"]},
        "admin_context": {"type": "array", "items": {"type": "string"}},
        "rank_score": {"type": "number"},
        "matched_by": {"type": "string"},
        "matched_name": {"type": "string"},
        "country": {"type": "string"},
        "address_count": {"type": "integer"},
    },
    "additionalProperties": True,
}

GEOCODE_SCHEMA = _anyof({
    "type": "object",
    "required": ["results"],
    "properties": {
        "results": {"type": "array", "items": _GEOCODE_ROW},
        "note": {"type": "string"},
        "truncated": {"type": "boolean"},
        "omitted_count": {"type": "integer"},
    },
    "additionalProperties": True,
})

GEOCODE_BATCH_SCHEMA = _anyof({
    "type": "object",
    "required": ["results"],
    "properties": {
        # One row per input query, in order — either a geocode match row
        # or {"query", "error": "not_found", "detail"} for a miss; a
        # per-row miss does not fail the batch, so it lives inside this
        # success envelope rather than the top-level error arm.
        "results": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "truncated": {"type": "boolean"},
        "omitted_count": {"type": "integer"},
    },
    "additionalProperties": True,
})

RESOLVE_PLACE_SCHEMA = _anyof({
    "type": "object",
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "kind", "name"],
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "kind": {"type": "string", "enum": ["division", "place"]},
                    "name": {"type": "string"},
                    "lat": {"type": "number"},
                    "lon": {"type": "number"},
                    "match": {"type": "string"},
                    "admin_context": {"type": "array", "items": {"type": "string"}},
                    "category": {"type": "string"},
                    "matched_by": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "need": {"type": "string"},
        "retry_with": {"type": "object"},
        "note": {"type": "string"},
        "truncated": {"type": "boolean"},
        "omitted_count": {"type": "integer"},
    },
    "additionalProperties": True,
})

# route() and from_to() share one success shape: from_to wraps route() and
# only adds name/id/type/admin_context onto the same "from"/"to" points.
_ROUTE_SUCCESS = {
    "type": "object",
    "required": ["distance_m", "duration_s", "mode"],
    "properties": {
        "distance_m": {"type": "number"},
        "duration_s": {"type": "number"},
        "mode": {"type": "string"},
        "from": {"type": "object"},
        "to": {"type": "object"},
        "export": _EXPORT,
        "path": {"type": "object"},
        "path_max_deviation_m": {"type": "number"},
        "path_omitted": {"type": "boolean"},
        "elevation": {"type": "object"},
        "elevation_omitted": {"type": "boolean"},
        "prefer_note": {"type": "string"},
        "avoid": {"type": "array", "items": {"type": "string"}},
        "avoid_note": {"type": "string"},
        "truncated": {"type": "boolean"},
        "resolved": {"type": "object"},
    },
    "additionalProperties": True,
}

ROUTE_SCHEMA = _anyof(_ROUTE_SUCCESS)
FROM_TO_SCHEMA = _anyof(_ROUTE_SUCCESS)

ISOCHRONE_SCHEMA = _anyof({
    "type": "object",
    "required": ["polygon", "stats"],
    "properties": {
        "polygon": {"type": "object"},
        "stats": {
            "type": "object",
            "properties": {
                "reachable_nodes": {"type": "integer"},
                "max_radius_m": {"type": "number"},
                "area_km2": {"type": "number"},
            },
            "additionalProperties": True,
        },
        "resolved": {"type": "object"},
        "truncated": {"type": "boolean"},
    },
    "additionalProperties": True,
})

TRAVEL_TIME_MATRIX_SCHEMA = _anyof({
    "type": "object",
    "required": ["elements"],
    "properties": {
        "mode": {"type": "string"},
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["origin_idx", "dest_idx"],
                "properties": {
                    "origin_idx": {"type": "integer"},
                    "dest_idx": {"type": "integer"},
                    "duration_min": {"type": ["number", "null"]},
                    "distance_m": {"type": ["number", "null"]},
                    "note": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "durations_note": {"type": "string"},
        "note": {"type": "string"},
        "truncated": {"type": "boolean"},
        "resolved": {"type": "object"},
    },
    "additionalProperties": True,
})

DISTANCE_MATRIX_SCHEMA = _anyof({
    "type": "object",
    "required": ["elements"],
    "properties": {
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["origin_idx", "dest_idx", "distance_m"],
                "properties": {
                    "origin_idx": {"type": "integer"},
                    "dest_idx": {"type": "integer"},
                    "distance_m": {"type": "number"},
                },
                "additionalProperties": True,
            },
        },
        "resolved": {"type": "object"},
        "truncated": {"type": "boolean"},
    },
    "additionalProperties": True,
})

OPTIMIZE_ROUTE_SCHEMA = _anyof({
    "type": "object",
    "required": [
        "order", "legs", "total_distance_m", "total_duration_s", "mode", "roundtrip",
    ],
    "properties": {
        "order": {"type": "array", "items": {"type": "integer"}},
        "legs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from_idx": {"type": "integer"},
                    "to_idx": {"type": "integer"},
                    "distance_m": {"type": "number"},
                    "duration_s": {"type": "number"},
                    "estimated": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
        "total_distance_m": {"type": "number"},
        "total_duration_s": {"type": "number"},
        "mode": {"type": "string"},
        "roundtrip": {"type": "boolean"},
        "export": _EXPORT,
        "estimated": {"type": "boolean"},
        "note": {"type": "string"},
        "verify_before_going": {"type": "string"},
        "resolved": {"type": "array", "items": _RESOLVED_ECHO},
    },
    "additionalProperties": True,
})

DATA_VERSION_SCHEMA = _anyof({
    "type": "object",
    "required": ["release", "release_date", "source"],
    "properties": {
        "release": {"type": "string"},
        "release_date": {"type": "string"},
        "source": {"type": "string"},
        "age_days": {"type": "number"},
        "note": {"type": "string"},
        "artifacts": {"type": "string"},
        "recreation_layer": {"type": "object"},
    },
    "additionalProperties": True,
})

# ---------------------------------------------------------------------------
# Every remaining tool: the generic honest envelope — cheap (~20 tokens),
# truthful (still separates "an object came back" from "the error envelope
# came back"), and upgradeable to a precise shape later without touching
# server.py's publish step.
# ---------------------------------------------------------------------------

GENERIC_SCHEMA = _anyof({"type": "object", "additionalProperties": True})

FIRST_WAVE: dict[str, dict] = {
    "find_places": FIND_PLACES_SCHEMA,
    "find_near": FIND_NEAR_SCHEMA,
    "geocode": GEOCODE_SCHEMA,
    "geocode_batch": GEOCODE_BATCH_SCHEMA,
    "resolve_place": RESOLVE_PLACE_SCHEMA,
    "from_to": FROM_TO_SCHEMA,
    "route": ROUTE_SCHEMA,
    "isochrone": ISOCHRONE_SCHEMA,
    "travel_time_matrix": TRAVEL_TIME_MATRIX_SCHEMA,
    "distance_matrix": DISTANCE_MATRIX_SCHEMA,
    "optimize_route": OPTIMIZE_ROUTE_SCHEMA,
    "data_version": DATA_VERSION_SCHEMA,
}

# Every other registered tool (server.py's _TOOL_FUNCS/_META_TOOL_FUNCS, minus
# FIRST_WAVE above) — listed explicitly, not derived from the registry, so a
# newly added tool fails test_output_schemas.py's coverage check instead of
# silently inheriting a schema by accident.
_GENERIC_TOOLS: list[str] = [
    "address_at",
    "admin_lookup",
    "buildings_at",
    "changes_in_area",
    "compare_areas",
    "elevation_at",
    "geocode_address",
    "geometry_op",
    "gers_lookup",
    "ground_location",
    "infrastructure_at",
    "land_use_at",
    "meeting_point",
    "neighborhood_verdict",
    "place_details",
    "places_along_route",
    "preferences",
    "render_map",
    "resolve_place_batch",
    "reverse_geocode",
    "reverse_geocode_batch",
    "search_categories",
    "simplify_geometry",
    "suggest_areas",
    "summarize_area",
    "summarize_buildings",
    "verify_claims",
    "warmup_city",
    "water_near",
    "within_distance",
    # Meta-tools (progressive-mode surface, tool_profiles.PROGRESSIVE_TOOLS):
    # same bare-dict-with-error-envelope shape as every real tool.
    "placeroot_call",
    "placeroot_capabilities",
]

OUTPUT_SCHEMAS: dict[str, dict] = {
    **FIRST_WAVE,
    **{name: GENERIC_SCHEMA for name in _GENERIC_TOOLS},
}
