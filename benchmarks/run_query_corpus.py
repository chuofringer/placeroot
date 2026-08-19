#!/usr/bin/env python3
"""Run the real-user-question corpus and report latency + correctness.

    uv run python benchmarks/run_query_corpus.py                 # everything, cold
    uv run python benchmarks/run_query_corpus.py --tool geocode  # one family
    uv run python benchmarks/run_query_corpus.py --id r01 --id c02
    uv run python benchmarks/run_query_corpus.py --json out.jsonl

    # Question-level 15s ship gate (#331): 20 ids, cold then warm, all hops.
    uv run python benchmarks/run_query_corpus.py --question-gate --warm --budget-s 15 --fail-on both

    # PR smoke (r01, g10, c01) — same rule, three questions.
    uv run python benchmarks/run_query_corpus.py --smoke --warm --budget-s 15 --fail-on both

Network-dependent by design, and slow (~15 minutes for the full corpus on a
residential connection). Never run from pytest — this is the gate you run
before a release that claims anything about cold-start performance, and after
any change to geocoding, ranking or the bundled artifacts.

Three rules make the numbers mean something, all of them learned by getting
them wrong first:

1. **One fresh process and one empty cache directory per query.** Anything
   else measures a warm path and reports it as cold. `--warm` reuses that
   process and cache for a second run of the *same* id; it does not share
   them across ids.
2. **Sequentially.** These queries are bandwidth-bound; running them
   concurrently makes every number a measurement of the other queries.
3. **The time budget lives inside the process** (a watchdog thread), because
   macOS ships no coreutils `timeout` and a hung remote scan is precisely
   what this exists to catch.

The clock is the whole user question (every hop inside the corpus fn).
Do not sum per-tool times. A fast wrong place fails even at 200ms.

Exit code is 1 if any query fails its correctness check or exceeds
--budget-s, so this can gate a release from CI or a shell. Use
--fail-on wrong for scheduled runs: correctness is deterministic, latency
is not. `--stretch-s` (default 10) is printed, never a fail reason.
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


def _p95(times: list[float]) -> float:
    if not times:
        return 0.0
    ordered = sorted(times)
    # nearest-rank, 0-based
    idx = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def _ensure_warm_leg(rows: list[dict], q: dict) -> list[dict]:
    """If --warm was requested, a missing warm row is a fail, not a skip.

    A worker that prints the cold JSON and then dies would otherwise
    leave the suite with only a passing cold leg.
    """
    if any(r.get("leg") == "warm" for r in rows):
        return rows
    tail = ""
    if rows:
        tail = str(rows[-1].get("detail") or "")[-80:]
    rows.append({
        "id": q["id"], "tool": q["tool"], "q": q["question"],
        "leg": "warm", "s": 0.0, "ok": False,
        "detail": "WARM MISSING (worker died after cold) " + tail,
    })
    return rows


ASK_PEEK_S = 0.5


def _annotate_row(row: dict, budget_s: float, stretch_s: float) -> dict:
    """needs_confirm is ask: not wrong, not a 15s fail.

    Route-only peeks (t01/t02/t04) over ASK_PEEK_S become ASK TOO SLOW.
    from_to/flow asks can include name-resolve time, so they are not
    judged against 500ms — they still must not be a 15s graph extract.
    confirm=true completed hops are not slow (user accepted the ETA).
    """
    detail = str(row.get("detail") or "")
    outcome = row.get("outcome")
    if outcome is None:
        if detail.startswith("ASK ON CONFIRM") or detail.startswith("ASK TOO SLOW"):
            outcome = "wrong"
        elif detail.startswith("ASK"):
            outcome = "ask"
        elif detail.startswith("CONFIRMED"):
            outcome = "confirmed"
        else:
            outcome = "ok" if row.get("ok") else "wrong"
    row["outcome"] = outcome
    if outcome == "ask":
        peek_cap = ASK_PEEK_S if row.get("tool") == "route" else budget_s
        if row["s"] >= peek_cap:
            row["ok"] = False
            row["outcome"] = "wrong"
            row["detail"] = f"ASK TOO SLOW {row['s']:.1f}s {detail}"[:150]
            row["over_budget"] = True
            row["over_stretch"] = False
        else:
            row["ok"] = True
            row["over_budget"] = False
            row["over_stretch"] = False
    elif outcome == "confirmed":
        row["over_budget"] = False
        row["over_stretch"] = row["s"] > stretch_s
    else:
        row["over_budget"] = row["s"] > budget_s
        row["over_stretch"] = row["s"] > stretch_s and not row["over_budget"]
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--tool", action="append", default=[],
                    help="only queries for this tool (repeatable)")
    ap.add_argument("--id", action="append", default=[],
                    help="only these query ids (repeatable)")
    ap.add_argument("--question-gate", action="store_true",
                    help="the 20-id question-level ship subset (#331)")
    ap.add_argument("--smoke", action="store_true",
                    help="3-id PR smoke: r01, g10, c01")
    ap.add_argument("--warm", action="store_true",
                    help="after each cold leg, rerun the same id in the "
                         "same process against the cache it just filled")
    ap.add_argument("--budget-s", type=float, default=None,
                    help="per-leg wall budget; over it is a failure "
                         "(default 15 with --question-gate/--smoke, else 10)")
    ap.add_argument("--stretch-s", type=float, default=10.0,
                    help="stretch target printed as STRETCH; never fails "
                         "the suite (default 10)")
    ap.add_argument("--timeout-s", type=float, default=180.0,
                    help="hard per-leg watchdog (default 180)")
    ap.add_argument("--json", type=Path, help="also write raw results as JSONL")
    ap.add_argument("--fail-on", choices=("wrong", "slow", "both"), default="both",
                    help="what makes the exit code non-zero. Correctness is "
                         "deterministic; latency is not (a residential "
                         "connection swings 2-3x in the evening), so scheduled "
                         "runs should gate on 'wrong' and read the times.")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from query_corpus import (  # noqa: E402
        QUERIES,
        QUESTION_GATE_IDS,
        QUESTION_GATE_SMOKE_IDS,
    )

    if args.budget_s is None:
        args.budget_s = 15.0 if (args.question_gate or args.smoke) else 10.0

    id_filter = list(args.id)
    if args.smoke:
        id_filter = list(QUESTION_GATE_SMOKE_IDS)
    elif args.question_gate and not id_filter:
        id_filter = list(QUESTION_GATE_IDS)

    picked = [
        (i, q) for i, q in enumerate(QUERIES)
        if (not args.tool or q["tool"] in args.tool)
        and (not id_filter or q["id"] in id_filter)
    ]
    if not picked:
        print("no queries matched", file=sys.stderr)
        return 2
    if id_filter:
        # Keep the gate's declared order, not corpus file order.
        order = {qid: n for n, qid in enumerate(id_filter)}
        picked.sort(key=lambda item: order.get(item[1]["id"], len(order)))

    legs = "cold+warm" if args.warm else "cold"
    print(
        f"{len(picked)} questions, {legs} "
        f"(fresh process + empty cache each; warm reuses that process)\n"
        f"budget {args.budget_s:g}s · stretch {args.stretch_s:g}s "
        f"(report only) · fail-on {args.fail_on}\n"
    )
    results = []
    for n, (index, q) in enumerate(picked, start=1):
        cache_dir = Path(tempfile.mkdtemp(prefix="placeroot-corpus-"))
        cmd = [sys.executable, str(WORKER), str(index), str(cache_dir),
               str(args.timeout_s)]
        if args.warm:
            cmd.append("warm")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, cwd=str(ROOT),
            )
            rows = []
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            if not rows:
                rows = [{
                    "id": q["id"], "tool": q["tool"], "q": q["question"],
                    "leg": "cold", "s": 0.0, "ok": False,
                    "detail": f"WORKER CRASH rc={proc.returncode} "
                              f"{proc.stderr.strip()[-120:]}",
                }]
        except OSError as e:  # noqa: PERF203
            rows = [{"id": q["id"], "tool": q["tool"], "q": q["question"],
                     "leg": "cold", "s": 0.0, "ok": False,
                     "detail": f"RUNNER ERROR {e}"}]
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

        if args.warm:
            rows = _ensure_warm_leg(rows, q)

        for row in rows:
            row.setdefault("leg", "cold")
            row = _annotate_row(row, args.budget_s, args.stretch_s)
            results.append(row)
            mark = "ok  " if row["ok"] else "FAIL"
            if row.get("outcome") == "ask":
                mark = "ask "
            flags = ""
            if row["over_budget"]:
                flags += " SLOW"
            elif row["over_stretch"]:
                flags += " STRETCH"
            print(f"[{n:>3}/{len(picked)}] {mark}{flags:<8} {row['s']:>6.1f}s  "
                  f"{row['id']:<5} {row['leg']:<5} "
                  f"{row['q'][:46]:<48} {row['detail'][:40]}")

    if args.json:
        args.json.write_text("".join(json.dumps(r) + "\n" for r in results))

    wrong = [r for r in results if not r["ok"]]
    slow = [r for r in results if r["over_budget"]]
    stretch = [r for r in results if r["over_stretch"] and not r["over_budget"]]
    times = sorted(r["s"] for r in results)
    cold_t = [r["s"] for r in results if r.get("leg") == "cold"]
    warm_t = [r["s"] for r in results if r.get("leg") == "warm"]
    print(f"\n{len(results)} legs · median {times[len(times) // 2]:.1f}s · "
          f"max {times[-1]:.1f}s · p95 { _p95(times):.1f}s · "
          f"{len(slow)} over {args.budget_s:g}s · "
          f"{len(stretch)} stretch (>{args.stretch_s:g}s) · "
          f"{len(wrong)} wrong or empty")
    if cold_t:
        print(f"  cold p95 {_p95(cold_t):.1f}s  (n={len(cold_t)}; "
              f"p95 is reported, not the gate)")
    if warm_t:
        print(f"  warm p95 {_p95(warm_t):.1f}s  (n={len(warm_t)}; "
              f"p95 is reported, not the gate)")
    for r in wrong:
        print(f"  WRONG {r['id']:<5} {r.get('leg','?'):<5} "
              f"{r['q'][:46]:<48} {r['detail'][:56]}")
    for r in slow:
        print(f"  SLOW  {r['id']:<5} {r.get('leg','?'):<5} "
              f"{r['s']:>6.1f}s  {r['q'][:50]}")
    for r in stretch:
        print(f"  STRETCH {r['id']:<5} {r.get('leg','?'):<5} "
              f"{r['s']:>6.1f}s  {r['q'][:50]}")
    failed = (wrong if args.fail_on in ("wrong", "both") else []) + (
        slow if args.fail_on in ("slow", "both") else []
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
