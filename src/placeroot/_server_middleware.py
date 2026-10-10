"""Request middleware for the PlaceRoot MCP server: session, progress and trace.

Moved verbatim out of server.py (no behaviour change): the three middlewares build_server()
installs, and the payload helpers they share (tool-name lookup, amending a tool result,
rendering its text, the per-client session id). The trace middleware logs on the
"placeroot.server" channel, as it always did, so log filters and caplog are unaffected.
server.py re-exports every name.
"""

import asyncio
import functools
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping

from placeroot import (
    progress,
    session,
    trace,
)

# Same log channel as when this lived in server.py: caplog and log config
# key off "placeroot.server", not this module's name.
logger = logging.getLogger("placeroot.server")


def _tool_name_of(ctx) -> str:
    """The tool a tools/call request names, from its raw inbound params.

    The middleware context carries the request's unvalidated params as
    `ctx.params` (a mapping, per mcp.server.context.ServerRequestContext);
    there is no `tool_name` attribute, so reading one would label every
    trace "tools/call".
    """
    params = getattr(ctx, "params", None)
    name = params.get("name") if isinstance(params, Mapping) else None
    return name if isinstance(name, str) and name else "tools/call"


def _amend_tool_payload(result, amend: Callable[[dict], dict]):
    """Apply `amend` to the tool's JSON answer inside a tools/call wire result.

    What `call_next` hands a middleware for tools/call is not the handler's
    CallToolResult but its *wire form*: the SDK's ServerRunner serializes the
    handler result inside the chain (runner.py `_inner` returns
    `self._serialize(...)`), so a middleware sees a dict shaped
    {"content": [{"type": "text", "text": <json>}], "structuredContent":
    {...}, "isError": false, ...}. The tool's answer lives in
    structuredContent (every tool here declares an outputSchema, so a
    successful call always has it) and, for clients that only read text, as
    the same JSON in the single text block. A key written onto the envelope
    itself — which is what `result["timing"] = ...` used to do — is not part
    of the answer and never reaches the agent.

    So this amends structuredContent and re-renders the text block from it
    (pydantic_core.to_json, the SDK's own rendering, so the two stay
    byte-identical — tests/test_output_schemas.py asserts that). Anything
    else — a non-dict, an isError result whose text is an exception message,
    a result with no JSON-object payload — is returned untouched.
    """
    if not isinstance(result, dict) or result.get("isError"):
        return result
    payload = result.get("structuredContent")
    content = result.get("content")
    text_block = (
        content[0]
        if isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        else None
    )
    if not isinstance(payload, dict):
        # No structuredContent (a tool without an outputSchema): the text
        # block is the only copy of the answer, when it is a JSON object.
        if text_block is None:
            return result
        try:
            payload = json.loads(text_block["text"])
        except (TypeError, ValueError):
            return result
        if not isinstance(payload, dict):
            return result
        amended = amend(dict(payload))
        if amended == payload:
            return result
        out = dict(result)
        out["content"] = [{**text_block, "text": _render_tool_text(amended)}]
        return out
    amended = amend(dict(payload))
    if amended == payload:
        return result
    out = dict(result)
    out["structuredContent"] = amended
    if text_block is not None:
        out["content"] = [{**text_block, "text": _render_tool_text(amended)}]
    return out


def _render_tool_text(payload: dict) -> str:
    """The text block the SDK renders for a dict tool result (func_metadata's
    _convert_to_content), so an amended answer reads the same as an
    unamended one."""
    import pydantic_core

    return pydantic_core.to_json(payload, fallback=str, indent=2).decode()


def _session_id_of(ctx) -> str:
    """The client session a request belongs to, from the SDK's request context.

    Over stateful streamable HTTP (the default for --http) the SDK's
    StreamableHTTPSessionManager hands `http_transport.mcp_session_id` —
    the `Mcp-Session-Id` the client echoes on every request — to
    `serve_loop`, which stores it as `Connection.session_id`; the
    middleware's `ctx.session` is a `ServerSession` over that connection
    (its `_connection`; the SDK exposes no public accessor from the
    middleware tier). That id is the stable per-client identity. Where the
    connection carries none, the request itself decides: no HTTP request
    object means stdio (one process = one session, session.STDIO_SESSION_ID).
    An HTTP request without one — a stateless app, or a 2026-07-28 client,
    whose era has no handshake and no session (the SDK's
    `handle_modern_request` builds a fresh `Connection` per request and the
    client never learns an id) — uses the header if the client sent one and
    is otherwise its own ephemeral session: nothing persists between two
    such requests, which is that era's own contract, and nothing leaks.
    """
    connection = getattr(getattr(ctx, "session", None), "_connection", None)
    sid = getattr(connection, "session_id", None)
    if isinstance(sid, str) and sid:
        return sid
    request = getattr(ctx, "request", None)
    if request is None:
        return session.STDIO_SESSION_ID
    headers = getattr(request, "headers", None)
    try:
        sid = headers.get("mcp-session-id") if headers is not None else None
    except Exception:  # noqa: BLE001 - an odd request object must not fail the call
        sid = None
    if isinstance(sid, str) and sid:
        return sid
    return session.new_ephemeral_id()


