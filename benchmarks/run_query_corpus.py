#!/usr/bin/env python3
"""Run the real-user-question corpus cold and report latency + correctness.

    uv run python benchmarks/run_query_corpus.py                 # everything
    uv run python benchmarks/run_query_corpus.py --tool geocode  # one family
    uv run python benchmarks/run_query_corpus.py --id r01 --id c02
    uv run python benchmarks/run_query_corpus.py --json out.jsonl

Network-dependent by design, and slow (~15 minutes for the full corpus on a
residential connection). Never run from pytest — this is the gate you run
before a release that claims anything about cold-start performance, and after
any change to geocoding, ranking or the bundled artifacts.

Three rules make the numbers mean something, all of them learned by getting
them wrong first:

1. **One fresh process and one empty cache directory per query.** Anything
   else measures a warm path and reports it as cold.
2. **Sequentially.** These queries are bandwidth-bound; running them
   concurrently makes every number a measurement of the other queries.
3. **The time budget lives inside the process** (a watchdog thread), because
   macOS ships no coreutils `timeout` and a hung remote scan is precisely
   what this exists to catch.

Exit code is 1 if any query fails its correctness check or exceeds
--budget-s, so this can gate a release from CI or a shell. Use
--fail-on wrong for scheduled runs: correctness is deterministic, latency
is not.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve().parent / "_query_corpus_worker.py"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--tool", action="append", default=[],
                    help="only queries for this tool (repeatable)")
    ap.add_argument("--id", action="append", default=[],
                    help="only these query ids (repeatable)")
    ap.add_argument("--budget-s", type=float, default=10.0,
                    help="per-query cold budget; over it is a failure (default 10)")
    ap.add_argument("--timeout-s", type=float, default=180.0,
                    help="hard per-query watchdog (default 180)")
    ap.add_argument("--json", type=Path, help="also write raw results as JSONL")
    ap.add_argument("--fail-on", choices=("wrong", "slow", "both"), default="both",
                    help="what makes the exit code non-zero. Correctness is "
                         "deterministic; latency is not (a residential "
                         "connection swings 2-3x in the evening), so scheduled "
                         "runs should gate on 'wrong' and read the times.")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from query_corpus import QUERIES

    picked = [
        (i, q) for i, q in enumerate(QUERIES)
        if (not args.tool or q["tool"] in args.tool)
        and (not args.id or q["id"] in args.id)
    ]
    if not picked:
        print("no queries matched", file=sys.stderr)
        return 2

    print(f"{len(picked)} queries, cold (fresh process + empty cache each)\n")
    results = []
    for n, (index, q) in enumerate(picked, start=1):
        cache_dir = Path(tempfile.mkdtemp(prefix="placeroot-corpus-"))
        try:
            proc = subprocess.run(
                [sys.executable, str(WORKER), str(index), str(cache_dir),
                 str(args.timeout_s)],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            row = json.loads(line) if line.startswith("{") else {
                "id": q["id"], "tool": q["tool"], "q": q["question"], "s": 0.0,
                "ok": False, "detail": f"WORKER CRASH rc={proc.returncode} "
                                       f"{proc.stderr.strip()[-120:]}",
            }
        except (json.JSONDecodeError, OSError) as e:  # noqa: PERF203
            row = {"id": q["id"], "tool": q["tool"], "q": q["question"], "s": 0.0,
                   "ok": False, "detail": f"RUNNER ERROR {e}"}
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

        row["over_budget"] = row["s"] > args.budget_s
        results.append(row)
        mark = "ok  " if row["ok"] else "FAIL"
        slow = " SLOW" if row["over_budget"] else ""
        print(f"[{n:>3}/{len(picked)}] {mark}{slow:<5} {row['s']:>6.1f}s  "
              f"{row['id']:<5} {row['q'][:52]:<54} {row['detail'][:44]}")

    if args.json:
        args.json.write_text("".join(json.dumps(r) + "\n" for r in results))

    wrong = [r for r in results if not r["ok"]]
    slow = [r for r in results if r["over_budget"]]
    times = sorted(r["s"] for r in results)
    print(f"\n{len(results)} queries · median {times[len(times) // 2]:.1f}s · "
          f"max {times[-1]:.1f}s · {len(slow)} over {args.budget_s:g}s · "
          f"{len(wrong)} wrong or empty")
    for r in wrong:
        print(f"  WRONG {r['id']:<5} {r['q'][:50]:<52} {r['detail'][:60]}")
    for r in slow:
        print(f"  SLOW  {r['id']:<5} {r['s']:>6.1f}s  {r['q'][:50]}")
    failed = (wrong if args.fail_on in ("wrong", "both") else []) + (
        slow if args.fail_on in ("slow", "both") else []
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
