"""Confirm and ETA gating for the slow tools.

A cold street-graph build or a warmup needs confirm=true before it starts, and a
confirmed graph build is capped at twice its advertised ETA.

Moved verbatim out of server.py (no behaviour change). server.py re-exports every name.
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor

from placeroot import (
    progress,
    routing,
)


def _confirm_graph_cap_s() -> float:
    """2x the advertised graph-build upper bound. Warm cache hits stay uncapped."""
    return 2.0 * float(progress.GRAPH_BUILD_S[1])


def _eta_exceeded_graph() -> dict:
    lo, hi = progress.GRAPH_BUILD_S
    return {
        "error": "eta_exceeded",
        "eta": progress.format_eta(lo, hi),
        "eta_s": [int(lo), int(hi)],
        "limit_s": int(_confirm_graph_cap_s()),
        "detail": (
            "The street-graph build exceeded twice the advertised wait "
            f"({progress.format_eta(lo, hi)}). Try a smaller area or a warm cache."
        ),
    }


def _run_route(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    *,
    mode: str,
    include_path: bool,
    include_elevation: bool = False,
    prefer: str | None = None,
    avoid: tuple[str, ...] = (),
    cap_confirm_build: bool,
) -> dict:
    """routing.route, with a 2x-ETA cap on a confirmed cold graph build."""
    if not cap_confirm_build:
        return routing.route(
            from_lat,
            from_lon,
            to_lat,
            to_lon,
            mode=mode,
            include_path=include_path,
            include_elevation=include_elevation,
            prefer=prefer,
            avoid=avoid,
        )
    limit_s = _confirm_graph_cap_s()
    # Install a log list before copy so worker report() appends are visible
    # to attach() on this thread (copy_context snapshots the list reference).
    progress._ensure_log()
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(
            ctx.run,
            routing.route,
            from_lat,
            from_lon,
            to_lat,
            to_lon,
            mode=mode,
            include_path=include_path,
            include_elevation=include_elevation,
            prefer=prefer,
            avoid=avoid,
        )
        try:
            return fut.result(timeout=limit_s)
        except TimeoutError:
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())
            return _eta_exceeded_graph()
    finally:
        # Do not join the worker — that would turn the cap back into a hang.
        pool.shutdown(wait=False, cancel_futures=True)


def _needs_confirm_graph(mode: str) -> dict:
    """Cheap reject before a cold street-graph extract (#336)."""
    lo, hi = progress.GRAPH_BUILD_S
    return {
        "error": "needs_confirm",
        "eta": progress.format_eta(lo, hi),
        "eta_s": [int(lo), int(hi)],
        "detail": (
            f"First {mode} in this city builds the street graph. "
            "Ask the user if they want to wait, then call the same tool "
            "again with confirm=true."
        ),
    }


def _needs_confirm_warmup() -> dict:
    lo, hi = progress.WARMUP_S
    return {
        "error": "needs_confirm",
        "eta": progress.format_eta(lo, hi),
        "eta_s": [int(lo), int(hi)],
        "detail": (
            "First warmup in this city copies map tiles into the local cache. "
            "Ask the user if they want to wait, then call the same tool "
            "again with confirm=true."
        ),
    }
