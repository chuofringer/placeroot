"""CLI transport selection (#24) and an end-to-end streamable-HTTP smoke test.

The HTTP server is started in-thread (not a subprocess) so it shares this
process's module state — in particular the `offline_data` autouse fixture's
`overture.set_data_path(...)` fixture overrides, which are plain module
globals, not environment variables a subprocess would inherit.
"""

import json
import socket
import threading

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_http_server():
    """Start placeroot's streamable-HTTP transport on an ephemeral port.

    Uses uvicorn.Server directly (rather than server.mcp.run(), which blocks
    forever) so the test can request a graceful shutdown afterward; this is
    the same Starlette app server.mcp.run(transport="streamable-http", ...)
    builds internally (mcp.streamable_http_app()), so it exercises the exact
    code path --http uses in production.
    """
    try:
        import uvicorn
    except ImportError:
        pytest.skip("uvicorn not installed; this SDK build lacks streamable-HTTP support")

    host = "127.0.0.1"
    port = _free_port()
    app = server.mcp.streamable_http_app(host=host)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    usrv = uvicorn.Server(config)

    thread = threading.Thread(target=usrv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if usrv.started:
            break
        threading.Event().wait(0.05)
    else:
        pytest.fail("streamable-HTTP server did not start within 10s")

    try:
        yield f"http://{host}:{port}/mcp"
    finally:
        usrv.should_exit = True
        thread.join(timeout=5)


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
            # structured_content isn't populated by this tool (plain dict
            # return, no output schema), so parse the text content block —
            # the same JSON body a stdio client would receive.
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
