"""Offline exercise of benchmarks/token_benchmark.py's harness (issue #26).

benchmarks/ isn't part of the installed package (it's a standalone script,
same as scripts/geocode_benchmark.py), so it's loaded here by file path
rather than imported as `placeroot.*`. This doesn't re-run the real 30-task
live benchmark (that needs network and live Overture data, and is run
separately — see benchmarks/README.md) — it proves the checker/measurement
machinery itself (Outcome, run_task, the raw-payload comparator helpers)
works correctly against the same committed fixtures every other offline
test uses.
"""

import importlib.util
import sys
from pathlib import Path

from .conftest import CENTER_LAT, CENTER_LON

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "token_benchmark", REPO_ROOT / "benchmarks" / "token_benchmark.py"
)
token_benchmark = importlib.util.module_from_spec(_SPEC)
# dataclass field-type resolution looks the module up in sys.modules by
# __module__ name, so it must be registered before exec_module runs.
sys.modules["token_benchmark"] = token_benchmark
_SPEC.loader.exec_module(token_benchmark)


def test_within_distance_offline_fixture_task_passes():
    task = token_benchmark._within_distance_task(
        "offline_within", CENTER_LAT, CENTER_LON, max_distance_m=1000, category=None, expected=True
    )
    run = token_benchmark.run_task(task)
    assert run.outcome.error is None
    assert run.outcome.correct is True
    assert run.outcome.placeroot_tokens > 0
    assert run.outcome.raw_tokens > 0
    assert run.outcome.ratio is not None


def test_point_in_admin_offline_fixture_task_passes():
    # tests/fixtures/division_areas.parquet nests "Downtown" as the smallest
    # division containing CENTER_LAT/CENTER_LON (see test_admin_lookup.py).
    task = token_benchmark._point_in_admin_task(
        "offline_admin", CENTER_LAT, CENTER_LON, expected=["Downtown"]
    )
    run = token_benchmark.run_task(task)
    assert run.outcome.error is None
    assert run.outcome.correct is True
    assert run.outcome.placeroot_tokens > 0
    assert run.outcome.raw_tokens > 0


def test_nearest_poi_offline_fixture_task_records_a_real_miss_honestly():
    """The fixture's coffee_shop places (scripts/build_fixture.py's cluster,
    including "Blue Bottle Roastery") never include "Starbucks" — this is a
    real, expected checker failure, exercising the "failing tasks are
    counted, not dropped" path with an actual miss rather than a synthetic
    always-pass stand-in."""
    task = token_benchmark._nearest_poi_top3_task(
        "offline_coffee", CENTER_LAT, CENTER_LON,
        category="coffee_shop", expected_names=["Starbucks"],
    )
    run = token_benchmark.run_task(task)
    assert run.outcome.error is None
    assert run.outcome.correct is False
    assert "Starbucks" not in run.outcome.detail.split("got ")[1]
    assert run.outcome.placeroot_tokens > 0


def test_summarize_counts_failures_without_dropping_them():
    runs = [
        token_benchmark.run_task(
            token_benchmark._within_distance_task(
                "pass", CENTER_LAT, CENTER_LON, max_distance_m=1000, category=None, expected=True
            )
        ),
        token_benchmark.run_task(
            token_benchmark._nearest_poi_top3_task(
                "fail", CENTER_LAT, CENTER_LON, category="coffee_shop", expected_names=["Starbucks"]
            )
        ),
    ]
    summary = token_benchmark.summarize(runs)
    assert summary["n_tasks"] == 2
    assert summary["n_correct"] == 1
    assert summary["accuracy"] == 0.5
    report = token_benchmark.render_report(runs, "test-release")
    assert "fail" in report  # the failing task's name appears in the failure detail
    assert "1/2" in report
