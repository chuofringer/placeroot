"""MCP 2026-07-28 listing-cache hints: ttlMs/cacheScope on tools/list (#209).

The spec (SEP-2549) requires a `ttlMs` and a `cacheScope` on every
`resultType: "complete"` listing result, and the SDK fills them from the
`cache_hints` map `build_server` passes to `MCPServer`. Three things can
silently break that, and there is a test here for each:

- an SDK upgrade that drops or renames the mechanism — pinned by asserting
  the protocol revision the SDK negotiates and the hints we declare;
- the hint not reaching the wire — asserted end to end over streamable HTTP,
  where a real client's `tools/list` result must carry our 24h/public values;
- the hint leaking to an older client — asserted byte-identical, by running
  the SDK's own per-version result sieve over a hinted and an unhinted
  `tools/list` for each pre-2026-07-28 revision and comparing serialized JSON.

The last one matters most: the caching fields are new vocabulary, and a
pre-2026-07-28 client that saw them would be receiving a result its schema
does not describe.
"""

import asyncio
import functools
import json

import pytest
from mcp.server.caching import apply_cache_hint
from mcp.types import LATEST_PROTOCOL_VERSION, ListToolsResult
from mcp_types.methods import serialize_server_result

from placeroot import server

# Protocol revisions that predate SEP-2549. A client speaking one of these
# must not see ttlMs/cacheScope.
LEGACY_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


def test_sdk_speaks_the_2026_07_28_revision():
    """Pin the revision the caching declaration belongs to.

    If an SDK bump moved LATEST_PROTOCOL_VERSION backwards (or forwards past
    a revision that reworked caching), the hints below would still be set but
    would mean something different on the wire. Fail here, loudly, rather
    than shipping a declaration nobody negotiates.
    """
    assert LATEST_PROTOCOL_VERSION == "2026-07-28"


def test_tools_list_declares_a_one_day_public_ttl():
    """The declaration itself: 24h, public, on the built server."""
    hint = server.CACHE_HINTS["tools/list"]
    assert hint.ttl_ms == 24 * 60 * 60 * 1000
    assert hint.scope == "public"


def test_every_listing_method_is_hinted():
    """The spec requires hints on all cacheable listing methods, not just
    tools/list — a partially hinted server is a spec-incomplete server."""
    assert set(server.CACHE_HINTS) == {
        "server/discover",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    }


def _tools_list_result(mcp, *, hinted: bool) -> ListToolsResult:
    """A tools/list result as the handler produces it, with or without the
    server's cache hint applied — the same two steps ServerRunner takes
    before handing the result to the per-version sieve."""
    result = ListToolsResult(tools=asyncio.run(mcp.list_tools()))
    return apply_cache_hint(result, server.CACHE_HINTS["tools/list"]) if hinted else result


def _dump(result: ListToolsResult) -> dict:
    """The wire dict, dumped exactly as ServerRunner._dump_result does."""
    return result.model_dump(by_alias=True, mode="json", exclude_none=True)


@pytest.mark.parametrize("version", LEGACY_PROTOCOL_VERSIONS)
def test_older_clients_see_a_byte_identical_tools_list(version):
    """Pre-2026-07-28 negotiation is unchanged, to the byte.

    Serializing through the SDK's per-version surface is what strips the new
    fields; comparing the hinted and unhinted serializations for the same
    revision proves the hint cannot reach a client that predates it.
    """
    mcp = server.build_server()
    sieve = functools.partial(serialize_server_result, "tools/list", version)
    hinted = sieve(_dump(_tools_list_result(mcp, hinted=True)))
    plain = sieve(_dump(_tools_list_result(mcp, hinted=False)))

    assert json.dumps(hinted, sort_keys=True) == json.dumps(plain, sort_keys=True)
    assert "ttlMs" not in hinted
    assert "cacheScope" not in hinted


def test_current_clients_see_the_hint_on_the_wire():
    """...and the same serialization at 2026-07-28 does carry it."""
    mcp = server.build_server()
    dumped = serialize_server_result(
        "tools/list", LATEST_PROTOCOL_VERSION, _dump(_tools_list_result(mcp, hinted=True))
    )
    assert dumped["ttlMs"] == 24 * 60 * 60 * 1000
    assert dumped["cacheScope"] == "public"


def test_http_client_receives_the_ttl_end_to_end(running_http_server):
    """End to end over the real transport: a current-revision client's
    tools/list result carries the declaration, through handler dispatch,
    hint fill, serialization and the HTTP transport."""
    try:
        from mcp.client.client import Client
    except ImportError:
        pytest.skip("mcp.client streamable-HTTP client not available in this SDK build")
    import anyio

    async def list_over_http():
        async with Client(running_http_server) as client:
            # cache_mode="bypass" so the client's own cache cannot serve a hit
            # from the handshake and hide a missing hint on the fresh result.
            return await client.list_tools(cache_mode="bypass")

    result = anyio.run(list_over_http)
    assert result.ttl_ms == 24 * 60 * 60 * 1000
    assert result.cache_scope == "public"
    assert result.tools
