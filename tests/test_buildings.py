"""Issue #23: building footprints, against the synthetic buildings fixture
(tests/fixtures/buildings.parquet, built by scripts/build_fixture.py) — an
8x10 grid of 80 rectangular footprints around places.parquet's downtown
cluster. Ground-truth areas/counts below are computed straight from the
generator's own width_m/depth_m/stride constants (no shapely, no
independent geometry library) — see scripts/build_fixture.py's "Buildings
fixture" section for why that's exact rather than approximate.
"""

import duckdb
import pytest

from placeroot import budget, buildings, overture, server

from .conftest import BUILDINGS_FIXTURE_PATH, CENTER_LAT, CENTER_LON

# Mirrors scripts/build_fixture.py's BUILDING_* constants — kept in sync by
# hand (there are only a handful) rather than imported, so a test failure
# here is a real regression signal, not a tautology against the same code.
_WIDTHS_M = [6.0, 9.0, 12.0, 15.0, 18.0]
_DEPTHS_M = [8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
_N = 80
_SUBTYPE_CYCLE = ["residential", "commercial", "industrial", None]
_HEIGHT_STRIDE = 3

_EXPECTED_AREAS_M2 = [_WIDTHS_M[i % 5] * _DEPTHS_M[i % 7] for i in range(_N)]
_EXPECTED_TOTAL_AREA_M2 = sum(_EXPECTED_AREAS_M2)
_EXPECTED_MEAN_AREA_M2 = _EXPECTED_TOTAL_AREA_M2 / _N
_EXPECTED_HEIGHT_KNOWN = sum(1 for i in range(_N) if i % _HEIGHT_STRIDE == 0)
_EXPECTED_SUBTYPE_COUNTS = {
    s: sum(1 for i in range(_N) if _SUBTYPE_CYCLE[i % 4] == s) for s in ["residential",
    "commercial", "industrial"]
}

# A radius comfortably covering the whole 8x10 grid (pitch 25m, so corner-to-
# corner is well under 250m even accounting for footprint extents).
FULL_GRID_RADIUS_M = 300


# --- summarize_buildings -----------------------------------------------

def test_count_matches_fixture_ground_truth():
    result = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M)
    assert result["count"] == _N


def test_total_and_mean_area_match_generator_dimensions():
    result = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M)
    assert result["total_footprint_area_m2"] == pytest.approx(_EXPECTED_TOTAL_AREA_M2, rel=1e-3)
    assert result["mean_footprint_area_m2"] == pytest.approx(_EXPECTED_MEAN_AREA_M2, rel=1e-3)


def test_height_coverage_matches_the_sparse_third():
    result = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M)
    expected_pct = 100 * _EXPECTED_HEIGHT_KNOWN / _N
    assert result["height_known_pct"] == pytest.approx(expected_pct, abs=0.1)
    assert result["num_floors_known_pct"] == pytest.approx(expected_pct, abs=0.1)
    assert "mean_height_m" in result
    assert "mean_num_floors" in result


def test_subtype_breakdown_matches_the_even_cycle():
    result = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M)
    counts = {row["subtype"]: row["count"] for row in result["top_subtypes"]}
    assert counts == _EXPECTED_SUBTYPE_COUNTS
    assert result["uncategorized_subtype_count"] == _EXPECTED_SUBTYPE_COUNTS["residential"]
    class_counts = {row["class"]: row["count"] for row in result["top_classes"]}
    assert class_counts == {"house": 20, "retail": 20, "warehouse": 20}


def test_small_radius_returns_a_strict_subset():
    full = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M)
    partial = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=20)
    assert 0 < partial["count"] < full["count"]


# --- buildings_at --------------------------------------------------------

def test_results_are_nearest_first():
    rows = buildings.buildings_at(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M, limit=25)
    distances = [r["distance_m"] for r in rows]
    assert distances == sorted(distances)


def test_limit_is_respected_and_capped_at_max_rows():
    rows = buildings.buildings_at(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M, limit=3)
    assert len(rows) == 3
    rows = buildings.buildings_at(
        CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M, limit=10_000
    )
    assert len(rows) <= buildings.MAX_ROWS


