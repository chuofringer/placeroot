"""Per-client session state (session.py): the id, its binding, and the
bounded per-session stores keyed on it in geocode.py and preferences.py.

Over --http one placeroot process serves every connected client; before
session.py the last-city memory and the preferences document were process
globals, so one client's state answered another's calls.
"""

import contextvars
import threading
import types

from placeroot import geocode, preferences, server, session


def test_default_session_is_the_stdio_constant():
    assert session.STDIO_SESSION_ID == "stdio"
    assert session.session_id() == session.STDIO_SESSION_ID


def test_bind_session_sets_and_restores():
    with session.bind_session("a") as bound:
        assert bound == "a"
        assert session.session_id() == "a"
        with session.bind_session("b"):
            assert session.session_id() == "b"
        assert session.session_id() == "a"
    assert session.session_id() == session.STDIO_SESSION_ID


def test_bound_session_follows_a_copied_context_onto_a_thread():
    """The SDK runs sync tools on a worker thread via anyio, which copies the
    context; server.py's own pools copy it explicitly. Either way the id
    the middleware bound must be what the query layer sees."""
    seen = {}
    with session.bind_session("thread-session"):
        ctx = contextvars.copy_context()

        def record():
            seen["id"] = session.session_id()

        t = threading.Thread(target=ctx.run, args=(record,))
        t.start()
        t.join()
    assert seen["id"] == "thread-session"


# --- server._session_id_of: how the id is derived per transport ----------


def _ctx(session_id=None, request=None, with_connection=True):
    connection = types.SimpleNamespace(session_id=session_id) if with_connection else None
    return types.SimpleNamespace(
        session=types.SimpleNamespace(_connection=connection),
        request=request,
        method="tools/call",
    )


def test_stdio_request_maps_to_the_stdio_session():
    # stdio: Connection.session_id is None and there is no HTTP request object.
    assert server._session_id_of(_ctx()) == session.STDIO_SESSION_ID


def test_stateful_http_request_uses_the_mcp_session_id():
    # StreamableHTTPSessionManager hands http_transport.mcp_session_id to
    # serve_loop, which stores it as Connection.session_id.
    sid = "0123456789abcdef0123456789abcdef"
    request = types.SimpleNamespace(headers={"mcp-session-id": sid})
    assert server._session_id_of(_ctx(session_id=sid, request=request)) == sid


def test_http_request_without_a_connection_id_falls_back_to_the_header():
    sid = "fedcba9876543210fedcba9876543210"
    request = types.SimpleNamespace(headers={"mcp-session-id": sid})
    assert server._session_id_of(_ctx(session_id=None, request=request)) == sid


def test_sessionless_http_request_gets_its_own_ephemeral_id():
    """A 2026-07-28 client (no handshake, no session) or a stateless app:
    every request is its own session, so nothing can be shared or replayed."""
    request = types.SimpleNamespace(headers={})
    ctx = _ctx(session_id=None, request=request)
    first = server._session_id_of(ctx)
    second = server._session_id_of(ctx)
    assert first.startswith(session.EPHEMERAL_PREFIX) and session.is_ephemeral(first)
    assert first != second
    assert not session.is_ephemeral(session.STDIO_SESSION_ID)


def test_ephemeral_sessions_store_nothing():
    geocode.clear_resolve_session(clear_all=True)
    preferences.clear_overlays()
    with session.bind_session(session.new_ephemeral_id()):
        geocode._remember_last_city("Brooklyn", {"lat": 40.7, "lon": -73.9})
        assert geocode._last_good() == (None, None)
        # The call still answers with what it was given, just for this request.
        assert preferences.update(mode="cycle")["mode"] == "cycle"
        assert preferences.get("mode") is None
    assert not geocode._last_good_by_session
    assert not preferences._overlays


