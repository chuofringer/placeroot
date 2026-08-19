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
  never knows whether anyone is listening. The message is always collected
  on a request-scoped log (the host-agnostic path: many clients never send
  a progressToken). An MCP reporter, when installed, also gets the send.
  No log and no reporter (a bare library call that never called begin)
  still no-ops the wire send.

Reporting is best-effort by contract: a reporter must never raise into
the query path, and its failures must never fail a query that would
otherwise have answered. Throttling (min interval between sends) lives
here so call sites can report freely without flooding the wire.
"""

import contextvars
import logging
import math
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

# Host-agnostic log: report() always appends here so attach() can put the
# same line on the JSON answer. Default is an empty tuple (immutable) so
# a forgotten begin() cannot leak a shared list across requests; begin()
# / clear() install a fresh list. Lazy-init in report() covers direct
# library calls and tests that never go through middleware.
_log: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "placeroot_progress_log", default=None
)
_started: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "placeroot_progress_started", default=None
)

# Don't stamp status onto a 50ms find. Attach if we reported a phase, or
# the call clearly lingered (graph / warmup / a cold scan).
SLOW_ATTACH_S = 2.0


def set_reporter(reporter: Reporter | None) -> contextvars.Token:
    """Install reporter for the current context; returns the reset token."""
    return _reporter.set(reporter)


def reset(token: contextvars.Token) -> None:
    _reporter.reset(token)


def reset_log(token: contextvars.Token) -> None:
    _log.reset(token)


# Honest first-touch ranges (seconds), from measured comments in cache.py /
# routing.py / geocode.py. These are ranges, not a countdown: a client that
# sees "about 8–15 seconds" should not treat the upper bound as a deadline.
# Heavy themes' *direct scans* are the Tokyo-class minute-plus path; a
# tiled first-touch is the seconds-scale COPY those comments contrast it
# against.
_TILE_COPY_S: dict[str, tuple[float, float]] = {
    "places": (3.0, 8.0),
    "buildings": (6.0, 20.0),
    "transportation": (6.0, 20.0),
    "base_water": (2.0, 6.0),
    "base_infrastructure": (2.0, 6.0),
}
_DEFAULT_TILE_S = (3.0, 10.0)
_DIRECT_SCAN_S: dict[str, tuple[float, float]] = {
    "places": (4.0, 12.0),
    "buildings": (45.0, 180.0),
    "transportation": (45.0, 180.0),
    "base_water": (8.0, 45.0),
    "base_infrastructure": (8.0, 45.0),
}
_DEFAULT_SCAN_S = (5.0, 20.0)
DIVISIONS_INDEX_S = (20.0, 40.0)
GRAPH_BUILD_S = (5.0, 25.0)


def format_eta(lo: float, hi: float) -> str:
    """Human ETA: 'about 8 seconds' or 'about 8–15 seconds', never a fake countdown.

    Ranges at or above a minute are spoken in minutes so a 45–180s buildings
    scan is "about 1–3 minutes" rather than a three-digit second count.
    """
    a, b = sorted((max(0.0, float(lo)), max(0.0, float(hi))))
    if b <= 0:
        return "about a second"
    if b >= 60:
        lo_m = max(1, int(round(a / 60.0))) if a > 0 else 1
        hi_m = max(lo_m, int(round(b / 60.0)))
        if lo_m == hi_m:
            return "about 1 minute" if lo_m == 1 else f"about {lo_m} minutes"
        return f"about {lo_m}–{hi_m} minutes"
    a_i = max(1, int(round(a))) if a > 0 else 1
    b_i = max(a_i, int(round(b)))
    if a_i == b_i:
        return "about 1 second" if a_i == 1 else f"about {a_i} seconds"
    return f"about {a_i}–{b_i} seconds"


def tile_eta(theme: str, n_missing: int, workers: int = 2) -> tuple[float, float]:
    """Estimated seconds to COPY n_missing tiles (2 workers when n>1)."""
    if n_missing <= 0:
        return (0.0, 0.0)
    lo, hi = _TILE_COPY_S.get(theme, _DEFAULT_TILE_S)
    waves = math.ceil(n_missing / max(1, workers)) if n_missing > 1 else n_missing
    return lo * waves, hi * waves


def scan_eta(theme: str) -> tuple[float, float]:
    """Estimated seconds for a cold direct upstream scan of theme."""
    return _DIRECT_SCAN_S.get(theme, _DEFAULT_SCAN_S)


def begin() -> contextvars.Token:
    """Start a request-scoped progress log. Returns the reset token for the log."""
    _started.set(time.monotonic())
    _last_sent.set((0.0, ""))
    return _log.set([])


def clear() -> None:
    """Drop the request log. Tests call this so phases do not leak."""
    _log.set([])
    _started.set(time.monotonic())
    _last_sent.set((0.0, ""))


def _ensure_log() -> list[str]:
    log = _log.get()
    if log is None:
        log = []
        _log.set(log)
        if _started.get() is None:
            _started.set(time.monotonic())
    return log


def report(
    message: str,
    current: float | None = None,
    total: float | None = None,
    *,
    eta_s: tuple[float, float] | None = None,
) -> None:
    """Report a phase of a slow operation.

    Always collected on the request-scoped log (even with no MCP reporter)
    so attach() can put the same line on the JSON answer. The MCP send is
    still a no-op unless a reporter is installed.

    Never raises: a progress send failing (client gone, queue full) must not
    take down the query it was narrating.

    eta_s is an optional (lo, hi) second range appended as an honest ETA
    (" — about 8–15 seconds"). Call sites that already have a number of
    tiles or a known phase use tile_eta / scan_eta; others omit it.
    """
    if eta_s is not None:
        message = f"{message} — {format_eta(*eta_s)}"
    log = _ensure_log()
    if not log or log[-1] != message:
        log.append(message)
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


def attach(result, extra: list[str] | str | None = None):
    """Copy this request's progress log onto a JSON answer.

    Adds `status` (the latest line) and `progress` (the phase list) so an
    agent can relay them without a progressToken. No-op on a fast call that
    never reported a phase — a 50ms find stays a 50ms find. `extra` appends
    one more line (or a short list) before the copy.
    """
    if not isinstance(result, dict):
        return result
    log = list(_log.get() or [])
    if extra:
        extras = extra if isinstance(extra, (list, tuple)) else [extra]
        for item in extras:
            if item and item not in log:
                log.append(str(item))
    started = _started.get()
    elapsed = (time.monotonic() - started) if started is not None else 0.0
    if not log and elapsed < SLOW_ATTACH_S:
        return result
    if log:
        # warmup_city (and a few others) already use status for their own
        # state machine. Do not clobber it; the phase list is enough for
        # an agent to relay, and the last line still lands on status when
        # the key is free (route, a cold scan).
        if "status" not in result:
            result["status"] = log[-1]
        result["progress"] = log
    elif elapsed >= SLOW_ATTACH_S and "status" not in result:
        result["status"] = f"Took about {max(1, int(round(elapsed)))} seconds"
    return result
