"""Issue #167: land_use_at(lat, lon) against a small synthetic base-theme
fixture — two land_use polygons (a big "residential" rectangle with a
smaller "park" nested inside it, both containing CENTER_LAT/CENTER_LON) and
one land_cover polygon ("grass") covering the same point. Mirrors
buildings.py/divisions.py's WKT->WKB fixture-building pattern (see
scripts/build_fixture.py's box_wkt/build_division_rows) but builds its
fixtures locally via duckdb rather than editing the shared fixture script,
since this is the only test module that needs theme=base at all.
"""

import duckdb
import pytest

from placeroot import land_use, overture, server

from .conftest import CENTER_LAT, CENTER_LON

# A point nowhere near any fixture polygon.
NOWHERE_LAT, NOWHERE_LON = 10.0, 10.0


def box_wkt(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> str:
    """Mirrors scripts/build_fixture.py's box_wkt: a rectangular polygon WKT."""
    return (
        f"POLYGON(({lon_min} {lat_min}, {lon_min} {lat_max}, "
        f"{lon_max} {lat_max}, {lon_max} {lat_min}, {lon_min} {lat_min}))"
    )


def _bbox_of(lat_min, lat_max, lon_min, lon_max):
    return {"xmin": lon_min, "ymin": lat_min, "xmax": lon_max, "ymax": lat_max}


# Big polygon: contains CENTER, and is much larger than the nested park.
_RESIDENTIAL_BOX = (40.65, 40.75, -73.95, -73.85)
# Small polygon nested inside the residential one, also containing CENTER —
# the overlap case: land_use_at must pick this (smaller area), not the
# residential polygon, and must flag the ambiguity via "note".
_PARK_BOX = (40.698, 40.702, -73.902, -73.898)
# land_cover polygon, same footprint as the residential land_use box, so a
# single query point is covered by both a land_use and a land_cover row.
_GRASS_BOX = (40.65, 40.75, -73.95, -73.85)


def _build_land_use_fixture(path, include_class=True) -> None:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    (residential_wkb,) = con.execute(
        f"SELECT ST_AsWKB(ST_GeomFromText('{box_wkt(*_RESIDENTIAL_BOX)}'))"
    ).fetchone()
    (park_wkb,) = con.execute(
        f"SELECT ST_AsWKB(ST_GeomFromText('{box_wkt(*_PARK_BOX)}'))"
    ).fetchone()
    rows = [
        ("lu-residential", residential_wkb, _bbox_of(*_RESIDENTIAL_BOX),
         "residential", "residential" if include_class else None, "Downtown Residential"),
        ("lu-park", park_wkb, _bbox_of(*_PARK_BOX), "park",
         "park" if include_class else None, "Test Park"),
    ]
    class_col = "class VARCHAR," if include_class else ""
    con.execute(f"""
        CREATE TABLE land_use (
            id VARCHAR,
            geometry BLOB,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            subtype VARCHAR,
            {class_col}
            names STRUCT("primary" VARCHAR)
        )
    """)
    insert_rows = [
        (r[0], r[1], r[2], r[3], {"primary": r[5]}) if not include_class
        else (r[0], r[1], r[2], r[3], r[4], {"primary": r[5]})
        for r in rows
    ]
    placeholders = ", ".join(["?"] * (6 if include_class else 5))
    con.executemany(f"INSERT INTO land_use VALUES ({placeholders})", insert_rows)
    con.execute(f"COPY land_use TO '{path}' (FORMAT PARQUET)")
    con.close()


def _build_land_cover_fixture(path) -> None:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    (grass_wkb,) = con.execute(
        f"SELECT ST_AsWKB(ST_GeomFromText('{box_wkt(*_GRASS_BOX)}'))"
    ).fetchone()
    con.execute("""
        CREATE TABLE land_cover (
            id VARCHAR,
            geometry BLOB,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            subtype VARCHAR,
            class VARCHAR
        )
    """)
    con.execute(
        "INSERT INTO land_cover VALUES (?, ?, ?, ?, ?)",
        ["lc-grass", grass_wkb, _bbox_of(*_GRASS_BOX), "grass", "grass"],
    )
    con.execute(f"COPY land_cover TO '{path}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def land_use_fixture(tmp_path):
    """Points land_use.py at the standard (non-degraded) fixtures for one test, then resets."""
    lu_path = tmp_path / "land_use.parquet"
    lc_path = tmp_path / "land_cover.parquet"
    _build_land_use_fixture(lu_path)
    _build_land_cover_fixture(lc_path)
    land_use.set_data_path(str(lu_path), type_=land_use.TYPE_LAND_USE)
    land_use.set_data_path(str(lc_path), type_=land_use.TYPE_LAND_COVER)
    try:
        yield lu_path, lc_path
    finally:
        land_use.set_data_path(None, type_=land_use.TYPE_LAND_USE)
        land_use.set_data_path(None, type_=land_use.TYPE_LAND_COVER)


# --- happy path -------------------------------------------------------------


def test_point_inside_known_polygons_returns_correct_classification(land_use_fixture):
    result = land_use.land_use_at(CENTER_LAT, CENTER_LON)
    # CENTER falls inside both the residential and the nested park polygon —
    # the park is smaller, so it must win (see the overlap test below for the
    # explicit assertion); land_cover has only the one covering polygon.
    assert result["land_cover"] == {"subtype": "grass", "class": "grass"}


def test_no_covering_polygon_returns_none_not_an_error(land_use_fixture):
    result = land_use.land_use_at(NOWHERE_LAT, NOWHERE_LON)
    assert result["land_use"] is None
    assert result["land_cover"] is None
    assert "note" not in result


# --- overlap / smallest-area wins -------------------------------------------


def test_overlapping_polygons_return_the_smallest_and_flag_a_note(land_use_fixture):
    result = land_use.land_use_at(CENTER_LAT, CENTER_LON)
    assert result["land_use"]["subtype"] == "park"
    assert result["land_use"]["class"] == "park"
    assert result["land_use"]["name"] == "Test Park"
    assert "note" in result
    assert "land_use" in result["note"]


def test_point_only_in_the_big_polygon_is_unambiguous(land_use_fixture):
    """A point inside the residential box but outside the nested park box."""
    lat, lon = 40.72, -73.90  # within _RESIDENTIAL_BOX, outside _PARK_BOX
    result = land_use.land_use_at(lat, lon)
    assert result["land_use"] == {
        "subtype": "residential", "class": "residential", "name": "Downtown Residential",
    }
    assert "note" not in result


# --- schema degrade -----------------------------------------------------


def test_missing_class_column_degrades_to_none(tmp_path):
    lu_path = tmp_path / "land_use_no_class.parquet"
    lc_path = tmp_path / "land_cover.parquet"
    _build_land_use_fixture(lu_path, include_class=False)
    _build_land_cover_fixture(lc_path)
    land_use.set_data_path(str(lu_path), type_=land_use.TYPE_LAND_USE)
    land_use.set_data_path(str(lc_path), type_=land_use.TYPE_LAND_COVER)
    try:
        result = land_use.land_use_at(CENTER_LAT, CENTER_LON)
        assert result["land_use"]["class"] is None
        assert result["land_use"]["subtype"] == "park"
        assert "class" in land_use.degraded_fields()
    finally:
        land_use.set_data_path(None, type_=land_use.TYPE_LAND_USE)
        land_use.set_data_path(None, type_=land_use.TYPE_LAND_COVER)


def test_missing_geometry_raises_schema_degraded(tmp_path):
    lu_path = tmp_path / "land_use.parquet"
    lc_path = tmp_path / "land_cover.parquet"
    _build_land_use_fixture(lu_path)
    _build_land_cover_fixture(lc_path)
    no_geom_path = tmp_path / "no_geometry.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (geometry) FROM read_parquet("
        f"'{lu_path}')) TO '{no_geom_path}' (FORMAT PARQUET)"
    )
    con.close()
    land_use.set_data_path(str(no_geom_path), type_=land_use.TYPE_LAND_USE)
    land_use.set_data_path(str(lc_path), type_=land_use.TYPE_LAND_COVER)
    try:
        with pytest.raises(overture.SchemaDegraded) as exc_info:
            land_use.land_use_at(CENTER_LAT, CENTER_LON)
        assert "geometry" in exc_info.value.missing
    finally:
        land_use.set_data_path(None, type_=land_use.TYPE_LAND_USE)
        land_use.set_data_path(None, type_=land_use.TYPE_LAND_COVER)


# --- server wiring -----------------------------------------------------


def test_server_land_use_at_rejects_out_of_range_coordinate(land_use_fixture):
    # #163 pattern: out-of-range (or swapped) coordinates are a bad_request
    # at the tool boundary, before any base-theme scan runs.
    result = server.land_use_at(lat=91.0, lon=0.0)
    assert result["error"] == "bad_request"
    result = server.land_use_at(lat=120.98, lon=14.60)
    assert result["error"] == "bad_request"
    assert "swap" in result["detail"]


def test_cache_theme_is_a_portable_path_component():
    # cache.tile_path uses the theme string verbatim as a directory
    # component; ':' (the original separator) is illegal on Windows.
    for type_ in (land_use.TYPE_LAND_USE, land_use.TYPE_LAND_COVER):
        theme = land_use._cache_theme(type_)
        assert theme  # non-empty
        assert not any(c in theme for c in ':\\/'), theme


def test_server_land_use_at_happy_path(land_use_fixture):
    result = server.land_use_at(CENTER_LAT, CENTER_LON)
    assert "error" not in result
    assert result["lat"] == CENTER_LAT
    assert result["lon"] == CENTER_LON
    assert result["land_use"]["subtype"] == "park"
    assert result["land_cover"] == {"subtype": "grass", "class": "grass"}
    assert "geometry" not in result["land_use"]
    assert "geometry" not in result["land_cover"]


def test_server_structured_error_on_unreachable_upstream(tmp_path):
    land_use.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), type_=land_use.TYPE_LAND_USE
    )
    land_use.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), type_=land_use.TYPE_LAND_COVER
    )
    try:
        result = server.land_use_at(CENTER_LAT, CENTER_LON)
        assert result["error"] == "upstream_unavailable"
    finally:
        land_use.set_data_path(None, type_=land_use.TYPE_LAND_USE)
        land_use.set_data_path(None, type_=land_use.TYPE_LAND_COVER)


