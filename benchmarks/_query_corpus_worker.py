#!/usr/bin/env python3
"""Runs ONE corpus query in a fresh process, then prints a JSON result line.

Invoked by run_query_corpus.py, which owns the cache directory's lifetime.
Not useful on its own: the point of the separate process is that nothing —
no resolved release, no warm DuckDB metadata cache, no built local table —
survives from the previous query.

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
os.environ["PLACEROOT_CACHE_DIR"] = cache_dir

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from query_corpus import QUERIES  # noqa: E402

entry = QUERIES[index]


def _emit(seconds: float, ok: bool, detail: object) -> None:
    print(json.dumps({
        "id": entry["id"], "tool": entry["tool"], "q": entry["question"],
        "s": round(seconds, 1), "ok": bool(ok), "detail": str(detail)[:150],
    }))
    sys.stdout.flush()


def _watchdog() -> None:
    time.sleep(budget_s)
    _emit(budget_s, False, "TIMEOUT (still running at the watchdog budget)")
    os._exit(0)


threading.Thread(target=_watchdog, daemon=True).start()

started = time.perf_counter()
try:
    ok, detail = entry["fn"]()
except Exception as e:  # noqa: BLE001 - any failure is a result, not a crash
    ok, detail = False, f"ERROR {type(e).__name__}: {str(e)[:100]}"
_emit(time.perf_counter() - started, ok, detail)
os._exit(0)
