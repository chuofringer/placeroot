#!/usr/bin/env python3
"""Runs ONE corpus query in a fresh process, then prints a JSON result line.

Invoked by run_query_corpus.py, which owns the cache directory's lifetime.
Not useful on its own: the point of the separate process is that nothing —
no resolved release, no warm DuckDB metadata cache, no built local table —
survives from the previous query.

Pass a fourth argument `warm` to run the same fn a second time in this
process against the cache the cold leg just filled (#331). Each leg is
its own wall clock and its own watchdog; a 14s warm is a hang.

The time budget is a watchdog thread rather than an external `timeout`,
because macOS ships no coreutils `timeout` and a query that hangs is exactly
what the budget is for.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

index, cache_dir, budget_s = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
want_warm = len(sys.argv) > 4 and sys.argv[4] == "warm"
os.environ["PLACEROOT_CACHE_DIR"] = cache_dir

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from query_corpus import QUERIES  # noqa: E402

entry = QUERIES[index]
# Generation token so a finished leg's watchdog cannot kill the next one.
_watch_gen = 0


def _classify(ok: bool, detail: object) -> tuple[bool, str, str]:
    """Map corpus detail prefixes to an outcome. ask is not wrong."""
    text = str(detail)
    if text.startswith("ASK ON CONFIRM") or text.startswith("ASK TOO SLOW"):
        return False, "wrong", text
    if text.startswith("ASK"):
        return True, "ask", text
    if text.startswith("CONFIRMED"):
        return bool(ok), "confirmed", text
    return bool(ok), ("ok" if ok else "wrong"), text


def _emit(seconds: float, ok: bool, detail: object, *, leg: str) -> None:
    ok, outcome, text = _classify(ok, detail)
    print(json.dumps({
        "id": entry["id"], "tool": entry["tool"], "q": entry["question"],
        "leg": leg, "s": round(seconds, 1), "ok": bool(ok),
        "outcome": outcome, "detail": text[:150],
    }))
    sys.stdout.flush()


def _arm_watchdog(leg: str) -> int:
    global _watch_gen
    _watch_gen += 1
    mine = _watch_gen

    def _watchdog() -> None:
        time.sleep(budget_s)
        if mine != _watch_gen:
            return
        _emit(budget_s, False, "TIMEOUT (still running at the watchdog budget)",
              leg=leg)
        os._exit(0)

    threading.Thread(target=_watchdog, daemon=True).start()
    return mine


def _run_leg(leg: str) -> bool:
    _arm_watchdog(leg)
    started = time.perf_counter()
    try:
        ok, detail = entry["fn"]()
    except Exception as e:  # noqa: BLE001 - any failure is a result, not a crash
        ok, detail = False, f"ERROR {type(e).__name__}: {str(e)[:100]}"
    _emit(time.perf_counter() - started, ok, detail, leg=leg)
    return bool(ok)


_run_leg("cold")
if want_warm:
    # Disarm the cold watchdog before the warm clock starts.
    _watch_gen += 1
    _run_leg("warm")
os._exit(0)
