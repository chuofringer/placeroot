"""Published tool schemas: the argument aliases every handler annotates with, the
from-keyword argument models for from_to, route and compare_modes, and the two
mcp SDK patches build_server() applies (the from keyword and the declared outputSchema).

Moved verbatim out of server.py (no behaviour change). The SDK patches reach the tool
handlers through the placeroot.server module at call time, as before. server.py re-exports
every name.
"""

import inspect
import sys
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from placeroot import (
    output_schemas,
    overture,
    routing,
)
from placeroot import (
    preferences as preference_store,
)

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


def _server():
    """The placeroot.server module, looked up at call time.

    The handlers this module publishes are defined in placeroot.server, which
    imports this module, so they are reached through sys.modules at call time.
    """
    return sys.modules["placeroot.server"]


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
        ("from_to", _server().from_to, _from_to_arg_model),
        ("route", _server().route, _route_arg_model),
        ("compare_modes", _server().compare_modes, _compare_modes_arg_model),
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
