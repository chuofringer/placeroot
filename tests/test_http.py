"""CLI transport selection (#24) and an end-to-end streamable-HTTP smoke test.

The server itself is started by the `running_http_server` fixture in
conftest.py, shared with tests/test_caching.py.
"""

import json

import pytest

from placeroot import server

from .conftest import CENTER_LAT, CENTER_LON


def test_parse_transport_args_defaults_to_stdio():
    args = server.parse_transport_args([])
    assert args.http is False
    assert args.host == server.DEFAULT_HTTP_HOST
    assert args.port == server.DEFAULT_HTTP_PORT


def test_parse_transport_args_selects_http():
    args = server.parse_transport_args(["--http"])
    assert args.http is True
    assert args.host == server.DEFAULT_HTTP_HOST
    assert args.port == server.DEFAULT_HTTP_PORT


def test_parse_transport_args_custom_host_and_port():
    args = server.parse_transport_args(["--http", "--host", "0.0.0.0", "--port", "9999"])
    assert args.http is True
    assert args.host == "0.0.0.0"
    assert args.port == 9999


def test_http_find_places_matches_stdio_path(running_http_server):
    """The HTTP transport must answer find_places identically to calling the
    tool function directly (the effective stdio path, since MCPServer.run()
    for stdio just dispatches JSON-RPC to the same tool functions)."""
    try:
        from mcp.client.client import Client
    except ImportError:
        pytest.skip("mcp.client streamable-HTTP client not available in this SDK build")
    import anyio

    direct = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=5)

    async def call_over_http():
        async with Client(running_http_server) as client:
            result = await client.call_tool(
                "find_places",
                {"lat": CENTER_LAT, "lon": CENTER_LON, "radius_m": 1000, "limit": 5},
            )
            assert result.is_error is False
            # find_places now declares an outputSchema (roadmap §4.3, #403),
            # so structured_content is populated too — parse the text content
            # block anyway: it's the same JSON body a stdio client would
            # receive, and content vs structured_content is asserted equal
            # in tests/test_output_schemas.py.
            return json.loads(result.content[0].text)

    over_http = anyio.run(call_over_http)
    assert over_http == direct


def test_http_initialize_reports_placeroot_server_info(running_http_server):
    """The initialize handshake over HTTP must identify the same server
    ("placeroot") a stdio client would see — same MCPServer instance, same
    tool set, just a different transport."""
    try:
        from mcp.client.client import Client
    except ImportError:
        pytest.skip("mcp.client streamable-HTTP client not available in this SDK build")
    import anyio

    async def call_over_http():
        async with Client(running_http_server) as client:
            # Client.__aenter__ already ran the handshake (discover or
            # initialize, depending on server support) before yielding.
            return client.session.server_info

    server_info = anyio.run(call_over_http)
    assert server_info is not None
    assert server_info.name == "placeroot"
