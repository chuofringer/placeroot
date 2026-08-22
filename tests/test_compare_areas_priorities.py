"""Issue #304: compare_areas' optional weighted `priorities` -> scored verdict.

Uses the same NYC-cluster-vs-Arctic fixture centers as test_compare_areas.py.
Ground truth within radius_m=1000 (via overture._count_places_by_category):
bank nyc=23/arctic=5, gym nyc=22/arctic=0, coffee_shop nyc=23/arctic=0,
novelty_shop nyc=2/arctic=0; density_per_km2 nyc=64.3/arctic=1.59.
"""

import duckdb
import pytest

from placeroot import overture, server

from .conftest import CENTER_LAT, CENTER_LON, FIXTURE_PATH

AREAS = [{"lat": CENTER_LAT, "lon": CENTER_LON}, {"lat": 78.0, "lon": 15.0}]
CENTERS = [(CENTER_LAT, CENTER_LON), (78.0, 15.0)]


def test_no_priorities_leaves_output_unchanged():
    default = server.compare_areas(AREAS, radius_m=1000)
    explicit_none = server.compare_areas(AREAS, radius_m=1000, priorities=None)
    assert default == explicit_none
    assert "verdict" not in default


def test_prefer_more_picks_the_richer_area():
    result = overture.compare_areas(
        CENTERS,
        radius_m=1000,
        priorities=[{"label": "gyms", "category": "gym", "prefer": "more", "weight": 1}],
    )
    verdict = result["verdict"]
    assert verdict["winner_idx"] == 0
    assert verdict["scores"] == [1.0, 0.0]
    assert "area 0=22" in verdict["reasons"][0]
    assert "area 1=0" in verdict["reasons"][0]
    assert "gyms" in verdict["reasons"][0]


def test_prefer_fewer_inverts_the_winner():
    result = overture.compare_areas(
        CENTERS,
        radius_m=1000,
        priorities=[{"label": "competition", "category": "bank", "prefer": "fewer", "weight": 1}],
    )
    verdict = result["verdict"]
    # Arctic (idx 1) has fewer banks (5) than NYC (23), so fewer-is-better
    # flips the winner relative to a raw count comparison.
    assert verdict["winner_idx"] == 1
    # Issue #354: prefer="fewer" normalizes as min/measure, so the best
    # area gets 1.0 (not 1 - min/max) and the other gets min/measure.
    assert verdict["scores"] == [round(5 / 23, 3), 1.0]


def test_prefer_fewer_all_zero_measures_share_full_credit():
    """Every area at 0 on a fewer-is-better priority is fully tied at 1.0
    each — not 1/n — so a criterion nobody fails doesn't drag every
    score down (issue #354)."""
    per_area = [
        {"center": {"lat": 78.0, "lon": 15.0}, "density_per_km2": 1.59, "category_counts": {}},
        {"center": {"lat": 78.0, "lon": 15.0}, "density_per_km2": 1.59, "category_counts": {}},
    ]
    priorities = [{"label": "gyms", "category": "gym", "prefer": "fewer", "weight": 1}]
    verdict = overture._build_verdict(
        [(78.0, 15.0), (78.0, 15.0)], per_area, 1000, priorities
    )
    assert verdict["scores"] == [1.0, 1.0]
    assert verdict["winner_idx"] is None


