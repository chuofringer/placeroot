"""PLACEROOT_TOOLS=progressive: the 3-tool meta surface (issue #210).

The feature's claim is a standing cost small enough to leave a PlaceRoot
install registered alongside a dozen other MCP servers, with nothing lost —
so the assertions here are the two halves of that claim: what `tools/list`
actually costs under `progressive`, and that dispatching through
placeroot_call gives back exactly what calling the tool gives back.

Like test_tool_profiles.py, every registration assertion goes through the
MCP server's own registry rather than the internal selection set.
"""

import asyncio
import json

import pytest
from pydantic import ValidationError

from placeroot import budget, prompts, server, tool_profiles
from tests._routing_fixture import build_routing_fixture as fx

from .conftest import CENTER_LAT, CENTER_LON

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)

# The whole point of the mode: what a client pays to have PlaceRoot loaded.
# The full surface is ~12.7k estimated tokens; the target from issue #210 is
# under 1.5k, and the meta surface currently measures ~580.
STANDING_COST_CEILING = 1500

# placeroot_capabilities' answer is read once per conversation that uses it,
# so it is budgeted like a response, not like documentation. A new tool
# that doesn't fit under this belongs in a shorter one-liner, not a raised
# ceiling. 42 tools (suggest_areas joining the 2026-08 train's 41)
# measure 1412 — each entry is already a one-liner, so the growth here
# is genuinely tool count, not verbosity.
CATALOG_CEILING = 1475

# The subset note's opening words, rendered by the renderer itself rather than
# retyped here. A phrase typed from memory can drift from what prompts.py
# actually writes, and an absence assertion that matches text no prompt ever
# renders passes for the wrong reason — so the marker is generated, and
# test_the_subset_note_is_detectable_when_it_is_there proves it still matches.
_NOTE_SENTINEL = "sentinel_tool_name"
SUBSET_NOTE_MARKER = prompts._profile_note([_NOTE_SENTINEL], set()).split(_NOTE_SENTINEL)[0].strip()


def _tools(spec):
    return asyncio.run(server.build_server(spec).list_tools())


def _rendered_prompts(spec) -> dict[str, str]:
    """Every prompt of a `spec` server, rendered, keyed by name."""
    built = server.build_server(spec)
    rendered = {}
    for prompt in asyncio.run(built.list_prompts()):
        args = {a.name: "x" for a in (prompt.arguments or [])}
        result = asyncio.run(built.get_prompt(prompt.name, args))
        rendered[prompt.name] = " ".join(str(m.content) for m in result.messages)
    return rendered


def _names(spec) -> set[str]:
    return {t.name for t in _tools(spec)}


def _schema_tokens(spec) -> int:
    """Estimated tokens of `tools/list`, serialized the way the SDK sends it."""
    payload = [t.model_dump(mode="json", exclude_none=True) for t in _tools(spec)]
    return budget.estimate_tokens(payload)


# --- Registration shape -----------------------------------------------------


def test_progressive_registers_exactly_the_three_meta_tools():
    assert _names("progressive") == {
        "placeroot_capabilities",
        "placeroot_call",
        "data_version",
        "preferences",
    }


def test_progressive_is_case_and_whitespace_insensitive():
    assert _names("  PROGRESSIVE ") == _names("progressive")


def test_env_var_selects_progressive(monkeypatch):
    monkeypatch.setenv("PLACEROOT_TOOLS", "progressive")
    assert {t.name for t in asyncio.run(server.build_server().list_tools())} == _names(
        "progressive"
    )


def test_meta_tools_are_absent_from_every_other_selection():
    """They are a way to reach the surface, not part of it."""
    for spec in [None, "all", "core", "search", "routing", "analysis", "geometry"]:
        assert not (_names(spec) & tool_profiles.PROGRESSIVE_TOOLS), spec


def test_meta_tools_cannot_be_selected_by_name():
    """Without its catalog the dispatcher is unusable, so neither is offered
    as a standalone name — asking for one is an unknown-name failure.
    """
    for name in sorted(tool_profiles.PROGRESSIVE_TOOLS):
        with pytest.raises(tool_profiles.InvalidToolSelection):
            server.build_server(name)


def test_prompts_and_resources_are_still_registered():
    built = server.build_server("progressive")
    assert asyncio.run(built.list_prompts())
    assert asyncio.run(built.list_resources())


