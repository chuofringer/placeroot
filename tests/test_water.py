"""Issue #200: water_near(lat, lon, radius_m, limit) against small synthetic
base/water fixtures.

Three fixtures, one per behaviour the live data forced into the design:

- `water_fixture` — one of each geometry kind the real dataset mixes (a
  spring POINT, a long canal LINESTRING, a pond POLYGON, a salt lagoon),
  for ordering, distance-to-geometry and flag reporting.
- `ocean_fixture` — the trap: a 1-degree "ocean tile" polygon that contains
  the query point (Overture's generalized ocean covers dry coastal land)
  plus an adjacent tile whose shared grid-cut edge sits metres away. Pins
  that neither produces a distance row.
- `canal_fixture` — Amsterdam in miniature: many canals, for the true
  in-range count, the truncated flag and the subtype filter.

Builds its fixtures locally via duckdb (WKT -> WKB BLOB) rather than
editing the shared scripts/build_fixture.py, exactly as
tests/test_infrastructure.py and tests/test_land_use.py do for theme=base.
"""

import duckdb
import pytest

from placeroot import geo, overture, server, water

from .conftest import CENTER_LAT, CENTER_LON

# A point nowhere near any fixture feature — the arid case.
NOWHERE_LAT, NOWHERE_LON = 10.0, 10.0

# Roughly metres-per-degree at CENTER_LAT, used only to place features at
# recognisably different distances from the query point.
_M_PER_DEG_LAT = 111_320.0


def _north(metres: float) -> float:
    return CENTER_LAT + metres / _M_PER_DEG_LAT


# --- fixture geometry --------------------------------------------------------

# Spring: a bare point ~80 m north — the nearest feature, and intermittent.
_SPRING_WKT = f"POINT({CENTER_LON} {_north(80)})"

# Canal: a long east-west line ~200 m north whose eastern end is ~1.7 km
# away. Its centroid sits far from either end, so a centroid- or
# bbox-corner-based distance misreports it from anywhere but the middle.
_CANAL_LAT = _north(200)
_CANAL_WKT = f"LINESTRING({CENTER_LON} {_CANAL_LAT}, {CENTER_LON + 0.02} {_CANAL_LAT})"

# Pond: a polygon whose southern edge is ~350 m north (not containing the
# query point) — the polygon-edge distance case.
_POND_WKT = (
    f"POLYGON(({CENTER_LON - 0.01} {_north(350)}, {CENTER_LON - 0.01} {_north(800)}, "
    f"{CENTER_LON + 0.01} {_north(800)}, {CENTER_LON + 0.01} {_north(350)}, "
    f"{CENTER_LON - 0.01} {_north(350)}))"
)

# Salt lagoon: ~450 m north, carries is_salt.
_LAGOON_WKT = (
    f"POLYGON(({CENTER_LON - 0.001} {_north(450)}, {CENTER_LON - 0.001} {_north(500)}, "
    f"{CENTER_LON + 0.001} {_north(500)}, {CENTER_LON + 0.001} {_north(450)}, "
    f"{CENTER_LON - 0.001} {_north(450)}))"
)

# (id, wkt, subtype, class, name, is_salt, is_intermittent)
_FEATURES = [
    ("water-spring", _SPRING_WKT, "spring", "spring", "Test Spring", False, True),
    ("water-canal", _CANAL_WKT, "canal", "canal", "Test Canal", False, False),
    ("water-pond", _POND_WKT, "pond", "pond", "Test Pond", False, False),
    ("water-lagoon", _LAGOON_WKT, "lake", "lagoon", "Test Lagoon", True, False),
]