def test_priority_count_is_exact_not_substring(tmp_path):
    """Issue #354: the verdict count must not ILIKE-substring-match — a
    dataset whose bank rows are actually food_banks must count 0 for a
    "bank" priority, not 23."""
    mutated = tmp_path / "food_banks.parquet"
    con = duckdb.connect()
    con.execute(
        f"""COPY (
            SELECT * REPLACE (
                CASE WHEN basic_category = 'bank' THEN 'food_bank'
                     ELSE basic_category END AS basic_category,
                CASE WHEN taxonomy."primary" = 'bank'
                     THEN struct_pack("primary" := 'food_bank',
                                      alternates := taxonomy.alternates)
                     ELSE taxonomy END AS taxonomy
            ) FROM read_parquet('{FIXTURE_PATH}')
        ) TO '{mutated}' (FORMAT PARQUET)"""
    )
    overture.set_data_path(str(mutated))
    try:
        result = overture.compare_areas(
            CENTERS,
            radius_m=1000,
            priorities=[
                {"label": "banks", "category": "bank", "prefer": "more", "weight": 1}
            ],
        )
        assert "area 0=0" in result["verdict"]["reasons"][0]
        assert "area 1=0" in result["verdict"]["reasons"][0]
    finally:
        overture.set_data_path(None)


def test_priority_count_includes_unnamed_places(tmp_path):
    """Issue #354: verdict counts describe the same population as
    summarize_area's category_counts, which include unnamed rows — so
    stripping every bank's name must not change the bank count."""
    unnamed = tmp_path / "unnamed_banks.parquet"
    con = duckdb.connect()
    con.execute(
        f"""COPY (
            SELECT * REPLACE (
                CASE WHEN basic_category = 'bank'
                     THEN struct_pack("primary" := CAST(NULL AS VARCHAR))
                     ELSE names END AS names
            ) FROM read_parquet('{FIXTURE_PATH}')
        ) TO '{unnamed}' (FORMAT PARQUET)"""
    )
    overture.set_data_path(str(unnamed))
    try:
        result = overture.compare_areas(
            CENTERS,
            radius_m=1000,
            priorities=[
                {"label": "banks", "category": "bank", "prefer": "more", "weight": 1}
            ],
        )
        assert "area 0=23" in result["verdict"]["reasons"][0]
    finally:
        overture.set_data_path(None)


def test_degraded_category_columns_mean_degraded_verdict_not_fabrication(tmp_path):
    """Issue #354: with both category columns gone, a count-based priority
    cannot be measured — the verdict says so explicitly instead of
    silently counting every place in the radius."""
    degraded = tmp_path / "no_category_columns.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * EXCLUDE (basic_category, taxonomy) "
        f"FROM read_parquet('{FIXTURE_PATH}')) TO '{degraded}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(degraded))
    try:
        result = overture.compare_areas(
            CENTERS,
            radius_m=1000,
            priorities=[
                {"label": "banks", "category": "bank", "prefer": "more", "weight": 1}
            ],
        )
        verdict = result["verdict"]
        assert verdict["degraded"] is True
        assert verdict["winner_idx"] is None
        assert verdict["scores"] is None
        assert "degraded" in verdict["measured_note"]
    finally:
        overture.set_data_path(None)


def test_weight_change_flips_the_verdict():
    priorities = [
        {"label": "foot traffic", "category": "__density__", "prefer": "more", "weight": 1},
        {"label": "competition", "category": "bank", "prefer": "fewer", "weight": 1},
    ]
    equal_weight = overture.compare_areas(CENTERS, radius_m=1000, priorities=priorities)
    assert equal_weight["verdict"]["winner_idx"] == 0  # NYC's density edge dominates

    heavier_competition = [
        dict(priorities[0]),
        {**priorities[1], "weight": 5},
    ]
    reweighted = overture.compare_areas(CENTERS, radius_m=1000, priorities=heavier_competition)
    assert reweighted["verdict"]["winner_idx"] == 1  # weighting competition heavier flips it


def test_density_proxy_measure():
    result = overture.compare_areas(
        CENTERS,
        radius_m=1000,
        priorities=[
            {"label": "activity", "category": "__density__", "prefer": "more", "weight": 1}
        ],
    )
    verdict = result["verdict"]
    assert verdict["winner_idx"] == 0
    assert "area 0=64.3" in verdict["reasons"][0]
    assert "area 1=1.59" in verdict["reasons"][0]


