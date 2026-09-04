"""Published schemas carry enums + a stated default for the string
parameters whose valid values used to live only in prose (roadmap
docs/ROADMAP.md #4.3): mode, prefer, op, operating_status.

Runtime validation is untouched — these params stay plain `str | None`
(or `str` for geometry_op's `op`) so an unsupported value still reaches
the function and comes back as the tool's own structured self-correcting
error (unsupported_mode, bad_request with `supported:` / `valid ops:`,
etc.) instead of being rejected by pydantic before the tool ever runs.
That behavior is guarded by each tool's own existing tests; this file
only checks what the published schema now says.
"""

import asyncio

import pytest

from placeroot import overture, routing, server


def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


MODE_ENUM = sorted(("walk", "cycle", "drive"))

# tool name -> (param name, expected enum, default substring expected in
# the schema description).
MODE_TOOLS = {
    "route": ("mode", MODE_ENUM, "else drive"),
    "isochrone": ("mode", MODE_ENUM, "else walk"),
    "from_to": ("mode", MODE_ENUM, "else walk"),
    "travel_time_matrix": ("mode", MODE_ENUM, "else walk"),
    "optimize_route": ("mode", MODE_ENUM, "else drive"),
    "ground_location": ("mode", MODE_ENUM, "else walk"),
    "places_along_route": ("mode", MODE_ENUM, "else drive"),
    "neighborhood_verdict": ("mode", MODE_ENUM, "else walk"),
    "preferences": ("mode", MODE_ENUM, None),
    "map_match": ("mode", MODE_ENUM, "else walk"),
}


@pytest.mark.parametrize("tool_name", sorted(MODE_TOOLS))
def test_mode_schema_carries_enum_and_default(tool_name):
    param, enum, default_text = MODE_TOOLS[tool_name]
    props = _tools()[tool_name].input_schema["properties"]
    schema = props[param]
    assert schema["enum"] == enum
    if default_text is not None:
        assert default_text in schema["description"]


@pytest.mark.parametrize("tool_name", ["route", "from_to"])
def test_prefer_schema_carries_enum(tool_name):
    props = _tools()[tool_name].input_schema["properties"]
    assert props["prefer"]["enum"] == sorted(routing.SUPPORTED_PREFERENCES)


def test_geometry_op_schema_carries_enum():
    props = _tools()["geometry_op"].input_schema["properties"]
    assert props["op"]["enum"] == sorted(server._GEOMETRY_OP_REQUIRED)


def test_find_places_operating_status_schema_carries_enum():
    props = _tools()["find_places"].input_schema["properties"]
    assert props["operating_status"]["enum"] == overture.accepted_operating_status_values()


def test_every_tool_taking_top_level_mode_is_covered():
    """Catch a new/renamed top-level `mode` param that skipped the enum.

    meeting_point and suggest_areas take `mode` nested inside a list
    parameter (per-origin/per-anchor), not as a top-level string, so they
    are correctly absent from MODE_TOOLS; this only guards top-level
    params named exactly `mode`.
    """
    top_level_mode_tools = set()
    for name, tool in _tools().items():
        props = tool.input_schema.get("properties", {})
        schema = props.get("mode")
        if schema is not None and schema.get("type") in (None, "string"):
            top_level_mode_tools.add(name)
    assert top_level_mode_tools == set(MODE_TOOLS)


def test_mode_enum_stays_plain_string_at_runtime():
    """The Annotated[...] schema sugar must not become a Literal: an
    unsupported mode has to reach the function body so its own structured
    error runs, never a pydantic ValidationError."""
    result = server.route(0.0, 0.0, 0.001, 0.001, mode="hovercraft")
    assert result["error"] == "unsupported_mode"