def _build_water_fixture(path, include_class=True, features=_FEATURES) -> None:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    class_col = "class VARCHAR," if include_class else ""
    con.execute(f"""
        CREATE TABLE water (
            id VARCHAR,
            geometry BLOB,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            subtype VARCHAR,
            {class_col}
            names STRUCT("primary" VARCHAR),
            is_salt BOOLEAN,
            is_intermittent BOOLEAN
        )
    """)
    for id_, wkt, subtype, class_, name, is_salt, is_intermittent in features:
        # bbox comes straight from the geometry, as it does upstream.
        wkb, xmin, ymin, xmax, ymax = con.execute(f"""
            SELECT ST_AsWKB(g), ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g)
            FROM (SELECT ST_GeomFromText('{wkt}') AS g)
        """).fetchone()
        bbox = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
        head = [id_, wkb, bbox, subtype]
        if include_class:
            head.append(class_)
        values = [*head, {"primary": name}, is_salt, is_intermittent]
        placeholders = ", ".join(["?"] * len(values))
        con.execute(f"INSERT INTO water VALUES ({placeholders})", values)
    con.execute(f"COPY water TO '{path}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def water_fixture(tmp_path):
    """Points water.py at the standard fixture for one test, then resets."""
    path = tmp_path / "water.parquet"
    _build_water_fixture(path)
    water.set_data_path(str(path))
    try:
        yield path
    finally:
        water.set_data_path(None)


# --- happy path -------------------------------------------------------------


def test_features_come_back_nearest_first(water_fixture):
    rows, radius_m, in_range_count, containing = water.water_near(CENTER_LAT, CENTER_LON)
    assert radius_m == water.DEFAULT_RADIUS_M
    assert [r["name"] for r in rows] == [
        "Test Spring", "Test Canal", "Test Pond", "Test Lagoon",
    ]
    assert in_range_count == 4
    assert containing is None
    distances = [r["distance_m"] for r in rows]
    assert distances == sorted(distances)


def test_distances_match_the_fixture_geometry(water_fixture):
    rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON)
    by_name = {r["name"]: r["distance_m"] for r in rows}
    assert by_name["Test Spring"] == pytest.approx(80.0, abs=5.0)
    assert by_name["Test Canal"] == pytest.approx(200.0, abs=5.0)
    assert by_name["Test Pond"] == pytest.approx(350.0, abs=5.0)
    assert by_name["Test Lagoon"] == pytest.approx(450.0, abs=5.0)


def test_line_distance_is_to_the_nearest_point_not_the_centroid(water_fixture):
    """The canal runs ~1.7 km east; its centroid is ~850 m from either end.

    Queried from beside its eastern half, the answer must be the
    perpendicular offset (~200 m), not the distance to the line's midpoint
    (which is what a centroid or bbox-corner stand-in would report, ~850 m
    — outside the default radius entirely, i.e. "no canal here").
    """
    lat, lon = CENTER_LAT, CENTER_LON + 0.015
    rows, _, _, _ = water.water_near(lat, lon, radius_m=400)
    canal = next(r for r in rows if r["name"] == "Test Canal")
    assert canal["distance_m"] == pytest.approx(200.0, abs=5.0)


def test_boolean_flags_appear_only_when_true(water_fixture):
    rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON)
    by_name = {r["name"]: r for r in rows}
    assert by_name["Test Spring"]["is_intermittent"] is True
    assert "is_salt" not in by_name["Test Spring"]
    assert by_name["Test Lagoon"]["is_salt"] is True
    assert "is_intermittent" not in by_name["Test Lagoon"]
    assert "is_salt" not in by_name["Test Canal"]
    assert "is_intermittent" not in by_name["Test Canal"]


def test_rows_carry_no_geometry(water_fixture):
    rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON)
    for row in rows:
        assert set(row) <= {
            "name", "subtype", "class", "distance_m", "is_salt", "is_intermittent",
        }
        assert {"subtype", "class", "distance_m"} <= set(row)


def test_radius_excludes_features_outside_it(water_fixture):
    rows, _, in_range_count, _ = water.water_near(CENTER_LAT, CENTER_LON, radius_m=250)
    assert [r["name"] for r in rows] == ["Test Spring", "Test Canal"]
    assert in_range_count == 2


