"""Declared MCP `outputSchema` drift-proofing (roadmap §4 feature 3 / §5.3).

`server._publish_output_schemas()` attaches hand-authored schemas
(`placeroot.output_schemas`) to every registered tool, and also wires a
permissive pass-through `output_model` so real `tools/call` answers ship as
`structuredContent` too — see that function's docstring for why: a
spec-compliant client requires structuredContent whenever a tool declares an
outputSchema (`mcp.client.session.ClientSession.validate_tool_result`), so
the schema can't be published without also producing it. This file is what
makes the resulting claim honest rather than aspirational:

- every declared schema is valid JSON Schema, and every registered tool has
  one (no silent gaps as the surface grows);
- a representative call still succeeds through the real MCP layer with a
  schema declared, and its structuredContent is byte-identical (once
  reparsed) to the text content every client saw before this feature —
  the runtime-safety guarantee, as a regression test;
- for each first-wave tool (precise schemas, not the generic envelope), a
  real success answer AND a real error envelope, both produced by actually
  calling the tool against the committed offline fixtures, validate against
  its declared schema — both as the tool's own return value and as the
  structuredContent a client actually receives. `jsonschema` is not an added
  dependency: it is `mcp[cli]`'s own direct dependency (see uv.lock),
  already installed wherever placeroot is.
"""

from __future__ import annotations

import asyncio
import json

import jsonschema
import pytest

from placeroot import output_schemas, overture, server
from tests._routing_fixture import build_routing_fixture as fx

from .conftest import CENTER_LAT, CENTER_LON, DIVISIONS_FIXTURE_PATH

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)
ISO_LAT, ISO_LON = fx.node_latlon(10, 10)

# Same shuffled collinear four tests/test_export.py uses for optimize_route.
_LINE_NODES = [(2, 8), (2, 2), (2, 11), (2, 5)]
LINE_STOPS = [{"lat": lat, "lon": lon} for lat, lon in (fx.node_latlon(*n) for n in _LINE_NODES)]


@pytest.fixture
def srv():
    return server.build_server()


def _schema_for(built, name: str) -> dict:
    tools = asyncio.run(built.list_tools())
    by_name = {t.name: t for t in tools}
    assert name in by_name, f"{name} is not registered on the default install"
    schema = by_name[name].output_schema
    assert schema is not None, f"{name} has no declared outputSchema"
    return schema


def _call(built, name: str, args: dict) -> dict:
    """Call a tool through the real MCP layer and return its parsed payload.

    Also asserts structuredContent is populated and matches content's text
    block exactly — the runtime-safety guarantee a real client depends on
    (mcp.client.session.ClientSession.validate_tool_result requires
    structuredContent on every successful call to a tool with a declared
    outputSchema; see _publish_output_schemas' docstring).
    """
    result = asyncio.run(built.call_tool(name, args))
    assert not getattr(result, "isError", False), f"{name}{args} raised: {result}"
    assert result.content, f"{name}{args} returned no content"
    payload = json.loads(result.content[0].text)
    assert result.structured_content == payload, (
        f"{name}{args}: structuredContent diverged from content's text block"
    )
    return payload


def _validate(schema: dict, payload: dict) -> None:
    jsonschema.validate(instance=payload, schema=schema)


# ---------------------------------------------------------------------------
# Meta-checks: every declared schema is legal, and coverage is exhaustive.
# ---------------------------------------------------------------------------


def test_every_declared_schema_is_valid_json_schema(srv):
    validator_cls = jsonschema.validators.validator_for({})
    tools = asyncio.run(srv.list_tools())
    assert tools, "no tools registered"
    for tool in tools:
        assert tool.output_schema is not None, f"{tool.name} has no declared outputSchema"
        validator_cls.check_schema(tool.output_schema)


def test_output_schemas_cover_every_registered_tool(srv):
    """Every tool either install registers has a declared schema, and
    output_schemas.py declares nothing for a tool that doesn't exist.

    The default install and `progressive` (the meta-tool surface) never
    register the same tool set at once (server.py's build_server docstring),
    so their union is exactly OUTPUT_SCHEMAS's key set.
    """
    default_names = {t.name for t in asyncio.run(srv.list_tools())}
    progressive_names = {
        t.name for t in asyncio.run(server.build_server("progressive").list_tools())
    }
    assert default_names | progressive_names == set(output_schemas.OUTPUT_SCHEMAS)


