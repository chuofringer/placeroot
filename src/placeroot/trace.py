"""Where a slow call actually spent its time — recorded by the code doing it.

Every latency investigation in this repo has followed the same three steps:
a user reports "that took a minute", someone hand-writes a cold repro
script, and someone reaches for cProfile to learn which phase was slow.
The answer has always been something the server knew while it was
happening — "15.2s of that 21s was a divisions recall scan" — and had no
way to say. This module is that way.

Two kinds of record, both cheap enough to leave on:

- **phases** — a named span with its duration. `with trace.phase("places
  scan", files=4): ...`
- **scans** — one per query issued against a dataset, tagged with whether
  it was *bounded* (carries a bbox or an id predicate, so the reader can
  prune) or unbounded (must read everything it touches). Boundedness is
  the property behind nearly every incident here, so it is recorded as a
  fact rather than inferred from a stopwatch afterwards.

Three consumers, in increasing order of how much they change the loop:

1. `PLACEROOT_TRACE=1` logs the breakdown when a call finishes.
2. server.py attaches it to any response slower than
   `PLACEROOT_TRACE_SLOW_S` (default 10s), so a slow call explains itself
   *in production, to the agent that made it*, with no repro step at all.
3. tests read the records directly and assert invariants — "this call
   issues no unbounded scans" catches the whole family of bugs that
   latency sampling only ever catches one instance of, offline and in
   milliseconds. See tests/test_trace_invariants.py.

Same contextvar discipline as progress.py, and the same contract: this is
observability, so a failure here must never fail a query that would
otherwise have answered. Nothing in this module raises into the caller.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Record:
    """One phase or scan. `kind` is "phase" or "scan"."""

    kind: str
    name: str
    seconds: float
    detail: dict = field(default_factory=dict)


# Request-scoped, like progress.py's reporter: concurrent HTTP requests each
# see their own list, and it propagates into the worker thread the SDK runs a
# sync tool on. None means "nobody is recording" — every call below then
# costs one contextvar read.
_records: contextvars.ContextVar[list[Record] | None] = contextvars.ContextVar(
    "placeroot_trace_records", default=None
)


def start() -> contextvars.Token:
    """Begin recording for the current context; returns the reset token."""
    return _records.set([])


def reset(token: contextvars.Token) -> None:
    _records.reset(token)


def enabled() -> bool:
    return _records.get() is not None


def records() -> list[Record]:
    """Everything recorded so far in this context (empty when not recording)."""
    return list(_records.get() or ())


def record(kind: str, name: str, seconds: float, **detail) -> None:
    """Add one record. A no-op when nothing is recording."""
    current = _records.get()
    if current is None:
        return
    try:
        current.append(Record(kind=kind, name=name, seconds=seconds, detail=detail))
    except Exception:  # noqa: BLE001 - observability must never break a query
        logger.debug("trace record dropped", exc_info=True)


@contextlib.contextmanager
def phase(name: str, **detail):
    """Time a named span. Records even when the body raises — a phase that
    blew up after 30s is exactly the one worth seeing."""
    if _records.get() is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        record("phase", name, time.perf_counter() - started, **detail)


@contextlib.contextmanager
def scan(name: str, *, bounded: bool, source: str | None = None, **detail):
    """Time one query against a dataset.

    `bounded` says whether the query carries a predicate the reader can
    prune on — a bbox, or an id. An unbounded scan reads everything it
    touches, which is fine for a small local table and catastrophic against
    a planet-scale remote theme; recording it as a fact is what lets a test
    assert "this tool issues no unbounded scans" without a network or a
    stopwatch.

    `source` is truncated: a manifest-pruned source is a list of dozens of
    file URLs, and the useful part is which theme it is, not all of it.
    """
    if _records.get() is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        record(
            "scan", name, time.perf_counter() - started,
            bounded=bounded, source=_short_source(source), **detail,
        )


def _short_source(source: str | None) -> str | None:
    """theme=places/type=place out of a glob or a read_parquet([...]) list."""
    if not source:
        return None
    import re

    m = re.search(r"theme=(\w+)/type=(\w+)", source)
    if m:
        n_files = source.count(".parquet")
        return f"{m.group(1)}/{m.group(2)}" + (f" ({n_files} files)" if n_files > 1 else "")
    return source[:60]


def summary() -> list[dict]:
    """Records as plain dicts, slowest first — the shape that goes on the
    wire and into a log line."""
    out = []
    for r in sorted(records(), key=lambda r: -r.seconds):
        row = {"kind": r.kind, "name": r.name, "seconds": round(r.seconds, 2)}
        row.update({k: v for k, v in r.detail.items() if v is not None})
        out.append(row)
    return out


def unbounded_scans() -> list[Record]:
    """Scans that had nothing to prune on. The invariant tests assert this is
    empty for tools that should always bound their reads."""
    return [r for r in records() if r.kind == "scan" and not r.detail.get("bounded")]


def _trace_logging_on() -> bool:
    return os.environ.get("PLACEROOT_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


def slow_threshold_s() -> float:
    """Responses slower than this carry their own breakdown. 0 disables."""
    try:
        return float(os.environ.get("PLACEROOT_TRACE_SLOW_S", "10"))
    except ValueError:
        return 10.0


def log_summary(label: str, total_s: float) -> None:
    """One INFO line per traced call under PLACEROOT_TRACE=1."""
    if not _trace_logging_on():
        return
    rows = summary()
    if not rows:
        return
    parts = "; ".join(
        f"{r['name']} {r['seconds']}s" + (" UNBOUNDED" if r.get("bounded") is False else "")
        for r in rows[:8]
    )
    logger.info("trace %s total=%.1fs :: %s", label, total_s, parts)