def test_limit_caps_the_row_count_but_not_the_count(water_fixture):
    rows, _, in_range_count, _ = water.water_near(CENTER_LAT, CENTER_LON, limit=2)
    assert [r["name"] for r in rows] == ["Test Spring", "Test Canal"]
    assert in_range_count == 4


def test_limit_zero_still_answers_with_the_nearest(water_fixture):
    """limit<=0 clamps to 1, not 0: in_range_count rides a window function
    over the returned rows, so LIMIT 0 would report ([], 0) with water all
    around — the confident wrong "nothing here" this tool exists to avoid."""
    for bad_limit in (0, -3):
        rows, _, in_range_count, _ = water.water_near(
            CENTER_LAT, CENTER_LON, limit=bad_limit
        )
        assert [r["name"] for r in rows] == ["Test Spring"]
        assert in_range_count == 4


def test_class_filter_matches_the_class_column(water_fixture):
    rows, _, in_range_count, _ = water.water_near(
        CENTER_LAT, CENTER_LON, water_class="lagoon"
    )
    assert [r["name"] for r in rows] == ["Test Lagoon"]
    assert in_range_count == 1


# --- the gridded-ocean trap -------------------------------------------------
#
# Overture ships the ocean cut into 1-degree tiles whose landward edge
# covers dry coastal land: live, the SF Ferry Building is ST_Contains-inside
# its ocean tile. Two failure modes follow, and this fixture reproduces both
# in miniature:
#
#   * the containing tile's ST_ClosestPoint *is* the query point, so it
#     would rank first at 0.0 m ("you are in the ocean");
#   * the adjacent tile shares the grid cut that runs metres away through
#     what is really open water, so its boundary would report a tiny,
#     entirely phantom "distance to water".
#
# A 20 m pond is planted nearby as the honest answer that must survive.

_OCEAN_TILE_WKT = (
    f"POLYGON(({CENTER_LON - 0.5} {CENTER_LAT - 0.5}, {CENTER_LON - 0.5} {_north(10)}, "
    f"{CENTER_LON + 0.5} {_north(10)}, {CENTER_LON + 0.5} {CENTER_LAT - 0.5}, "
    f"{CENTER_LON - 0.5} {CENTER_LAT - 0.5}))"
)
# The tile to the north, sharing the grid cut 10 m from the query point.
_OCEAN_TILE_NORTH_WKT = (
    f"POLYGON(({CENTER_LON - 0.5} {_north(10)}, {CENTER_LON - 0.5} {CENTER_LAT + 0.5}, "
    f"{CENTER_LON + 0.5} {CENTER_LAT + 0.5}, {CENTER_LON + 0.5} {_north(10)}, "
    f"{CENTER_LON - 0.5} {_north(10)}))"
)
_COAST_POND_WKT = (
    f"POLYGON(({CENTER_LON - 0.0005} {_north(120)}, {CENTER_LON - 0.0005} {_north(160)}, "
    f"{CENTER_LON + 0.0005} {_north(160)}, {CENTER_LON + 0.0005} {_north(120)}, "
    f"{CENTER_LON - 0.0005} {_north(120)}))"
)
_OCEAN_FEATURES = [
    ("ocean-tile", _OCEAN_TILE_WKT, "ocean", "ocean", None, True, False),
    ("ocean-tile-n", _OCEAN_TILE_NORTH_WKT, "ocean", "ocean", None, True, False),
    ("coast-pond", _COAST_POND_WKT, "pond", "pond", "Coast Pond", False, False),
]


@pytest.fixture
def ocean_fixture(tmp_path):
    path = tmp_path / "water_ocean.parquet"
    _build_water_fixture(path, features=_OCEAN_FEATURES)
    water.set_data_path(str(path))
    try:
        yield path
    finally:
        water.set_data_path(None)


def test_point_inside_a_gridded_ocean_tile_reports_on_water_not_a_zero_metre_row(
    ocean_fixture,
):
    rows, _, in_range_count, containing = water.water_near(CENTER_LAT, CENTER_LON)
    assert containing == {"water_body": "ocean", "generalized": True}
    # Neither the containing tile nor its grid-cut neighbour contributes a
    # row — the pond is the only real answer, at its real distance.
    assert [r["name"] for r in rows] == ["Coast Pond"]
    assert in_range_count == 1
    assert rows[0]["distance_m"] == pytest.approx(120.0, abs=5.0)


