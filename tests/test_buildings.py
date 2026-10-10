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

from placeroot import budget, buildings, geo, overture, server

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

def test_radius_m_reports_the_effective_clamped_radius():
    # #136 (buildings sibling of #131): _bbox_filter clamps radius_m to
    # geo.MAX_QUERY_RADIUS_M and searches with that, so the response must
    # report the effective radius, not the caller's un-clamped input.
    over = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=5_000_000)
    assert over["radius_m"] == geo.MAX_QUERY_RADIUS_M
    normal = buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=FULL_GRID_RADIUS_M)
    assert normal["radius_m"] == FULL_GRID_RADIUS_M


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


# --- multi-tile reads return a straddling footprint once --------------------
#
# Offline harness: the spatial extension cannot be installed here, so these
# tests run the module's real queries on a bare connection whose ST_* names
# are stub macros over a [lon, lat] list geometry (each footprint's centre;
# ST_Area is a constant 1.0 deg^2). The bbox prefilter — the part the tile
# edge affects — is the real one.


def _stub_spatial_conn():
    con = duckdb.connect()
    for ddl in (
        "CREATE MACRO ST_GeomFromWKB(g) AS g",
        "CREATE MACRO ST_Centroid(g) AS g",
        "CREATE MACRO ST_X(g) AS g[1]",
        "CREATE MACRO ST_Y(g) AS g[2]",
        "CREATE MACRO ST_Area(g) AS 1.0::DOUBLE",
    ):
        con.execute(ddl)
    return con


def _bbox_of(lat_min, lat_max, lon_min, lon_max):
    return {"xmin": lon_min, "ymin": lat_min, "xmax": lon_max, "ymax": lat_max}


# A footprint crossing the tile edge at lon=-74, and one wholly inside the
# eastern tile; both residential/house, one with a known height.
_EDGE_HOUSE = ("bld-straddle", _bbox_of(40.649, 40.651, -74.001, -73.999), 10.0)
_INSIDE_HOUSE = ("bld-inside", _bbox_of(40.649, 40.651, -73.995, -73.993), None)


def _write_centre_geometry_fixture(path, rows) -> None:
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE buildings (
            id VARCHAR,
            geometry DOUBLE[],
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            subtype VARCHAR,
            class VARCHAR,
            height DOUBLE,
            num_floors INTEGER
        )
    """)
    for id_, bbox, height in rows:
        centre = [(bbox["xmin"] + bbox["xmax"]) / 2, (bbox["ymin"] + bbox["ymax"]) / 2]
        con.execute(
            "INSERT INTO buildings VALUES (?, ?, ?, ?, ?, ?, ?)",
            [id_, centre, bbox, "residential", "house", height, None],
        )
    con.execute(f"COPY buildings TO '{path}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def straddling_buildings(tmp_path, monkeypatch):
    """Both tiles either side of lon=-74 materialized from the fixture, the
    straddling footprint proven to be in both, and buildings.py reading
    the pair."""
    from placeroot import cache, db

    monkeypatch.setenv("PLACEROOT_CACHE", "on")
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "placeroot-cache"))
    con = _stub_spatial_conn()
    monkeypatch.setattr(db, "shared_conn", lambda: con)
    monkeypatch.setattr(db, "ensure_spatial", lambda: None)
    src = tmp_path / "buildings.parquet"
    _write_centre_geometry_fixture(src, [_EDGE_HOUSE, _INSIDE_HOUSE])
    buildings.set_data_path(str(src))
    theme = buildings.THEME
    deg = cache.tile_deg_for(theme)
    ty = int(40.65 // deg)
    tiles = [(round(-74.0 / deg) - 1, ty), (round(-74.0 / deg), ty)]
    fingerprint = cache.resolve_fingerprint("2026-07-22.0", theme, str(src))
    paths = [cache.ensure_tile(con, "2026-07-22.0", theme, t, str(src), fingerprint)
             for t in tiles]
    for p in paths:
        (n,) = con.execute(
            f"SELECT count(*) FROM read_parquet({db._sql_str(str(p))}) "
            "WHERE id = 'bld-straddle'"
        ).fetchone()
        assert n == 1
    source = f"read_parquet([{', '.join(db._sql_str(str(p)) for p in paths)}])"
    monkeypatch.setattr(buildings, "_from_source", lambda bbox: source)
    try:
        yield con
    finally:
        buildings.set_data_path(None)


def test_summary_does_not_count_a_straddling_footprint_twice(straddling_buildings):
    """count, footprint-area totals, coverage percentages and the
    subtype/class breakdown are all computed over the deduped rows."""
    result = buildings.summarize_buildings(40.65, -74.0005, radius_m=2000)
    assert result["count"] == 2
    one_m2 = buildings._area_m2(1.0, 40.65)
    assert result["total_footprint_area_m2"] == pytest.approx(2 * one_m2, rel=1e-6)
    assert result["mean_footprint_area_m2"] == pytest.approx(one_m2, rel=1e-6)
    assert result["height_known_pct"] == 50.0
    assert result["top_subtypes"] == [{"subtype": "residential", "count": 2}]
    assert result["top_classes"] == [{"class": "house", "count": 2}]


def test_a_footprint_straddling_a_tile_edge_is_listed_once(straddling_buildings):
    rows = buildings.buildings_at(40.65, -74.0005, radius_m=2000, limit=10)
    assert [r["id"] for r in rows] == ["bld-straddle", "bld-inside"]


def test_no_id_column_means_no_dedupe_in_buildings(straddling_buildings, monkeypatch):
    """Without id there is nothing to key on (a NULL partition would
    collapse every row into one), so no QUALIFY is emitted and the
    duplicate shows — the harness really does see both copies."""
    monkeypatch.setattr(buildings, "_check_schema", lambda glob: ["id"])
    assert buildings._dedupe_clause({"id"}) == ""
    assert buildings.summarize_buildings(40.65, -74.0005, radius_m=2000)["count"] == 3
    rows = buildings.buildings_at(40.65, -74.0005, radius_m=2000, limit=10)
    assert [r["id"] for r in rows] == [None, None, None]