# ---------------------------------------------------------------------------
# Runtime-safety regression: the guarantee behind _publish_output_schemas.
#
# A spec-compliant client (confirmed against mcp.client.session.ClientSession
# .validate_tool_result, session.py:1080-1100) requires structuredContent on
# every successful call to a tool with a declared outputSchema, and validates
# it against exactly that schema — so a declared schema must both (a) never
# make the server-side call itself fail, and (b) always produce
# structuredContent that validates. `_publish_output_schemas` gets both by
# populating structuredContent through `_PermissiveOutput` (extra="allow", no
# required fields — model_validate(any_dict) can never raise) while the
# schema a client checks it against is the precise, additionalProperties-true
# one from output_schemas.py. `content`'s text block is computed from the
# same raw return value before that model ever sees it, so it stays
# byte-identical to what an unpatched call would have sent.
# ---------------------------------------------------------------------------


def test_a_tool_call_still_succeeds_with_a_schema_declared(srv):
    schema = _schema_for(srv, "data_version")
    assert schema is not None

    result = asyncio.run(srv.call_tool("data_version", {}))
    assert not getattr(result, "isError", False)
    assert result.content
    payload = json.loads(result.content[0].text)
    assert "release" in payload
    _validate(schema, payload)

    # structuredContent is populated and is exactly the same payload content
    # carries as text — the runtime-safety guarantee a real client depends on.
    assert result.structured_content == payload


def test_a_real_client_accepts_a_schema_declared_call(running_http_server):
    """End-to-end version of the guarantee above, through a real
    mcp.client.client.Client over streamable HTTP rather than calling
    MCPServer.call_tool() directly.

    Client.call_tool() routes through ClientSession.call_tool(), which —
    unlike this server's own call_tool() — runs validate_tool_result() on
    every non-error response (session.py:1063-1064) and RAISES RuntimeError
    if a tool declares an outputSchema but the response carries no
    structuredContent, or structuredContent that fails to validate against
    it. This is the check that caught the original publication-only design
    (see _publish_output_schemas' docstring, part 2): a tool declaring a
    schema but never populating structuredContent would fail here, on every
    single successful call, for any client actually enforcing the spec.
    """
    try:
        from mcp.client.client import Client
    except ImportError:
        pytest.skip("mcp.client streamable-HTTP client not available in this SDK build")
    import anyio

    async def call_over_http():
        async with Client(running_http_server) as client:
            result = await client.call_tool(
                "find_places",
                {"lat": CENTER_LAT, "lon": CENTER_LON, "radius_m": 1000, "limit": 5},
            )
            assert result.is_error is False
            assert result.structured_content is not None
            return result

    result = anyio.run(call_over_http)
    assert result.content


# ---------------------------------------------------------------------------
# First wave: precise per-tool schemas, checked against real success and
# error answers pulled from the committed offline fixtures.
# ---------------------------------------------------------------------------


def test_find_places(srv):
    schema = _schema_for(srv, "find_places")
    success = _call(
        srv, "find_places", {"lat": CENTER_LAT, "lon": CENTER_LON, "radius_m": 1000, "limit": 10}
    )
    assert "error" not in success
    _validate(schema, success)

    error = _call(srv, "find_places", {})
    assert error["error"] == "bad_request"
    _validate(schema, error)


def test_find_near(srv):
    schema = _schema_for(srv, "find_near")
    success = _call(srv, "find_near", {"category": "coffee_shop", "near": "Blue Bottle Roastery"})
    assert "error" not in success
    _validate(schema, success)

    error = _call(srv, "find_near", {"category": "  ", "near": "Brooklyn"})
    assert error["error"] == "bad_request"
    _validate(schema, error)


def test_geocode(srv, tmp_path):
    schema = _schema_for(srv, "geocode")
    success = _call(srv, "geocode", {"query": "Brooklyn", "limit": 5})
    assert "error" not in success
    _validate(schema, success)

    overture.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), theme="divisions", type_="division"
    )
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"), theme="places")
    try:
        error = _call(srv, "geocode", {"query": "Brooklyn", "limit": 5})
        assert error["error"] == "upstream_unavailable"
        _validate(schema, error)
    finally:
        overture.set_data_path(str(DIVISIONS_FIXTURE_PATH), theme="divisions", type_="division")


def test_geocode_batch(srv):
    schema = _schema_for(srv, "geocode_batch")
    success = _call(srv, "geocode_batch", {"queries": ["Brooklyn", "Riverside"]})
    assert "error" not in success
    _validate(schema, success)

    error = _call(srv, "geocode_batch", {"queries": ["Brooklyn"] * 21})
    assert "error" in error
    assert "results" not in error
    _validate(schema, error)