def test_no_phantom_shoreline_distance_from_the_grid_cut(ocean_fixture):
    """The neighbouring tile's edge is 10 m away and is a pure artifact of
    the 1-degree grid. No row may be derived from it at any distance."""
    rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON, radius_m=50)
    assert rows == []
    for row in rows:
        assert row["subtype"] != "ocean"


def test_server_reports_on_water_with_an_explanatory_note(ocean_fixture):
    result = server.water_near(CENTER_LAT, CENTER_LON)
    assert result["on_water"] is True
    assert result["water_body"] == "ocean"
    assert all(r["distance_m"] > 0 for r in result["results"])
    assert "generalized" in result["note"]
    assert "1-degree" in result["note"]


def test_a_named_containing_body_is_not_flagged_generalized(tmp_path):
    """Standing in a small lake is also answered by on_water rather than a
    0.0 m row — but it carries no generalized-geometry caveat."""
    lake_wkt = (
        f"POLYGON(({CENTER_LON - 0.002} {CENTER_LAT - 0.002}, "
        f"{CENTER_LON - 0.002} {CENTER_LAT + 0.002}, "
        f"{CENTER_LON + 0.002} {CENTER_LAT + 0.002}, "
        f"{CENTER_LON + 0.002} {CENTER_LAT - 0.002}, "
        f"{CENTER_LON - 0.002} {CENTER_LAT - 0.002}))"
    )
    path = tmp_path / "water_lake.parquet"
    _build_water_fixture(
        path, features=[("lake-1", lake_wkt, "lake", "lake", "Sloterplas", False, False)]
    )
    water.set_data_path(str(path))
    try:
        rows, _, in_range_count, containing = water.water_near(CENTER_LAT, CENTER_LON)
        assert containing == {"water_body": "Sloterplas", "generalized": False}
        assert rows == []
        assert in_range_count == 0
        result = server.water_near(CENTER_LAT, CENTER_LON)
        assert result["water_body"] == "Sloterplas"
        assert "inside Sloterplas" in result["note"]
        assert "generalized" not in result["note"]
    finally:
        water.set_data_path(None)


def test_a_large_polygon_that_does_not_contain_the_point_is_still_excluded(tmp_path):
    """The generalized test is not containment-based: a tile-scale *marine*
    polygon (here class='sea' under subtype='physical') whose edge happens
    to run past the query point is an arbitrary open-water closure as often
    as a real shore, so it never yields a distance."""
    big_wkt = (
        f"POLYGON(({CENTER_LON - 0.5} {_north(100)}, {CENTER_LON - 0.5} {CENTER_LAT + 0.5}, "
        f"{CENTER_LON + 0.5} {CENTER_LAT + 0.5}, {CENTER_LON + 0.5} {_north(100)}, "
        f"{CENTER_LON - 0.5} {_north(100)}))"
    )
    path = tmp_path / "water_big.parquet"
    _build_water_fixture(
        path, features=[("sea-1", big_wkt, "physical", "sea", "Test Sea", True, False)]
    )
    water.set_data_path(str(path))
    try:
        rows, _, in_range_count, containing = water.water_near(CENTER_LAT, CENTER_LON)
        assert rows == []
        assert in_range_count == 0
        assert containing is None
    finally:
        water.set_data_path(None)