# --- the bundled coarse land-cover grid (cold-path answer) ------------------


def _write_grid(tmp_path, cells):
    import duckdb as _duckdb

    p = tmp_path / "grid.parquet"
    con = _duckdb.connect()
    con.execute("CREATE TABLE g (gx SMALLINT, gy SMALLINT, subtype VARCHAR)")
    for gx, gy, st in cells:
        con.execute("INSERT INTO g VALUES (?, ?, ?)", [gx, gy, st])
    con.execute(f"COPY g TO '{p}' (FORMAT PARQUET)")
    con.close()
    return p


def test_grid_answers_cold_land_cover_with_an_approximation_note(tmp_path, monkeypatch):
    import math

    from placeroot import overture as ov

    gx = math.floor(CENTER_LON / land_use.GRID_DEG)
    gy = math.floor(CENTER_LAT / land_use.GRID_DEG)
    grid = _write_grid(tmp_path, [(gx, gy, "forest")])
    monkeypatch.setattr(land_use, "_bundled_grid_path", lambda: grid)
    monkeypatch.setattr(land_use, "_land_cover_tiles_warm", lambda lat, lon: False)
    monkeypatch.setattr(ov, "dataset_is_pinned", lambda theme, type_=None: False)
    # The grid path schedules real land_cover tiles for convergence; that
    # side effect is live-network and not under test here. land_use's own
    # classify still needs the real source.
    real_source_sql = land_use.cache.source_sql

    def _no_land_cover_scheduling(theme, *a, **k):
        if "land_cover" in theme:
            return None
        return real_source_sql(theme, *a, **k)

    monkeypatch.setattr(land_use.cache, "source_sql", _no_land_cover_scheduling)

    result = land_use.land_use_at(CENTER_LAT, CENTER_LON)
    assert result["land_cover"] == {"subtype": "forest", "class": None}
    assert land_use.APPROXIMATE_LAND_COVER_NOTE in result.get("note", "")


