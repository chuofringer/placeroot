"""Offline guards for scripts/overture_canary.py (#219).

The canary itself is network-dependent and runs weekly in CI, so nothing here
probes a bucket. What these cover is the part that can rot silently between
those runs: the requirement table pointing at column lists the runtime no
longer uses, or at a dataset the server reads but the canary doesn't watch.
A canary watching the wrong thing is worse than none — it reports "clean".
"""

import importlib.util
from pathlib import Path

import pytest

from placeroot import addresses, divisions, land_use, overture

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't a package — load the module by path, the same way
# test_bump_version.py does.
_spec = importlib.util.spec_from_file_location(
    "overture_canary", REPO_ROOT / "scripts" / "overture_canary.py"
)
overture_canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(overture_canary)


def test_requirements_reference_the_runtime_lists_not_copies():
    """Identity, not equality: a restated literal would drift the moment a
    theme module gained or dropped a column."""
    by_dataset = {(t, ty): req for t, ty, req in overture_canary.THEME_REQUIREMENTS}
    assert by_dataset[("places", "place")] is overture.REQUIRED_COLUMNS
    assert by_dataset[("divisions", "division_area")] is divisions.REQUIRED_COLUMNS
    assert by_dataset[("divisions", "division")] is addresses.DIVISION_REQUIRED_COLUMNS
    assert by_dataset[("addresses", "address")] is addresses.REQUIRED_COLUMNS
    assert by_dataset[("base", "land_cover")] is land_use.LAND_COVER_REQUIRED_COLUMNS


def test_every_dataset_the_server_reads_is_watched_exactly_once():
    datasets = [(theme, type_) for theme, type_, _ in overture_canary.THEME_REQUIREMENTS]
    # A duplicate is a second network DESCRIBE of the same dataset for no signal.
    assert len(datasets) == len(set(datasets)), "a dataset is probed twice"
    assert set(datasets) == {
        ("places", "place"),
        ("divisions", "division_area"),
        ("divisions", "division"),
        ("addresses", "address"),
        ("buildings", "building"),
        ("base", "land_use"),
        ("base", "land_cover"),
        ("base", "infrastructure"),
        ("base", "water"),
        ("transportation", "segment"),
    }


def test_required_lists_name_top_level_columns_only():
    """probe_columns compares against DESCRIBE's top-level names, so a nested
    path like "bbox.xmin" in one of these lists would report as missing
    forever. Struct fields are reached through their top-level column."""
    for theme, type_, required in overture_canary.THEME_REQUIREMENTS:
        for column in required:
            assert "." not in column, f"{theme}/{type_}: {column!r} is not a top-level column"


def test_land_cover_is_not_watched_for_columns_it_never_had():
    """land_cover carries neither class nor names upstream; watching it with
    land_use's list would open an issue every week for a by-design absence."""
    assert "class" not in land_use.LAND_COVER_REQUIRED_COLUMNS
    assert "names" not in land_use.LAND_COVER_REQUIRED_COLUMNS
    assert set(land_use.LAND_COVER_REQUIRED_COLUMNS) <= set(land_use.REQUIRED_COLUMNS)


# --- #416 field-coverage gate: compare_bbox_metrics / render_coverage_report,
# offline with synthetic numbers. probe_bbox_metrics() itself is network-
# dependent and, per this script's own rule, never exercised here.


def _metrics(places_rows, brand_rate, confidence_rate, category_rate, addresses_rows):
    return {
        "places_rows": float(places_rows),
        "brand_non_null_rate": brand_rate,
        "confidence_non_null_rate": confidence_rate,
        "category_non_null_rate": category_rate,
        "addresses_rows": float(addresses_rows),
    }


def test_probe_set_is_three_to_five_bboxes_with_comments():
    assert 3 <= len(overture_canary.PROBE_METROS) <= 5
    names = [name for name, _, _ in overture_canary.PROBE_METROS]
    assert len(names) == len(set(names)), "duplicate probe bbox name"


