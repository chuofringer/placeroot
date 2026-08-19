"""Offline guards for the #331 question-level 15s subset.

Never invoke run_query_corpus.py against live S3 from pytest — that is
the nightly / manual gate. These tests only check the id list, the
argparse surface, and that p95 is reported rather than used as a fail.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "benchmarks"


def _corpus_ids_from_source() -> set[str]:
    """Parse q("id", ...) calls without importing placeroot / DuckDB."""
    tree = ast.parse((BENCH / "query_corpus.py").read_text())
    ids = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "q"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            ids.append(node.args[0].value)
    return set(ids)


def test_question_gate_ids_are_twenty_real_corpus_rows():
    # Import only the constants — executing query_corpus.py as a module
    # would register 148 closures that import placeroot on first call,
    # which is fine, but q() also fills QUERIES. That's local and cheap.
    sys.path.insert(0, str(BENCH))
    import query_corpus  # noqa: E402

    ids = list(query_corpus.QUESTION_GATE_IDS)
    assert len(ids) == 20
    assert len(set(ids)) == 20
    registered = {q["id"] for q in query_corpus.QUERIES}
    missing = set(ids) - registered
    assert not missing, f"gate ids missing from QUERIES: {sorted(missing)}"
    smoke = query_corpus.QUESTION_GATE_SMOKE_IDS
    assert smoke == ("r01", "g10", "c01")
    assert set(smoke) <= set(ids)


def test_question_gate_covers_the_required_families():
    sys.path.insert(0, str(BENCH))
    import query_corpus  # noqa: E402

    ids = query_corpus.QUESTION_GATE_IDS
    prefixes = {i[0] for i in ids}
    for needed in ("r", "g", "f", "s", "t", "c", "x"):
        assert needed in prefixes, f"gate is missing family {needed}*"
    assert "f13" in ids and "f14" in ids, "sparse/remote find ids"
    assert "c15" in ids, "deepest composite (2x resolve + route)"
    assert "g10" in ids, "Casablanca Chile-trap must stay"


def test_source_q_calls_include_every_gate_id():
    # Belt: even if QUERIES failed to append, the file still names them.
    named = _corpus_ids_from_source()
    sys.path.insert(0, str(BENCH))
    import query_corpus  # noqa: E402

    assert set(query_corpus.QUESTION_GATE_IDS) <= named


def test_runner_help_exposes_gate_flags():
    proc = subprocess.run(
        [sys.executable, str(BENCH / "run_query_corpus.py"), "--help"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=True,
    )
    help_text = proc.stdout
    for flag in (
        "--question-gate",
        "--smoke",
        "--warm",
        "--budget-s",
        "--stretch-s",
        "--fail-on",
    ):
        assert flag in help_text


def test_p95_is_nearest_rank_not_a_pass():
    spec = importlib.util.spec_from_file_location(
        "run_query_corpus", BENCH / "run_query_corpus.py"
    )
    # Importing the module does not run main. No network.
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._p95([]) == 0.0
    assert mod._p95([1.0]) == 1.0
    # 20 samples: 95th nearest-rank is toward the top, not the mean.
    twenty = [float(i) for i in range(20)]
    assert mod._p95(twenty) >= 18.0
