"""Request-scoped client session identity.

placeroot keeps a little per-conversation state outside the tile cache:
the last city a resolve_place call pinned (geocode.py, #329) and the
travel-preferences document (preferences.py, #315). Over stdio one process
serves one client, so "the process" and "the session" are the same thing.
Over --http (streamable HTTP) one process serves every connected client,
and a module global is shared by all of them: client A's inferred city
answered client B's POI-shaped query, and A's stored travel mode became
B's default.

This module is the one place that knows which client the current call is
for. server.py's middleware binds the session id for the duration of each
request (contextvars follow the request onto the worker thread the SDK
runs a sync tool on, via anyio's context propagation), and the modules
holding per-conversation state key it by `session_id()`.

How the id is derived (server._session_id_of):

* streamable HTTP, stateful (the default): the SDK's `Mcp-Session-Id`
  (`StreamableHTTPSessionManager` passes `http_transport.mcp_session_id`
  to `serve_loop`, which stores it as `Connection.session_id`; the
  middleware's `ctx.session` is a `ServerSession` over that connection).
* stdio: `Connection.session_id` is None and the request carries no HTTP
  request object -> `STDIO_SESSION_ID`, one process = one session.
* streamable HTTP without a session: a stateless app, or a 2026-07-28
  client (that era dropped the handshake and with it the session: the
  SDK serves each request on a fresh `Connection` and the client never
  learns a session id) -> a fresh *ephemeral* id per request
  (`new_ephemeral_id`). Nothing can persist between two such requests,
  which is the protocol's own contract, and the per-session stores skip
  ephemeral ids (`is_ephemeral`) rather than let a flood of sessionless
  requests evict the sessions that do have state.

Outside any bound request (library use, tests) `session_id()` is the
stdio constant, which keeps the single-process behaviour byte-identical.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

STDIO_SESSION_ID = "stdio"
EPHEMERAL_PREFIX = "request:"

current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "placeroot_session_id", default=STDIO_SESSION_ID
)


def session_id() -> str:
    """The client session the current call belongs to."""
    return current_session_id.get()


def new_ephemeral_id() -> str:
    """A session id for one request that no later request can share.

    Unique rather than derived from an object's identity: `id()` of a
    per-request connection is reused once it is freed, which would hand a
    later client an earlier one's state — the leak this module exists to
    close.
    """
    return f"{EPHEMERAL_PREFIX}{uuid.uuid4().hex}"


def is_ephemeral(sid: str | None = None) -> bool:
    """Whether `sid` (default: the current session) lives for one request only."""
    return (session_id() if sid is None else sid).startswith(EPHEMERAL_PREFIX)


@contextmanager
def bind_session(id: str) -> Iterator[str]:  # noqa: A002 - public name, mirrors the id
    """Run the block with `id` as the current session id, restoring afterwards."""
    token = current_session_id.set(id)
    try:
        yield id
    finally:
        current_session_id.reset(token)