def test_healthy_release_flags_nothing():
    pinned = {"Paris": _metrics(1000, 0.5, 0.9, 0.95, 2000)}
    # Small, ordinary jitter — well within REGRESSION_TOLERANCE.
    newest = {"Paris": _metrics(1010, 0.49, 0.91, 0.94, 1990)}
    rows = overture_canary.compare_bbox_metrics(pinned, newest)
    assert not any(r["regression"] for r in rows)
    assert overture_canary.render_coverage_report(rows, "2026-08-19.0", "2026-09-16.0") == []


def test_regression_detected_with_correct_table_numbers():
    """A synthetic brand-collapse (#546's shape): places/confidence/category
    hold, brand's non-null rate craters — the exact metric #546 was about."""
    pinned = {"Sao Paulo": _metrics(10_000, 0.60, 0.90, 0.95, 5_000)}
    newest = {"Sao Paulo": _metrics(10_000, 0.40, 0.90, 0.95, 5_000)}  # brand -33%
    rows = overture_canary.compare_bbox_metrics(pinned, newest)
    flagged = [r for r in rows if r["regression"]]
    assert len(flagged) == 1
    row = flagged[0]
    assert row["bbox"] == "Sao Paulo"
    assert row["metric"] == "brand_non_null_rate"
    assert row["pinned"] == 0.60
    assert row["newest"] == 0.40
    assert row["delta_pct"] == pytest.approx((0.40 - 0.60) / 0.60 * 100.0)
    assert row["delta_pct"] < -overture_canary.REGRESSION_TOLERANCE * 100.0

    report = overture_canary.render_coverage_report(rows, "2026-08-19.0", "2026-09-16.0")
    assert report, "regression must produce a non-empty report"
    joined = "\n".join(report)
    assert "Sao Paulo" in joined
    assert "brand non-null rate" in joined
    assert "0.6" in joined and "0.4" in joined
    assert "-33.3%" in joined
    assert "OvertureMaps/data#546" in joined


def test_row_count_metric_regression_also_flagged():
    pinned = {"Tokyo": _metrics(5_000, 0.5, 0.9, 0.95, 3_000)}
    newest = {"Tokyo": _metrics(3_000, 0.5, 0.9, 0.95, 3_000)}  # places rows -40%
    rows = overture_canary.compare_bbox_metrics(pinned, newest)
    flagged = {r["metric"] for r in rows if r["regression"]}
    assert "places_rows" in flagged


def test_small_denominator_is_skipped_not_flagged():
    """MIN_ROWS floors row-count-backed metrics: a tiny pinned count makes a
    percentage meaningless (one missing row is a "100% drop")."""
    tiny = overture_canary.MIN_ROWS - 1
    pinned = {"Tiny": _metrics(tiny, 0.5, 0.9, 0.95, 3_000)}
    newest = {"Tiny": _metrics(0, 0.0, 0.0, 0.0, 3_000)}
    rows = overture_canary.compare_bbox_metrics(pinned, newest)
    by_metric = {r["metric"]: r for r in rows}
    for metric in ("places_rows", "brand_non_null_rate", "confidence_non_null_rate"):
        assert by_metric[metric]["regression"] is False
        assert by_metric[metric]["skip_reason"] is not None
        assert by_metric[metric]["delta_pct"] is None
    # addresses_rows' own denominator (3_000) clears the floor, so it is
    # still compared even though places_rows didn't.
    assert by_metric["addresses_rows"]["skip_reason"] is None
    assert overture_canary.render_coverage_report(rows, "2026-08-19.0", "2026-09-16.0") == []


def test_pinned_zero_denominator_is_skipped():
    pinned = {"Empty": _metrics(0, 0.0, 0.0, 0.0, 0)}
    newest = {"Empty": _metrics(0, 0.0, 0.0, 0.0, 0)}
    rows = overture_canary.compare_bbox_metrics(pinned, newest)
    assert all(r["skip_reason"] is not None for r in rows)
    assert all(r["regression"] is False for r in rows)


def test_bbox_missing_from_one_side_is_not_compared():
    pinned = {"A": _metrics(1000, 0.5, 0.9, 0.95, 500), "B": _metrics(1000, 0.5, 0.9, 0.95, 500)}
    newest = {"A": _metrics(1000, 0.5, 0.9, 0.95, 500)}  # B's probe failed upstream
    rows = overture_canary.compare_bbox_metrics(pinned, newest)
    assert {r["bbox"] for r in rows} == {"A"}