def test_prompts_do_not_disown_tools_progressive_can_still_reach():
    """Every tool is reachable via placeroot_call, so no prompt may carry the
    subset note naming tools this selection "left out" (#194 vs #210).

    This is what build_server's `reachable` line buys: hand prompts.register
    the three meta names instead of the full surface and all three prompts
    render the note, disowning the tools their own steps depend on.
    """
    rendered = _rendered_prompts("progressive")
    assert rendered
    for name, text in rendered.items():
        assert SUBSET_NOTE_MARKER not in text, name


def test_the_subset_note_is_detectable_when_it_is_there():
    """Positive control for the assertion above.

    Under a genuine subset every one of these prompts names a tool the
    selection dropped, so the marker must appear — otherwise the absence
    assertion is matching text no prompt ever renders and cannot fail.
    """
    rendered = _rendered_prompts("geometry")
    assert rendered
    for name, text in rendered.items():
        assert SUBSET_NOTE_MARKER in text, name


def test_build_server_logs_the_progressive_selection(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="placeroot.server"):
        server.build_server("progressive")
    assert "registered 4 of" in caplog.text
    assert "PLACEROOT_TOOLS=progressive" in caplog.text


# --- Standing cost ----------------------------------------------------------


def test_progressive_schema_surface_is_under_the_ceiling():
    tokens = _schema_tokens("progressive")
    assert tokens < STANDING_COST_CEILING, (
        f"progressive tools/list costs {tokens} estimated tokens, over the "
        f"{STANDING_COST_CEILING} the mode exists to stay under"
    )


def test_progressive_costs_a_fraction_of_the_full_surface():
    assert _schema_tokens("progressive") < _schema_tokens(None) // 5


def test_the_catalog_answer_fits_its_budget():
    tokens = budget.estimate_tokens(server.placeroot_capabilities())
    assert tokens < CATALOG_CEILING, f"catalog costs {tokens} estimated tokens"


# --- Catalog completeness ---------------------------------------------------


def test_catalog_names_are_exactly_the_full_registry():
    """The guard that keeps a new tool from silently missing the catalog."""
    catalog = server.placeroot_capabilities()
    listed = {entry.split("(", 1)[0] for entry in catalog["tools"]}
    assert listed == set(server._TOOL_FUNCS)
    assert catalog["count"] == len(server._TOOL_FUNCS)
    full_surface = {t.name for t in _tools(None)}
    assert listed == full_surface


def test_every_catalog_entry_carries_a_summary_and_an_arg_list():
    for entry in server.placeroot_capabilities()["tools"]:
        name, _, rest = entry.partition("(")
        args, _, summary = rest.partition(")")
        assert name in server._TOOL_FUNCS
        assert summary.strip(), f"{name} has no summary"
        assert args == server._arg_summary(server._TOOL_FUNCS[name])


def test_catalog_summaries_match_the_tools_own_descriptions():
    """One source of truth: the docstring the full surface advertises."""
    descriptions = {
        t.name: " ".join((t.description or "").split()) for t in _tools(None)
    }
    for entry in server.placeroot_capabilities()["tools"]:
        name, _, rest = entry.partition("(")
        summary = rest.partition(")")[2].strip()
        assert descriptions[name].startswith(summary), name


def test_required_and_optional_args_are_distinguished():
    assert server._arg_summary(server._TOOL_FUNCS["geocode"]) == "query,limit?,lang?"
    assert server._arg_summary(server._TOOL_FUNCS["data_version"]) == ""


def test_route_advertised_params_keep_from_lat():
    """from_ alias is the exact token only; route still advertises from_lat."""
    advertised = server._arg_summary(server._TOOL_FUNCS["route"])
    tokens = advertised.split(",")
    # Optional since #419: either the four scalars or from/to, never both.
    assert "from_lat?" in tokens
    assert "from?" in tokens
    assert "fromlat" not in advertised
    catalog = next(e for e in server.placeroot_capabilities()["tools"] if e.startswith("route("))
    assert "from_lat" in catalog
    assert "fromlat" not in catalog
    along = server._arg_summary(server._TOOL_FUNCS["places_along_route"])
    assert "from_lat" in along.split(",")
    assert server._arg_summary(server._TOOL_FUNCS["from_to"]).split(",")[0] == "from"


# --- Dispatch ---------------------------------------------------------------