def test_resolve_place(srv, tmp_path):
    schema = _schema_for(srv, "resolve_place")
    success = _call(
        srv,
        "resolve_place",
        {
            "query": "Blue Bottle Roastery",
            "near_lat": CENTER_LAT,
            "near_lon": CENTER_LON,
            "limit": 5,
        },
    )
    assert "error" not in success
    _validate(schema, success)

    overture.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), theme="divisions", type_="division"
    )
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"), theme="places")
    try:
        error = _call(srv, "resolve_place", {"query": "Brooklyn", "limit": 5})
        assert error["error"] == "upstream_unavailable"
        _validate(schema, error)
    finally:
        overture.set_data_path(str(DIVISIONS_FIXTURE_PATH), theme="divisions", type_="division")


def test_from_to(srv):
    schema = _schema_for(srv, "from_to")
    success = _call(
        srv,
        "from_to",
        {
            "from": {"lat": FROM_LAT, "lon": FROM_LON},
            "to": {"lat": TO_LAT, "lon": TO_LON},
            "mode": "walk",
            "confirm": True,
        },
    )
    assert "error" not in success
    _validate(schema, success)

    error = _call(
        srv, "from_to", {"from": {"lat": 91.0, "lon": 0.0}, "to": {"lat": TO_LAT, "lon": TO_LON}}
    )
    assert error["error"] == "bad_request"
    _validate(schema, error)


def test_route(srv):
    schema = _schema_for(srv, "route")
    success = _call(
        srv,
        "route",
        {
            "from_lat": FROM_LAT,
            "from_lon": FROM_LON,
            "to_lat": TO_LAT,
            "to_lon": TO_LON,
            "mode": "walk",
            "confirm": True,
        },
    )
    assert "error" not in success
    _validate(schema, success)

    error = _call(
        srv,
        "route",
        {
            "from_lat": FROM_LAT,
            "from_lon": FROM_LON,
            "to_lat": TO_LAT,
            "to_lon": TO_LON,
            "mode": "teleport",
        },
    )
    assert error["error"] == "unsupported_mode"
    _validate(schema, error)


def test_isochrone(srv):
    schema = _schema_for(srv, "isochrone")
    success = _call(srv, "isochrone", {"lat": ISO_LAT, "lon": ISO_LON, "minutes": 15})
    assert "error" not in success
    _validate(schema, success)

    error = _call(srv, "isochrone", {"minutes": 15})
    assert error["error"] == "bad_request"
    _validate(schema, error)


def test_travel_time_matrix(srv):
    schema = _schema_for(srv, "travel_time_matrix")
    origins = [{"lat": FROM_LAT, "lon": FROM_LON}]
    destinations = [{"lat": TO_LAT, "lon": TO_LON}]
    success = _call(
        srv,
        "travel_time_matrix",
        {"origins": origins, "destinations": destinations, "mode": "walk"},
    )
    assert "error" not in success
    _validate(schema, success)

    error = _call(
        srv,
        "travel_time_matrix",
        {"origins": [{"lat": 0.0, "lon": 0.0}] * 6, "destinations": destinations},
    )
    assert error["error"] == "bad_request"
    _validate(schema, error)


def test_distance_matrix(srv):
    schema = _schema_for(srv, "distance_matrix")
    origins = [{"lat": 30.2672, "lon": -97.7431}]
    destinations = [{"lat": 29.7604, "lon": -95.3698}]
    success = _call(srv, "distance_matrix", {"origins": origins, "destinations": destinations})
    assert "error" not in success
    _validate(schema, success)

    error = _call(
        srv, "distance_matrix", {"origins": [{"lon": -97.7431}], "destinations": destinations}
    )
    assert error["error"] == "bad_request"
    _validate(schema, error)


def test_optimize_route(srv):
    schema = _schema_for(srv, "optimize_route")
    success = _call(
        srv,
        "optimize_route",
        {"stops": LINE_STOPS, "mode": "walk", "roundtrip": False, "confirm": True},
    )
    assert "error" not in success
    _validate(schema, success)

    error = _call(srv, "optimize_route", {"stops": LINE_STOPS, "mode": "teleport"})
    assert error["error"] == "unsupported_mode"
    _validate(schema, error)


def test_data_version(srv):
    """No-argument tool: there is no bad_request to trigger, so only the
    success arm is exercised here. ERROR_SCHEMA itself is still validated,
    both structurally (test_every_declared_schema_is_valid_json_schema) and
    against real error payloads from every other first-wave tool above."""
    schema = _schema_for(srv, "data_version")
    success = _call(srv, "data_version", {})
    assert "error" not in success
    _validate(schema, success)