def test_session_middleware_binds_for_the_handler(anyio_backend="asyncio"):
    import anyio

    seen = {}

    async def handler(ctx):
        seen["id"] = session.session_id()
        return {"ok": True}

    async def run():
        sid = "abcdefabcdefabcdefabcdefabcdefab"
        request = types.SimpleNamespace(headers={"mcp-session-id": sid})
        result = await server._session_middleware(_ctx(session_id=sid, request=request), handler)
        assert result == {"ok": True}
        assert seen["id"] == sid
        assert session.session_id() == session.STDIO_SESSION_ID

    anyio.run(run)


def test_session_middleware_wraps_the_other_placeroot_middleware():
    chain = server.mcp._lowlevel_server.middleware
    order = [
        chain.index(m)
        for m in (
            server._session_middleware,
            server._progress_middleware,
            server._trace_middleware,
        )
    ]
    assert order == sorted(order), "chain composes outermost-first; session must bind first"


# --- geocode: per-session last-city memory, bounded ------------------------


def test_last_city_is_kept_per_session():
    geocode.clear_resolve_session(clear_all=True)
    pin = {"lat": 40.7, "lon": -73.9}
    with session.bind_session("a"):
        geocode._remember_last_city("Brooklyn", pin)
        assert geocode._last_good() == ("Brooklyn", (40.7, -73.9))
    with session.bind_session("b"):
        assert geocode._last_good() == (None, None)
        geocode._remember_last_city("Paris", {"lat": 48.857, "lon": 2.351})
    with session.bind_session("a"):
        assert geocode._last_good()[0] == "Brooklyn"
    assert geocode._last_good() == (None, None)
    geocode.clear_resolve_session(clear_all=True)


def test_clear_resolve_session_drops_only_the_current_session_by_default():
    geocode.clear_resolve_session(clear_all=True)
    pin = {"lat": 40.7, "lon": -73.9}
    with session.bind_session("a"):
        geocode._remember_last_city("Brooklyn", pin)
    with session.bind_session("b"):
        geocode._remember_last_city("Paris", pin)
        geocode.clear_resolve_session()
        assert geocode._last_good() == (None, None)
    with session.bind_session("a"):
        assert geocode._last_good()[0] == "Brooklyn"
    geocode.clear_resolve_session(clear_all=True)
    with session.bind_session("a"):
        assert geocode._last_good() == (None, None)


def test_last_city_sessions_are_bounded_lru():
    geocode.clear_resolve_session(clear_all=True)
    cap = geocode._LAST_GOOD_SESSIONS_MAX
    assert cap == 256
    pin = {"lat": 1.0, "lon": 2.0}
    for i in range(cap + 10):
        with session.bind_session(f"s{i}"):
            geocode._remember_last_city(f"City {i}", pin)
    assert len(geocode._last_good_by_session) == cap
    with session.bind_session("s0"):
        assert geocode._last_good() == (None, None)  # evicted, oldest first
    with session.bind_session(f"s{cap + 9}"):
        assert geocode._last_good()[0] == f"City {cap + 9}"
    # A read refreshes recency: s10 survives the next insertion, s11 does not.
    with session.bind_session("s10"):
        assert geocode._last_good()[0] == "City 10"
    with session.bind_session("one-more"):
        geocode._remember_last_city("More", pin)
    with session.bind_session("s10"):
        assert geocode._last_good()[0] == "City 10"
    with session.bind_session("s11"):
        assert geocode._last_good() == (None, None)
    geocode.clear_resolve_session(clear_all=True)


# --- preferences: per-session overlay, bounded ----------------------------


def test_preference_overlays_are_bounded_lru():
    preferences.clear_overlays()
    cap = preferences._OVERLAY_SESSIONS_MAX
    assert cap == 256
    for i in range(cap + 5):
        with session.bind_session(f"p{i}"):
            preferences.update(mode="cycle")
    assert len(preferences._overlays) == cap
    with session.bind_session("p0"):
        assert preferences.get("mode") is None
    with session.bind_session(f"p{cap + 4}"):
        assert preferences.get("mode") == "cycle"
    preferences.clear_overlays()