def test_a_small_physical_feature_is_not_treated_as_generalized(tmp_path):
    """subtype='physical' holds the Mediterranean *and* individual
    waterfalls (live: 14.6-degree sea rows next to 0.00015-degree
    waterfalls), which is why the generalized test is a geometric span test
    rather than a subtype allowlist. A waterfall must keep its distance."""
    falls_wkt = f"POINT({CENTER_LON} {_north(60)})"
    path = tmp_path / "water_falls.parquet"
    _build_water_fixture(
        path,
        features=[("falls-1", falls_wkt, "physical", "waterfall", "Test Falls", False, False)],
    )
    water.set_data_path(str(path))
    try:
        rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON)
        assert [r["name"] for r in rows] == ["Test Falls"]
        assert rows[0]["distance_m"] == pytest.approx(60.0, abs=5.0)
    finally:
        water.set_data_path(None)


# --- big lakes and rivers are not tile-cut ----------------------------------
#
# The gridded-ocean exclusion above must not spill onto the largest real
# bodies of water. Live against release 2026-07-22.0, Lake Michigan is a
# *single* subtype='lake' polygon spanning 3.29 x 4.49 degrees, Lago di
# Como 0.322 x 0.357, Lake Geneva 0.783, the Mississippi 0.553 — none of
# them cut by the 1-degree grid, all of them with real shorelines. A span
# test applied to polygons in general deleted every one of them from the
# distance list: a query 371 m from Lake Como came back with a 376 m
# creek and no lake, and a query 992 m off Lake Michigan filtered to
# water_class='lake' came back empty with an "arid, remote or unmapped"
# note.

# 0.4 x 0.4 degrees — well over the generalized span threshold, and a lake.
_BIG_LAKE_SOUTH = _north(371)
_BIG_LAKE_WKT = (
    f"POLYGON(({CENTER_LON - 0.2} {_BIG_LAKE_SOUTH}, "
    f"{CENTER_LON - 0.2} {_BIG_LAKE_SOUTH + 0.4}, "
    f"{CENTER_LON + 0.2} {_BIG_LAKE_SOUTH + 0.4}, "
    f"{CENTER_LON + 0.2} {_BIG_LAKE_SOUTH}, "
    f"{CENTER_LON - 0.2} {_BIG_LAKE_SOUTH}))"
)
_BIG_LAKE_FEATURES = [
    ("lake-big", _BIG_LAKE_WKT, "lake", "lake", "Lake Michigan", False, False),
    (
        "creek-1",
        f"LINESTRING({CENTER_LON - 0.001} {_north(376)}, "
        f"{CENTER_LON + 0.001} {_north(376)})",
        "stream",
        "stream",
        "Torrente Cosia",
        False,
        False,
    ),
]


@pytest.fixture
def big_lake_fixture(tmp_path):
    path = tmp_path / "water_big_lake.parquet"
    _build_water_fixture(path, features=_BIG_LAKE_FEATURES)
    water.set_data_path(str(path))
    try:
        yield path
    finally:
        water.set_data_path(None)


def test_a_large_lake_outside_the_point_keeps_a_real_edge_distance(big_lake_fixture):
    """371 m from a tile-scale lake must answer "the lake, 371 m", not "a
    creek 376 m away and no lake at all" — the live Lago di Como repro."""
    rows, _, in_range_count, containing = water.water_near(CENTER_LAT, CENTER_LON)
    assert containing is None
    assert [r["name"] for r in rows] == ["Lake Michigan", "Torrente Cosia"]
    assert rows[0]["distance_m"] == pytest.approx(371.0, abs=5.0)
    assert in_range_count == 2


def test_a_class_filter_still_finds_a_large_lake(big_lake_fixture):
    """water_class='lake' 371 m from a lake returned [] plus an "arid,
    remote or unmapped" note — the live Lake Michigan repro."""
    result = server.water_near(CENTER_LAT, CENTER_LON, water_class="lake")
    assert [r["name"] for r in result["results"]] == ["Lake Michigan"]
    assert "arid" not in result.get("note", "")


