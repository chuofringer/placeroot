"""In-band progress for slow queries: "fetching a lot of data", not a hang.

A cold query — the first over a new area — can spend tens of seconds in
S3 scans and tile COPYs. The answer is still correct and still arrives,
but a client staring at a silent spinner can't tell that from a hung
server. MCP has a channel for exactly this (`notifications/progress`,
sent when the client attached a `progressToken` to its request), and the
query layer has natural phase boundaries to report at: the start of a
direct upstream scan, and each tile COPY in a synchronous materialization
loop.

The plumbing problem is that the query layer is deep synchronous code
(DuckDB calls under a lock) while the notification is an async send on
the MCP session. This module bridges the two with a contextvar:

- server.py installs a per-request reporter from middleware around each
  `tools/call` (see server._progress_middleware). The contextvar is
  request-scoped, so concurrent HTTP requests each see their own
  reporter, and it propagates into the worker thread the SDK runs a sync
  tool on (anyio.to_thread carries contextvars).
- query-layer code calls `progress.report(...)` at phase boundaries and
  never knows whether anyone is listening. No reporter (a direct library
  call, a test, a background thread) means the call is a no-op.

Reporting is best-effort by contract: a reporter must never raise into
the query path, and its failures must never fail a query that would
otherwise have answered. Throttling (min interval between sends) lives
here so call sites can report freely without flooding the wire.
"""

import contextvars
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# reporter(message, current, total) -> None. current/total may both be None
# for indeterminate phases ("scanning upstream...").
Reporter = Callable[[str, float | None, float | None], None]

_reporter: contextvars.ContextVar[Reporter | None] = contextvars.ContextVar(
    "placeroot_progress_reporter", default=None
)

# Progress is UX, not telemetry: one message a second is plenty, and a
# tight per-row loop must not turn into a notification flood. Phase
# *transitions* (a new message string) always go out immediately, so the
# first "this will be slow" line is never delayed behind the throttle.
MIN_INTERVAL_S = 1.0
_last_sent = contextvars.ContextVar("placeroot_progress_last", default=(0.0, ""))


def set_reporter(reporter: Reporter | None) -> contextvars.Token:
    """Install reporter for the current context; returns the reset token."""
    return _reporter.set(reporter)


def reset(token: contextvars.Token) -> None:
    _reporter.reset(token)


def report(message: str, current: float | None = None, total: float | None = None) -> None:
    """Report a phase of a slow operation. No-op unless a reporter is installed.

    Never raises: a progress send failing (client gone, queue full) must not
    take down the query it was narrating.
    """
    reporter = _reporter.get()
    if reporter is None:
        return
    now = time.monotonic()
    last_time, last_message = _last_sent.get()
    if message == last_message and now - last_time < MIN_INTERVAL_S:
        return
    _last_sent.set((now, message))
    try:
        reporter(message, current, total)
    except Exception as e:  # noqa: BLE001 - progress must never fail the query
        logger.debug("progress report dropped: %s", e)