# Argument values are written at the tool's own declared types (radius_m is a
# float, not an int): dispatch validates through the tool's pydantic model, so
# an int handed to a float parameter comes back normalized to 1000.0 — exactly
# as it would over tools/call — and a mistyped literal here would be measuring
# that normalization rather than the round trip.
ROUND_TRIPS = [
    ("find_places", {"lat": CENTER_LAT, "lon": CENTER_LON, "radius_m": 1000.0, "limit": 5}),
    ("geocode", {"query": "Brooklyn", "limit": 5}),
    (
        "route",
        {
            "from_lat": FROM_LAT,
            "from_lon": FROM_LON,
            "to_lat": TO_LAT,
            "to_lon": TO_LON,
            "mode": "walk",
        },
    ),
    ("summarize_area", {"lat": CENTER_LAT, "lon": CENTER_LON, "radius_m": 1000.0}),
    ("data_version", {}),
]


@pytest.mark.parametrize("tool,args", ROUND_TRIPS, ids=[t for t, _ in ROUND_TRIPS])
def test_dispatch_round_trips_byte_identically(tool, args):
    """Dispatch is the same function, not a copy of it, so the JSON matches."""
    direct = getattr(server, tool)(**args)
    through = server.placeroot_call(tool, args)
    assert json.dumps(through, default=str, sort_keys=True) == json.dumps(
        direct, default=str, sort_keys=True
    )


def test_dispatch_reaches_every_tool_the_full_surface_registers():
    """No parallel copies: the dispatcher's registry is the one build_server
    registers from, so every name on the full surface resolves — checked by
    dispatching each with a deliberately wrong argument and requiring the
    argument error rather than unknown_tool.
    """
    for name in {t.name for t in _tools(None)}:
        result = server.placeroot_call(name, {"definitely_not_an_argument": 1})
        assert result["error"] == "bad_request", name


def test_the_meta_registry_is_disjoint_from_the_tool_registry():
    assert not set(server._META_TOOL_FUNCS) & set(server._TOOL_FUNCS)
    assert set(server._META_TOOL_FUNCS) == set(tool_profiles.PROGRESSIVE_TOOLS)


def test_args_may_be_omitted_for_a_no_argument_tool():
    assert server.placeroot_call("data_version") == server.data_version()


def test_unknown_tool_is_a_structured_error_listing_the_valid_names():
    result = server.placeroot_call("find_place", {"lat": CENTER_LAT})
    assert result["error"] == "unknown_tool"
    assert "find_place" in result["detail"]
    assert result["valid_tools"] == sorted(server._TOOL_FUNCS)
    assert "find_places" in result["valid_tools"]


def test_unknown_argument_is_a_bad_request_naming_what_the_tool_accepts():
    result = server.placeroot_call("geocode", {"querry": "Brooklyn"})
    assert result["error"] == "bad_request"
    assert result["accepts"] == "query,limit?,lang?"


def test_missing_required_argument_is_a_bad_request():
    result = server.placeroot_call("geocode", {})
    assert result["error"] == "bad_request"
    assert "query" in result["detail"]


def test_non_object_args_is_a_bad_request():
    result = server.placeroot_call("geocode", ["Brooklyn"])
    assert result["error"] == "bad_request"
    assert "geocode" in result["detail"]


# --- Argument types --------------------------------------------
#
# The catalog ships argument names without types, so a caller guessing a type
# wrong is the expected path, not the exotic one. Dispatch therefore validates
# through each tool's own pydantic model — the one the SDK builds for a direct
# tools/call — so a wrong type is this dispatcher's bad_request and a coercible
# one reaches the tool exactly as it would over the wire.


def test_a_string_where_a_list_belongs_is_rejected_not_iterated():
    """`queries="paris"` used to iterate the string: five per-character
    geocodes, a wrong answer with no error and five times the scan.
    """
    result = server.placeroot_call("geocode_batch", {"queries": "paris"})
    assert result["error"] == "bad_request"
    assert "queries" in result["detail"]
    assert result["accepts"] == server._arg_summary(server.geocode_batch)


def test_a_stringy_number_is_coerced_the_way_a_direct_call_coerces_it():
    """`limit="5"` used to raise a raw TypeError out of the dispatcher."""
    through = server.placeroot_call("geocode", {"query": "Brooklyn", "limit": "5"})
    direct = server.geocode(query="Brooklyn", limit=5)
    assert json.dumps(through, default=str, sort_keys=True) == json.dumps(
        direct, default=str, sort_keys=True
    )


def test_a_bad_type_is_rejected_before_the_tool_writes_anything(tmp_path, monkeypatch):
    """render_map with a string `result` used to write a junk artifact."""
    monkeypatch.setenv("PLACEROOT_ARTIFACT_DIR", str(tmp_path))
    result = server.placeroot_call("render_map", {"result": "hello"})
    assert result["error"] == "bad_request"
    assert "result" in result["detail"]
    assert list(tmp_path.iterdir()) == []