def test_inside_a_large_lake_is_on_water_without_the_tile_cut_claim(big_lake_fixture):
    """Standing in Lake Michigan is on_water — but the note must not
    recite ocean facts (1-degree tiles, dry coastal land) about a lake."""
    inside_lat = _BIG_LAKE_SOUTH + 0.2
    rows, _, _, containing = water.water_near(inside_lat, CENTER_LON)
    assert containing == {"water_body": "Lake Michigan", "generalized": False}
    assert all(r["name"] != "Lake Michigan" for r in rows)
    result = server.water_near(inside_lat, CENTER_LON)
    assert result["on_water"] is True
    assert result["water_body"] == "Lake Michigan"
    assert "inside Lake Michigan" in result["note"]
    assert "1-degree" not in result["note"]
    assert "generalized" not in result["note"]


# --- a subtype='ocean' filter is structurally unsatisfiable ------------------


def test_ocean_subtype_filter_says_why_it_is_empty(ocean_fixture):
    """subtype='ocean' can never match a distance row, because ocean rows
    are reported through on_water. Left to the query it returns [] and the
    empty-result note calls a pier "arid, remote or unmapped"."""
    result = server.water_near(CENTER_LAT, CENTER_LON, subtype="ocean")
    assert result["results"] == []
    assert result["on_water"] is True
    assert "arid" not in result["note"]
    assert "on_water" in result["note"]


def test_ocean_only_filter_recognises_substrings_but_not_ambiguous_ones():
    assert water._ocean_only_filter("ocean", None) is True
    assert water._ocean_only_filter("OCEAN", None) is True
    assert water._ocean_only_filter("cea", None) is True
    # 'o' also matches pond/reservoir, so the query still has to run.
    assert water._ocean_only_filter("o", None) is False
    assert water._ocean_only_filter("river", None) is False
    assert water._ocean_only_filter(None, None) is False


# --- degree-space nearest is not ground nearest -----------------------------


def test_high_latitude_nearest_point_is_measured_on_the_ground(tmp_path):
    """ST_ClosestPoint minimises *degree* distance, and a degree of
    longitude is cos(lat) shorter than one of latitude. For this segment at
    70 deg N the degree-space point is 4112.8 m away while the true nearest
    point is 2518.5 m — 63% high, enough to push the feature outside a
    3 km radius and delete it from the answer entirely."""
    lat0, lon0 = 70.0, 10.0
    wkt = f"LINESTRING({lon0 + 0.07} {lat0}, {lon0} {lat0 + 0.07})"
    path = tmp_path / "water_high_lat.parquet"
    _build_water_fixture(
        path, features=[("fjord-1", wkt, "river", "river", "Nordkanal", False, False)]
    )
    water.set_data_path(str(path))
    try:
        rows, _, in_range_count, _ = water.water_near(lat0, lon0, radius_m=3000)
        assert [r["name"] for r in rows] == ["Nordkanal"]
        assert in_range_count == 1
        assert rows[0]["distance_m"] == pytest.approx(2518.5, rel=0.01)
    finally:
        water.set_data_path(None)


# --- NULL subtype is not a reason to drop a row -----------------------------


def test_a_null_subtype_row_still_appears(tmp_path):
    """`NOT (subtype = 'ocean' ...)` is NULL, not TRUE, for a NULL subtype,
    and a NULL WHERE clause drops the row. Overture's subtype is nullable,
    so an unlabelled pond next door would silently vanish."""
    features = [
        (
            "nameless-1",
            f"POINT({CENTER_LON} {_north(90)})",
            None,
            None,
            "Unlabelled Water",
            False,
            False,
        ),
    ]
    path = tmp_path / "water_null_subtype.parquet"
    _build_water_fixture(path, features=features)
    water.set_data_path(str(path))
    try:
        rows, _, in_range_count, _ = water.water_near(CENTER_LAT, CENTER_LON)
        assert [r["name"] for r in rows] == ["Unlabelled Water"]
        assert rows[0]["subtype"] is None
        assert in_range_count == 1
    finally:
        water.set_data_path(None)


# --- canal density ----------------------------------------------------------
#
# A 0.1-degree box over Amsterdam holds 2044 water rows, 1084 of them
# canals. Miniature version: 30 canals closer than the one named river, so
# the unfiltered answer is a slice and says so, and a subtype filter finds
# the feature the canals were hiding.