def test_tie_gives_no_winner_for_that_priority_and_overall():
    same_center_twice = [(CENTER_LAT, CENTER_LON), (CENTER_LAT, CENTER_LON)]
    result = overture.compare_areas(
        same_center_twice,
        radius_m=1000,
        priorities=[{"label": "banks", "category": "bank", "prefer": "more", "weight": 1}],
    )
    verdict = result["verdict"]
    assert verdict["winner_idx"] is None
    assert verdict["scores"][0] == verdict["scores"][1]
    assert "tied" in verdict["reasons"][0]


def test_requested_category_outside_top_10_alignment_still_counted():
    """The verdict measure comes from an explicit count query, not a
    category_counts dict lookup — so it's correct even when the caller's
    per_area rows (as compare_areas' own top-10 alignment can produce) don't
    carry that category at all.
    """
    per_area = [
        {
            "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
            "density_per_km2": 64.3,
            "category_counts": {},
        },
        {"center": {"lat": 78.0, "lon": 15.0}, "density_per_km2": 1.59, "category_counts": {}},
    ]
    priorities = [{"label": "novelty", "category": "novelty_shop", "prefer": "more", "weight": 1}]
    verdict = overture._build_verdict(CENTERS, per_area, 1000, priorities)
    assert verdict["winner_idx"] == 0
    assert "area 0=2" in verdict["reasons"][0]
    assert "area 1=0" in verdict["reasons"][0]


def test_measured_note_always_present_with_priorities():
    result = overture.compare_areas(
        CENTERS,
        radius_m=1000,
        priorities=[{"label": "banks", "category": "bank", "prefer": "more", "weight": 1}],
    )
    note = result["verdict"]["measured_note"]
    assert "revenue" in note
    assert "foot traffic" in note


def test_reasons_align_with_per_area_measures():
    result = overture.compare_areas(
        CENTERS,
        radius_m=1000,
        priorities=[
            {"label": "gyms", "category": "gym", "prefer": "more", "weight": 1},
            {"label": "competition", "category": "bank", "prefer": "fewer", "weight": 1},
        ],
    )
    reasons = result["verdict"]["reasons"]
    assert len(reasons) == 2
    assert reasons[0].startswith("gyms:")
    assert reasons[1].startswith("competition:")


# -- server-level validation --------------------------------------------


def test_server_bad_request_on_too_many_priorities():
    priorities = [
        {"label": f"p{i}", "category": "bank", "prefer": "more", "weight": 1} for i in range(7)
    ]
    result = server.compare_areas(AREAS, priorities=priorities)
    assert result["error"] == "bad_request"


def test_server_bad_request_on_bad_prefer():
    priorities = [{"label": "banks", "category": "bank", "prefer": "sideways", "weight": 1}]
    result = server.compare_areas(AREAS, priorities=priorities)
    assert result["error"] == "bad_request"


def test_server_bad_request_on_missing_category():
    priorities = [{"label": "banks", "prefer": "more", "weight": 1}]
    result = server.compare_areas(AREAS, priorities=priorities)
    assert result["error"] == "bad_request"


def test_server_bad_request_on_missing_label():
    priorities = [{"category": "bank", "prefer": "more", "weight": 1}]
    result = server.compare_areas(AREAS, priorities=priorities)
    assert result["error"] == "bad_request"


def test_server_bad_request_on_non_numeric_weight():
    priorities = [{"label": "banks", "category": "bank", "prefer": "more", "weight": "high"}]
    result = server.compare_areas(AREAS, priorities=priorities)
    assert result["error"] == "bad_request"


def test_server_weight_is_clamped_not_rejected():
    priorities = [{"label": "banks", "category": "bank", "prefer": "more", "weight": 999}]
    result = server.compare_areas(AREAS, priorities=priorities)
    assert "error" not in result
    assert "verdict" in result


def test_overture_compare_areas_still_rejects_bad_area_count():
    with pytest.raises(ValueError):
        overture.compare_areas([CENTERS[0]], priorities=[])