async def _session_middleware(ctx, call_next):
    """Bind the client session id for the whole request (session.py).

    Outermost in the chain so every handler — tools, resources, prompts —
    runs with session.session_id() set to the client it serves; the
    contextvar follows the request onto the worker thread the SDK runs a
    sync tool on (anyio copies the context), and into the pools server.py
    itself fans out to (which copy it explicitly). geocode's last-city
    memory and preferences' HTTP overlay key on it.
    """
    with session.bind_session(_session_id_of(ctx)):
        return await call_next(ctx)


async def _progress_middleware(ctx, call_next):
    """Narrate slow tool calls via MCP progress notifications.

    A cold query (first over a new area) legitimately spends tens of
    seconds in S3 scans and tile COPYs; without this, the client shows a
    silent spinner indistinguishable from a hang. When the caller attached
    a progressToken to its tools/call, this installs a request-scoped
    reporter (progress.set_reporter) that the query layer's phase
    boundaries feed — the start of a direct upstream scan, each tile COPY
    — and the client renders as live status. Every tools/call also starts
    a request-scoped log so attach() can put the same line on the JSON
    answer when the client never sent a token. Non-tool requests pass
    through untouched.

    The reporter is called from the worker thread the SDK runs a sync tool
    on, so the async send is scheduled onto the event loop with
    run_coroutine_threadsafe, fire-and-forget: progress must never block or
    fail the query it narrates (see progress.py's contract), and per the
    spec a progress send for a completed request is dropped harmlessly.
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)
    log_token = progress.begin()
    token = (ctx.meta or {}).get("progress_token")
    if token is None:
        try:
            result = await call_next(ctx)
            return _amend_tool_payload(result, progress.attach)
        finally:
            progress.reset_log(log_token)

    loop = asyncio.get_running_loop()
    session, request_id = ctx.session, ctx.request_id
    # The spec requires progress to increase with every notification on a
    # token. Call sites report per-phase counts that reset between phases
    # (tile 1..N for places, then 1..M for each base theme), so the wire
    # value is a per-request monotonic sequence instead; the human-facing
    # counts live in the message, which is what clients render anyway.
    seq = 0
    seq_lock = threading.Lock()

    def reporter(message: str, current: float | None, total: float | None) -> None:
        nonlocal seq
        # Scheduling happens under the same lock as the increment so the
        # wire order matches the sequence order even if two threads ever
        # report concurrently — a later value must not reach the loop first.
        with seq_lock:
            seq += 1
            future = asyncio.run_coroutine_threadsafe(
                session.send_progress_notification(
                    token,
                    seq,
                    None,
                    message,
                    related_request_id=request_id,
                ),
                loop,
            )
        # Consume the eventual result: a failed or cancelled send is already
        # best-effort (progress.py's contract) and must not surface as an
        # exception-was-never-retrieved warning at GC time.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())

    reset_token = progress.set_reporter(reporter)
    try:
        result = await call_next(ctx)
        return _amend_tool_payload(result, progress.attach)
    finally:
        progress.reset(reset_token)
        progress.reset_log(log_token)


async def _trace_middleware(ctx, call_next):
    """Record where a tool call spent its time, and let a slow one say so.

    Every latency investigation here has started with a user reporting "that
    took a minute" and ended with a number the server already knew while it
    was running — which phase, which scan, whether it was bounded. This
    middleware records that for every tools/call (trace.py), logs it under
    PLACEROOT_TRACE=1, and, when the call took longer than
    PLACEROOT_TRACE_SLOW_S, attaches the breakdown to the response as
    `timing` so the agent that waited gets the explanation with the answer.

    Attached only to JSON-object answers (see _amend_tool_payload) and only
    when slow: a fast call's payload is unchanged, byte for byte, and a tool
    returning a list or a scalar is left alone rather than being reshaped to
    carry telemetry.
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)

    token = trace.start()
    started = time.perf_counter()
    try:
        result = await call_next(ctx)
        # Inside the try: the records are read before `finally` resets them.
        return _amend_tool_payload(
            result, functools.partial(_with_timing, elapsed=time.perf_counter() - started)
        )
    finally:
        elapsed = time.perf_counter() - started
        try:
            trace.log_summary(_tool_name_of(ctx), elapsed)
        except Exception:  # noqa: BLE001 - telemetry must not fail the call
            logger.debug("trace summary failed", exc_info=True)
        trace.reset(token)


def _with_timing(payload: dict, *, elapsed: float) -> dict:
    """Add `timing` to a tool answer that took longer than the slow threshold."""
    threshold = trace.slow_threshold_s()
    if not threshold or elapsed < threshold or "timing" in payload:
        return payload
    rows = trace.summary()
    if not rows:
        return payload
    payload["timing"] = {
        "total_s": round(elapsed, 1),
        "phases": rows[:8],
        "note": (
            "This call was slow enough to explain itself. Scans marked "
            "bounded:false read everything they touch."
        ),
    }
    return payload