TYPE_PARITY = [
    ("geocode", {"query": "Brooklyn", "limit": "5"}, {"query": "Brooklyn", "limit": 5}),
    (
        "find_places",
        {"lat": str(CENTER_LAT), "lon": str(CENTER_LON), "radius_m": "1000", "limit": "5"},
        {"lat": CENTER_LAT, "lon": CENTER_LON, "radius_m": 1000, "limit": 5},
    ),
]


@pytest.mark.parametrize("tool,stringy,typed", TYPE_PARITY, ids=[t for t, _, _ in TYPE_PARITY])
def test_coerced_dispatch_matches_the_direct_call_byte_for_byte(tool, stringy, typed):
    direct = getattr(server, tool)(**typed)
    through = server.placeroot_call(tool, stringy)
    assert json.dumps(through, default=str, sort_keys=True) == json.dumps(
        direct, default=str, sort_keys=True
    )


def test_an_uncoercible_value_is_rejected_by_the_same_model_the_sdk_uses():
    """Parity on the rejection side: what the tool's own arg model refuses,
    dispatch refuses too, rather than handing the tool a value it cannot use.
    """
    with pytest.raises(ValidationError):
        server._arg_metadata(server.geocode).validate_arguments({"query": "x", "limit": "many"})
    result = server.placeroot_call("geocode", {"query": "x", "limit": "many"})
    assert result["error"] == "bad_request"
    assert "limit" in result["detail"]
    assert result["accepts"] == "query,limit?,lang?"


def test_the_validation_detail_is_advice_not_a_pydantic_dump():
    result = server.placeroot_call("geocode", {"query": "x", "limit": "many"})
    assert "https://errors.pydantic.dev" not in result["detail"]
    assert result["detail"].startswith("geocode: ")


def test_a_tools_own_error_is_passed_through_unchanged():
    """The dispatcher reports argument problems; everything else is the
    tool's answer, including its structured errors.
    """
    assert server.placeroot_call("find_places", {"lat": 91.0, "lon": 0.0}) == server.find_places(
        lat=91.0, lon=0.0
    )


def test_dispatch_works_through_the_registered_mcp_tool():
    """End to end over the server, not just the plain function.

    Asserted on the payload rather than on the result existing: call_tool
    returns a CallToolResult for a failed dispatch too, so `is not None` held
    even when the answer was an unknown_tool error.
    """
    result = asyncio.run(
        server.build_server("progressive").call_tool(
            "placeroot_call", {"tool": "data_version", "args": {}}
        )
    )
    assert not result.is_error
    payload = json.loads(result.content[0].text)
    assert "error" not in payload, payload
    assert payload["release"] == server.data_version()["release"]
    assert payload == server.data_version()


# --- Composition ------------------------------------------------------------


@pytest.mark.parametrize("spec", ["progressive,core", "core,progressive", "progressive,all"])
def test_mixing_progressive_with_other_names_fails_loudly(spec):
    with pytest.raises(tool_profiles.InvalidToolSelection) as excinfo:
        server.build_server(spec)
    message = str(excinfo.value)
    assert "progressive" in message
    assert "cannot be combined" in message


def test_mixing_fails_before_registering_either_surface():
    """The failure mode this replaces would have registered both."""
    with pytest.raises(tool_profiles.InvalidToolSelection):
        tool_profiles.resolve("progressive,geometry", set(server._TOOL_FUNCS))


def test_repeating_progressive_is_not_a_mix():
    assert _names("progressive,progressive") == _names("progressive")


def test_progressive_is_named_in_the_unknown_name_error():
    with pytest.raises(tool_profiles.InvalidToolSelection) as excinfo:
        server.build_server("nonesuch")
    assert "progressive" in str(excinfo.value)


# --- Annotations ------------------------------------------------------------


def test_placeroot_call_does_not_claim_to_be_read_only():
    """It can reach render_map, which writes a file; readOnlyHint is what
    clients gate auto-approval on.
    """
    tools = {t.name: t for t in _tools("progressive")}
    annotations = tools["placeroot_call"].annotations
    assert annotations.read_only_hint is False
    assert annotations.idempotent_hint is False
    assert annotations.destructive_hint is False
    assert annotations.open_world_hint is False


def test_the_catalog_tool_is_read_only():
    annotations = {t.name: t for t in _tools("progressive")}["placeroot_capabilities"].annotations
    assert annotations.read_only_hint is True


def test_every_meta_tool_is_annotated_and_titled():
    for tool in _tools("progressive"):
        assert tool.annotations is not None, tool.name
        assert (tool.title or "").strip(), tool.name