_CANAL_COUNT = 30
_DENSE_FEATURES = [
    (
        f"canal-{i}",
        f"LINESTRING({CENTER_LON - 0.001} {_north(10 + 5 * i)}, "
        f"{CENTER_LON + 0.001} {_north(10 + 5 * i)})",
        "canal",
        "canal",
        f"Gracht {i}",
        False,
        False,
    )
    for i in range(_CANAL_COUNT)
] + [
    (
        "river-1",
        f"LINESTRING({CENTER_LON - 0.01} {_north(400)}, {CENTER_LON + 0.01} {_north(400)})",
        "river",
        "river",
        "Hidden River",
        False,
        False,
    )
]


@pytest.fixture
def canal_fixture(tmp_path):
    path = tmp_path / "water_canals.parquet"
    _build_water_fixture(path, features=_DENSE_FEATURES)
    water.set_data_path(str(path))
    try:
        yield path
    finally:
        water.set_data_path(None)


def test_dense_water_reports_the_true_in_range_count(canal_fixture):
    rows, _, in_range_count, _ = water.water_near(CENTER_LAT, CENTER_LON)
    assert len(rows) == water.DEFAULT_LIMIT
    assert all(r["subtype"] == "canal" for r in rows)
    assert in_range_count == _CANAL_COUNT + 1


def test_oversized_limit_is_clamped_to_max_rows(canal_fixture):
    rows, _, in_range_count, _ = water.water_near(CENTER_LAT, CENTER_LON, limit=10_000)
    assert len(rows) == min(water.MAX_ROWS, _CANAL_COUNT + 1)
    assert in_range_count == _CANAL_COUNT + 1


def test_subtype_filter_surfaces_the_feature_the_canals_were_hiding(canal_fixture):
    rows, _, in_range_count, _ = water.water_near(CENTER_LAT, CENTER_LON, subtype="river")
    assert [r["name"] for r in rows] == ["Hidden River"]
    assert in_range_count == 1


def test_server_flags_the_limit_bite(canal_fixture):
    result = server.water_near(CENTER_LAT, CENTER_LON)
    assert result["truncated"] is True
    assert result["in_range_count"] == _CANAL_COUNT + 1
    assert "subtype" in result["note"]


def test_server_untruncated_answer_carries_no_truncated_flag(water_fixture):
    result = server.water_near(CENTER_LAT, CENTER_LON)
    assert "truncated" not in result
    assert result["in_range_count"] == 4
    assert "note" not in result


# --- filter wildcard escaping ----------------------------------------------


def test_filter_underscore_is_literal_not_a_wildcard(tmp_path):
    """subtype='salt_pond' must not match 'saltXpond' — Overture's values
    are snake_case, so an unescaped '_' would over-match constantly."""
    features = [
        (
            "under-literal",
            f"POINT({CENTER_LON} {_north(50)})",
            "salt_pond",
            "salt_pond",
            "Literal Underscore",
            False,
            False,
        ),
        (
            "under-wildcard",
            f"POINT({CENTER_LON} {_north(60)})",
            "saltXpond",
            "saltXpond",
            "Wildcard Match",
            False,
            False,
        ),
    ]
    path = tmp_path / "water_underscore.parquet"
    _build_water_fixture(path, features=features)
    water.set_data_path(str(path))
    try:
        rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON, subtype="salt_pond")
        assert [r["name"] for r in rows] == ["Literal Underscore"]
        rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON, water_class="salt_pond")
        assert [r["name"] for r in rows] == ["Literal Underscore"]
    finally:
        water.set_data_path(None)


# --- empty is an answer, not an error ---------------------------------------


def test_arid_point_returns_empty_results(water_fixture):
    rows, _, in_range_count, containing = water.water_near(NOWHERE_LAT, NOWHERE_LON)
    assert rows == []
    assert in_range_count == 0
    assert containing is None