def test_grid_absent_cell_means_no_land_cover_not_a_fallback_scan(tmp_path, monkeypatch):
    from placeroot import overture as ov

    grid = _write_grid(tmp_path, [(1000, 1000, "forest")])  # nowhere near CENTER
    monkeypatch.setattr(land_use, "_bundled_grid_path", lambda: grid)
    monkeypatch.setattr(land_use, "_land_cover_tiles_warm", lambda lat, lon: False)
    monkeypatch.setattr(ov, "dataset_is_pinned", lambda theme, type_=None: False)

    result = land_use.land_use_at(CENTER_LAT, CENTER_LON)
    assert result["land_cover"] is None


def test_pinned_datasets_never_see_the_grid():
    """Fixture-pinned runs (this suite) take the exact path — the fixture's
    own land_cover rows answer, no approximation note appears."""
    result = land_use.land_use_at(CENTER_LAT, CENTER_LON)
    assert land_use.APPROXIMATE_LAND_COVER_NOTE not in result.get("note", "")


def test_warm_tiles_prefer_the_exact_path(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(land_use, "_land_cover_tiles_warm", lambda lat, lon: True)
    monkeypatch.setattr(
        land_use, "_grid_land_cover",
        lambda lat, lon: called.append(1) or {"subtype": "x", "class": None},
    )
    land_use.land_use_at(CENTER_LAT, CENTER_LON)
    assert called == []