def test_row_shape_has_no_geometry_by_default():
    rows = buildings.buildings_at(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M, limit=5)
    for r in rows:
        assert "geometry" not in r
        assert set(r) == {
            "id", "subtype", "class", "footprint_area_m2", "height_m",
            "num_floors", "distance_m",
        }


def test_nearest_footprint_area_matches_a_generator_dimension():
    rows = buildings.buildings_at(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M, limit=1)
    assert rows[0]["footprint_area_m2"] in [pytest.approx(a, rel=1e-3) for a in _EXPECTED_AREAS_M2]


def test_include_geometry_returns_simplified_geojson_under_per_row_cap():
    rows = buildings.buildings_at(
        CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M, limit=5, include_geometry=True
    )
    assert rows
    for r in rows:
        assert r["geometry"]["type"] == "Polygon"
        tokens = budget.estimate_tokens({"geometry": r["geometry"]})
        assert tokens <= buildings.PER_ROW_GEOMETRY_TOKEN_CAP
        assert "geometry_max_deviation_m" in r


# --- degraded columns ------------------------------------------------------

def test_height_missing_entirely_is_omitted_not_zero(tmp_path):
    out = tmp_path / "no_height.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (height) FROM read_parquet("
        f"'{BUILDINGS_FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    buildings.set_data_path(str(out))
    try:
        result = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M)
        assert "height_known_pct" not in result
        assert "mean_height_m" not in result
        # num_floors is untouched — still reported normally.
        assert "num_floors_known_pct" in result
        assert "height" in buildings.degraded_fields()
    finally:
        buildings.set_data_path(str(BUILDINGS_FIXTURE_PATH))


def test_missing_geometry_raises_schema_degraded(tmp_path):
    out = tmp_path / "no_geometry.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (geometry) FROM read_parquet("
        f"'{BUILDINGS_FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    buildings.set_data_path(str(out))
    try:
        with pytest.raises(overture.SchemaDegraded) as exc_info:
            buildings.summarize_buildings(CENTER_LAT, CENTER_LON)
        assert "geometry" in exc_info.value.missing
    finally:
        buildings.set_data_path(str(BUILDINGS_FIXTURE_PATH))


# --- structured errors (upstream) -------------------------------------

def test_upstream_unavailable_raises(tmp_path):
    buildings.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    try:
        with pytest.raises(overture.UpstreamUnavailable):
            buildings.summarize_buildings(CENTER_LAT, CENTER_LON)
        with pytest.raises(overture.UpstreamUnavailable):
            buildings.buildings_at(CENTER_LAT, CENTER_LON)
    finally:
        buildings.set_data_path(str(BUILDINGS_FIXTURE_PATH))


# --- budget ---------------------------------------------------------------

def test_buildings_at_server_tool_applies_budget(monkeypatch):
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "80")
    result = server.buildings_at(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M, limit=25)
    assert "error" not in result
    assert result["truncated"] is True
    assert result["omitted_count"] > 0
    assert len(result["results"]) < 25


def test_summarize_buildings_server_tool_happy_path():
    result = server.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M)
    assert "error" not in result
    assert result["count"] == _N


def test_server_tools_return_structured_error_on_bad_path(tmp_path):
    buildings.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    try:
        result = server.summarize_buildings(CENTER_LAT, CENTER_LON)
        assert result["error"] == "upstream_unavailable"
        result = server.buildings_at(CENTER_LAT, CENTER_LON)
        assert result["error"] == "upstream_unavailable"
    finally:
        buildings.set_data_path(str(BUILDINGS_FIXTURE_PATH))


# --- live (opt-in) ---------------------------------------------------------

@pytest.mark.live
def test_summarize_buildings_against_real_overture_data():
    """Downtown Austin, 300m: sanity only, not exact numbers.

    A dense US downtown should have well over 50 buildings within 300m, and
    mean footprint area for a mix of commercial/residential buildings in a
    dense core should land somewhere in the tens-to-low-thousands of square
    meters — 50-5000 m^2 is a generous plausibility band, not a tight bound.
    """
    result = buildings.summarize_buildings(30.2672, -97.7431, radius_m=300)
    assert result["count"] > 50
    assert 50 <= result["mean_footprint_area_m2"] <= 5000
    print("\nlive summarize_buildings(downtown Austin, 300m):", result)