def test_server_arid_point_says_so_in_a_note(water_fixture):
    result = server.water_near(NOWHERE_LAT, NOWHERE_LON)
    assert "error" not in result
    assert result["results"] == []
    assert result["in_range_count"] == 0
    assert "no water features within" in result["note"]
    assert "on_water" not in result


# --- radius clamp -----------------------------------------------------------


def test_oversized_radius_is_clamped_and_reported(water_fixture):
    _, radius_m, _, _ = water.water_near(
        CENTER_LAT, CENTER_LON, radius_m=geo.MAX_QUERY_RADIUS_M * 10
    )
    assert radius_m == geo.MAX_QUERY_RADIUS_M


def test_non_finite_radius_clamps_to_zero(water_fixture):
    rows, radius_m, _, _ = water.water_near(
        CENTER_LAT, CENTER_LON, radius_m=float("nan")
    )
    assert radius_m == 0.0
    assert rows == []


# --- schema degrade ---------------------------------------------------------


def test_missing_class_column_degrades_to_none(tmp_path):
    path = tmp_path / "water_no_class.parquet"
    _build_water_fixture(path, include_class=False)
    water.set_data_path(str(path))
    try:
        rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON)
        assert [r["subtype"] for r in rows] == ["spring", "canal", "pond", "lake"]
        assert all(r["class"] is None for r in rows)
        assert "class" in water.degraded_fields()
    finally:
        water.set_data_path(None)


def test_missing_flag_columns_degrade_rather_than_fail(tmp_path):
    path = tmp_path / "water.parquet"
    _build_water_fixture(path)
    stripped = tmp_path / "water_no_flags.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (is_salt, is_intermittent) FROM read_parquet("
        f"'{path}')) TO '{stripped}' (FORMAT PARQUET)"
    )
    con.close()
    water.set_data_path(str(stripped))
    try:
        rows, _, _, _ = water.water_near(CENTER_LAT, CENTER_LON)
        assert [r["name"] for r in rows][0] == "Test Spring"
        assert all("is_salt" not in r and "is_intermittent" not in r for r in rows)
        assert set(water.degraded_fields()) >= {"is_salt", "is_intermittent"}
    finally:
        water.set_data_path(None)


def test_missing_geometry_raises_schema_degraded(tmp_path):
    path = tmp_path / "water.parquet"
    _build_water_fixture(path)
    no_geom_path = tmp_path / "no_geometry.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (geometry) FROM read_parquet("
        f"'{path}')) TO '{no_geom_path}' (FORMAT PARQUET)"
    )
    con.close()
    water.set_data_path(str(no_geom_path))
    try:
        with pytest.raises(overture.SchemaDegraded) as exc_info:
            water.water_near(CENTER_LAT, CENTER_LON)
        assert "geometry" in exc_info.value.missing
    finally:
        water.set_data_path(None)


def test_cache_theme_is_a_portable_path_component():
    theme = water._cache_theme()
    assert theme == "base_water"
    assert not any(c in theme for c in ':\\/'), theme


# --- server wiring ----------------------------------------------------------


def test_server_water_near_happy_path(water_fixture):
    result = server.water_near(CENTER_LAT, CENTER_LON)
    assert "error" not in result
    assert result["center"] == {"lat": CENTER_LAT, "lon": CENTER_LON}
    assert result["radius_m"] == water.DEFAULT_RADIUS_M
    assert [r["name"] for r in result["results"]] == [
        "Test Spring", "Test Canal", "Test Pond", "Test Lagoon",
    ]


def test_server_water_near_rejects_out_of_range_coordinate(water_fixture):
    result = server.water_near(lat=91.0, lon=0.0)
    assert result["error"] == "bad_request"
    result = server.water_near(lat=120.98, lon=14.60)
    assert result["error"] == "bad_request"
    assert "swap" in result["detail"]


def test_server_structured_error_on_unreachable_upstream(tmp_path):
    water.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    try:
        result = server.water_near(CENTER_LAT, CENTER_LON)
        assert result["error"] == "upstream_unavailable"
    finally:
        water.set_data_path(None)
