"""Issue #179: infrastructure_at(lat, lon, radius_m, limit) against a small
synthetic base/infrastructure fixture — one of each geometry kind the real
dataset mixes: a bridge LINESTRING, an airport POLYGON, and a communication
tower POINT, all near CENTER_LAT/CENTER_LON at known, different distances.

Builds its fixture locally via duckdb (WKT -> WKB BLOB) rather than editing
the shared scripts/build_fixture.py, exactly as tests/test_land_use.py does
for theme=base — this and that module are the only consumers of the theme.
"""

import duckdb
import pytest

from placeroot import geo, infrastructure, overture, server

from .conftest import CENTER_LAT, CENTER_LON

# A point nowhere near any fixture feature.
NOWHERE_LAT, NOWHERE_LON = 10.0, 10.0

# Roughly metres-per-degree at CENTER_LAT, used only to place the fixture
# features at recognisably different distances from the query point.
_M_PER_DEG_LAT = 111_320.0

# Tower: a bare point ~100 m north of CENTER — the nearest feature.
_TOWER_LAT = CENTER_LAT + 100.0 / _M_PER_DEG_LAT
_TOWER_LON = CENTER_LON

# Bridge: a long east-west linestring ~250 m north of CENTER. Its centroid
# sits directly north of CENTER too, but the line extends far to the east,
# so a bbox-corner or bbox-centre distance would misreport it — the closest
# *point on the line* is what should be measured.
_BRIDGE_LAT = CENTER_LAT + 250.0 / _M_PER_DEG_LAT
_BRIDGE_LON_MIN = CENTER_LON
_BRIDGE_LON_MAX = CENTER_LON + 0.02

# Airport: a polygon whose southern edge is ~400 m north of CENTER.
_AIRPORT_LAT_MIN = CENTER_LAT + 400.0 / _M_PER_DEG_LAT
_AIRPORT_LAT_MAX = CENTER_LAT + 900.0 / _M_PER_DEG_LAT
_AIRPORT_LON_MIN = CENTER_LON - 0.01
_AIRPORT_LON_MAX = CENTER_LON + 0.01

_TOWER_WKT = f"POINT({_TOWER_LON} {_TOWER_LAT})"
_BRIDGE_WKT = (
    f"LINESTRING({_BRIDGE_LON_MIN} {_BRIDGE_LAT}, {_BRIDGE_LON_MAX} {_BRIDGE_LAT})"
)
_AIRPORT_WKT = (
    f"POLYGON(({_AIRPORT_LON_MIN} {_AIRPORT_LAT_MIN}, "
    f"{_AIRPORT_LON_MIN} {_AIRPORT_LAT_MAX}, "
    f"{_AIRPORT_LON_MAX} {_AIRPORT_LAT_MAX}, "
    f"{_AIRPORT_LON_MAX} {_AIRPORT_LAT_MIN}, "
    f"{_AIRPORT_LON_MIN} {_AIRPORT_LAT_MIN}))"
)

_FEATURES = [
    ("infra-tower", _TOWER_WKT, "communication", "communication_tower", "Test Tower"),
    ("infra-bridge", _BRIDGE_WKT, "bridge", "bridge", "Test Bridge"),
    ("infra-airport", _AIRPORT_WKT, "airport", "airport", "Test Airport"),
]


def _build_infrastructure_fixture(path, include_class=True, features=_FEATURES) -> None:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    class_col = "class VARCHAR," if include_class else ""
    con.execute(f"""
        CREATE TABLE infrastructure (
            id VARCHAR,
            geometry BLOB,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            subtype VARCHAR,
            {class_col}
            names STRUCT("primary" VARCHAR)
        )
    """)
    for id_, wkt, subtype, class_, name in features:
        # bbox comes straight from the geometry, as it does upstream —
        # hand-written boxes would be one more thing to get wrong.
        wkb, xmin, ymin, xmax, ymax = con.execute(f"""
            SELECT ST_AsWKB(g), ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g)
            FROM (SELECT ST_GeomFromText('{wkt}') AS g)
        """).fetchone()
        bbox = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
        values = (
            [id_, wkb, bbox, subtype, class_, {"primary": name}] if include_class
            else [id_, wkb, bbox, subtype, {"primary": name}]
        )
        placeholders = ", ".join(["?"] * len(values))
        con.execute(f"INSERT INTO infrastructure VALUES ({placeholders})", values)
    con.execute(f"COPY infrastructure TO '{path}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def infrastructure_fixture(tmp_path):
    """Points infrastructure.py at the standard fixture for one test, then resets."""
    path = tmp_path / "infrastructure.parquet"
    _build_infrastructure_fixture(path)
    infrastructure.set_data_path(str(path))
    try:
        yield path
    finally:
        infrastructure.set_data_path(None)


# --- happy path -------------------------------------------------------------


def test_all_three_geometry_kinds_are_returned_nearest_first(infrastructure_fixture):
    rows, radius_m, _ = infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON)
    assert radius_m == infrastructure.DEFAULT_RADIUS_M
    assert [r["name"] for r in rows] == ["Test Tower", "Test Bridge", "Test Airport"]
    assert [r["subtype"] for r in rows] == ["communication", "bridge", "airport"]
    assert [r["class"] for r in rows] == ["communication_tower", "bridge", "airport"]
    distances = [r["distance_m"] for r in rows]
    assert distances == sorted(distances)


def test_distances_match_the_fixture_geometry(infrastructure_fixture):
    rows, _, _ = infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON)
    by_name = {r["name"]: r["distance_m"] for r in rows}
    # Each feature was placed at a known offset due north of CENTER; a few
    # metres of slack covers the haversine-vs-flat-earth difference in how
    # the fixture offsets were computed.
    assert by_name["Test Tower"] == pytest.approx(100.0, abs=5.0)
    assert by_name["Test Bridge"] == pytest.approx(250.0, abs=5.0)
    assert by_name["Test Airport"] == pytest.approx(400.0, abs=5.0)


def test_line_distance_is_to_the_nearest_point_not_the_centroid(infrastructure_fixture):
    """The bridge extends ~1.7 km east; its centroid is far from its near end.

    Queried from under the bridge's eastern half, the answer must be the
    perpendicular offset (~250 m), not the distance to the line's midpoint.
    """
    lat, lon = CENTER_LAT, CENTER_LON + 0.015
    rows, _, _ = infrastructure.infrastructure_at(lat, lon, radius_m=400)
    bridge = next(r for r in rows if r["name"] == "Test Bridge")
    assert bridge["distance_m"] == pytest.approx(250.0, abs=5.0)


def test_radius_excludes_features_outside_it(infrastructure_fixture):
    rows, _, _ = infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON, radius_m=150)
    assert [r["name"] for r in rows] == ["Test Tower"]


def test_limit_caps_the_row_count(infrastructure_fixture):
    rows, _, _ = infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON, limit=2)
    assert [r["name"] for r in rows] == ["Test Tower", "Test Bridge"]


def test_limit_zero_still_answers_with_the_nearest(infrastructure_fixture):
    """limit<=0 clamps to 1, not 0: total_in_range rides a window function
    on the returned rows, so LIMIT 0 would report ([], total_in_range=0)
    with features in range — a confident wrong "nothing here"."""
    for bad_limit in (0, -3):
        rows, _, total_in_range = infrastructure.infrastructure_at(
            CENTER_LAT, CENTER_LON, limit=bad_limit
        )
        assert [r["name"] for r in rows] == ["Test Tower"]
        assert total_in_range == 3


def test_no_geometry_in_rows(infrastructure_fixture):
    rows, _, _ = infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON)
    for row in rows:
        assert set(row) == {"id", "subtype", "class", "name", "distance_m"}


def test_rows_carry_the_gers_id(infrastructure_fixture):
    """id is SELECTed, not just declared in REQUIRED_COLUMNS — it is what
    makes a row composable with the other GERS-keyed tools."""
    rows, _, _ = infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON)
    assert {r["name"]: r["id"] for r in rows} == {
        "Test Tower": "infra-tower",
        "Test Bridge": "infra-bridge",
        "Test Airport": "infra-airport",
    }


# --- empty is an answer, not an error ---------------------------------------


def test_far_away_point_returns_empty_results(infrastructure_fixture):
    rows, _, _ = infrastructure.infrastructure_at(NOWHERE_LAT, NOWHERE_LON)
    assert rows == []


# --- radius clamp -----------------------------------------------------------


def test_oversized_radius_is_clamped_and_reported(infrastructure_fixture):
    _, radius_m, _ = infrastructure.infrastructure_at(
        CENTER_LAT, CENTER_LON, radius_m=geo.MAX_QUERY_RADIUS_M * 10
    )
    assert radius_m == geo.MAX_QUERY_RADIUS_M


def test_non_finite_radius_clamps_to_zero(infrastructure_fixture):
    rows, radius_m, _ = infrastructure.infrastructure_at(
        CENTER_LAT, CENTER_LON, radius_m=float("nan")
    )
    assert radius_m == 0.0
    assert rows == []


# --- schema degrade ---------------------------------------------------------


def test_missing_class_column_degrades_to_none(tmp_path):
    path = tmp_path / "infrastructure_no_class.parquet"
    _build_infrastructure_fixture(path, include_class=False)
    infrastructure.set_data_path(str(path))
    try:
        rows, _, _ = infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON)
        assert [r["subtype"] for r in rows] == ["communication", "bridge", "airport"]
        assert all(r["class"] is None for r in rows)
        assert "class" in infrastructure.degraded_fields()
    finally:
        infrastructure.set_data_path(None)


def test_missing_geometry_raises_schema_degraded(tmp_path):
    path = tmp_path / "infrastructure.parquet"
    _build_infrastructure_fixture(path)
    no_geom_path = tmp_path / "no_geometry.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (geometry) FROM read_parquet("
        f"'{path}')) TO '{no_geom_path}' (FORMAT PARQUET)"
    )
    con.close()
    infrastructure.set_data_path(str(no_geom_path))
    try:
        with pytest.raises(overture.SchemaDegraded) as exc_info:
            infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON)
        assert "geometry" in exc_info.value.missing
    finally:
        infrastructure.set_data_path(None)


def test_cache_theme_is_a_portable_path_component():
    # cache.tile_path uses the theme string verbatim as a directory
    # component; ':' (the original separator) is illegal on Windows.
    theme = infrastructure._cache_theme()
    assert theme == "base_infrastructure"
    assert not any(c in theme for c in ':\\/'), theme


# --- server wiring ----------------------------------------------------------


def test_server_infrastructure_at_happy_path(infrastructure_fixture):
    result = server.infrastructure_at(CENTER_LAT, CENTER_LON)
    assert "error" not in result
    assert result["center"] == {"lat": CENTER_LAT, "lon": CENTER_LON}
    assert result["radius_m"] == infrastructure.DEFAULT_RADIUS_M
    assert [r["name"] for r in result["results"]] == [
        "Test Tower", "Test Bridge", "Test Airport",
    ]


def test_server_infrastructure_at_empty_is_not_an_error(infrastructure_fixture):
    result = server.infrastructure_at(NOWHERE_LAT, NOWHERE_LON)
    assert "error" not in result
    assert result["results"] == []


def test_server_infrastructure_at_rejects_out_of_range_coordinate(infrastructure_fixture):
    # #163 pattern: out-of-range (or swapped) coordinates are a bad_request
    # at the tool boundary, before any base-theme scan runs.
    result = server.infrastructure_at(lat=91.0, lon=0.0)
    assert result["error"] == "bad_request"
    result = server.infrastructure_at(lat=120.98, lon=14.60)
    assert result["error"] == "bad_request"
    assert "swap" in result["detail"]


def test_server_structured_error_on_unreachable_upstream(tmp_path):
    infrastructure.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    try:
        result = server.infrastructure_at(CENTER_LAT, CENTER_LON)
        assert result["error"] == "upstream_unavailable"
    finally:
        infrastructure.set_data_path(None)


# --- street-furniture domination (#180 sweep) -------------------------------
#
# The real base/infrastructure layer is ~50:1 street furniture: around Dam
# Square, r=500 has 1263 rows in range including 72 bridges, and the 10
# nearest are all lamps and benches. The fixture below reproduces that shape
# in miniature — 30 street lamps closer than the single bridge — so the two
# behaviours that keep "is there a bridge near here?" honest are pinned: the
# unfiltered answer says it is a slice, and a subtype filter finds the bridge.

_LAMP_COUNT = 30
_FURNITURE_BRIDGE_LAT = CENTER_LAT + 400.0 / _M_PER_DEG_LAT
_FURNITURE_FEATURES = [
    (
        f"lamp-{i}",
        f"POINT({CENTER_LON} {CENTER_LAT + (10.0 + 5.0 * i) / _M_PER_DEG_LAT})",
        "street_furniture",
        "street_lamp",
        f"Lamp {i}",
    )
    for i in range(_LAMP_COUNT)
] + [
    (
        "furniture-bridge",
        f"LINESTRING({CENTER_LON} {_FURNITURE_BRIDGE_LAT}, "
        f"{CENTER_LON + 0.002} {_FURNITURE_BRIDGE_LAT})",
        "bridge",
        "viaduct",
        "Hidden Bridge",
    )
]


@pytest.fixture
def furniture_fixture(tmp_path):
    path = tmp_path / "infrastructure_furniture.parquet"
    _build_infrastructure_fixture(path, features=_FURNITURE_FEATURES)
    infrastructure.set_data_path(str(path))
    try:
        yield path
    finally:
        infrastructure.set_data_path(None)


def test_oversized_limit_is_clamped_to_max_rows(furniture_fixture):
    """The response-size cap holds even for a direct caller asking for
    everything; the window-function total still reports the full set."""
    rows, _, total_in_range = infrastructure.infrastructure_at(
        CENTER_LAT, CENTER_LON, limit=10_000
    )
    assert len(rows) == infrastructure.MAX_ROWS
    assert total_in_range == _LAMP_COUNT + 1


def test_unfiltered_query_reports_the_true_in_range_count(furniture_fixture):
    rows, _, total_in_range = infrastructure.infrastructure_at(CENTER_LAT, CENTER_LON)
    assert len(rows) == infrastructure.DEFAULT_LIMIT
    assert all(r["class"] == "street_lamp" for r in rows)
    assert total_in_range == _LAMP_COUNT + 1


def test_subtype_filter_surfaces_the_landmark_the_lamps_were_hiding(furniture_fixture):
    rows, _, total_in_range = infrastructure.infrastructure_at(
        CENTER_LAT, CENTER_LON, subtype="bridge"
    )
    assert [r["name"] for r in rows] == ["Hidden Bridge"]
    assert total_in_range == 1


def test_class_filter_matches_the_class_column(furniture_fixture):
    rows, _, _ = infrastructure.infrastructure_at(
        CENTER_LAT, CENTER_LON, infra_class="viaduct"
    )
    assert [r["name"] for r in rows] == ["Hidden Bridge"]


def test_server_flags_the_limit_bite_with_a_note(furniture_fixture):
    result = server.infrastructure_at(CENTER_LAT, CENTER_LON)
    assert result["truncated"] is True
    assert result["total_in_range"] == _LAMP_COUNT + 1
    assert result["omitted_count"] == _LAMP_COUNT + 1 - len(result["results"])
    assert "street furniture" in result["note"]
    assert "subtype" in result["note"]


def test_server_untruncated_answer_carries_no_note(infrastructure_fixture):
    result = server.infrastructure_at(CENTER_LAT, CENTER_LON)
    assert "truncated" not in result
    assert "note" not in result
    assert "total_in_range" not in result


def test_server_subtype_filter_reaches_the_query(furniture_fixture):
    result = server.infrastructure_at(CENTER_LAT, CENTER_LON, subtype="bridge")
    assert [r["name"] for r in result["results"]] == ["Hidden Bridge"]
    assert "truncated" not in result


# --- filter wildcard escaping ----------------------------------------------

_UNDERSCORE_FEATURES = [
    (
        "under-literal",
        f"POINT({CENTER_LON} {CENTER_LAT + 50.0 / _M_PER_DEG_LAT})",
        "foo_bar",
        "foo_bar",
        "Literal Underscore",
    ),
    (
        "under-wildcard",
        f"POINT({CENTER_LON} {CENTER_LAT + 60.0 / _M_PER_DEG_LAT})",
        "fooXbar",
        "fooXbar",
        "Wildcard Match",
    ),
]


def test_filter_underscore_is_literal_not_a_wildcard(tmp_path):
    """subtype='foo_bar' must not match 'fooXbar' — Overture's values are
    snake_case, so an unescaped '_' would over-match constantly."""
    path = tmp_path / "infrastructure_underscore.parquet"
    _build_infrastructure_fixture(path, features=_UNDERSCORE_FEATURES)
    infrastructure.set_data_path(str(path))
    try:
        rows, _, _ = infrastructure.infrastructure_at(
            CENTER_LAT, CENTER_LON, subtype="foo_bar"
        )
        assert [r["name"] for r in rows] == ["Literal Underscore"]
        rows, _, _ = infrastructure.infrastructure_at(
            CENTER_LAT, CENTER_LON, infra_class="foo_bar"
        )
        assert [r["name"] for r in rows] == ["Literal Underscore"]
    finally:
        infrastructure.set_data_path(None)


# --- bbox prefilter ring (#180 sweep) ---------------------------------------

# Metres per degree of latitude on the haversine sphere the distance
# predicate uses (R=6371000), which is *not* the 111_320.0 m/deg
# geo.bbox_around converts with. A bare point placed with this constant at
# 499.9 m due north is genuinely 499.9 m away by the exact predicate, but
# falls outside the unpadded prefilter box for radius_m=500.
_SPHERE_M_PER_DEG = 6371000.0 * 3.141592653589793 / 180.0
_RING_FEATURES = [
    (
        f"ring-{int(d * 10)}",
        f"POINT({CENTER_LON} {CENTER_LAT + d / _SPHERE_M_PER_DEG})",
        "communication",
        "communication_tower",
        f"Ring Tower {d}",
    )
    for d in (499.5, 499.7, 499.9, 500.0)
]


def test_prefilter_ring_features_are_not_dropped(tmp_path):
    path = tmp_path / "infrastructure_ring.parquet"
    _build_infrastructure_fixture(path, features=_RING_FEATURES)
    infrastructure.set_data_path(str(path))
    try:
        rows, _, _ = infrastructure.infrastructure_at(
            CENTER_LAT, CENTER_LON, radius_m=500
        )
        assert [r["name"] for r in rows] == [
            "Ring Tower 499.5", "Ring Tower 499.7", "Ring Tower 499.9", "Ring Tower 500.0",
        ]
        for row in rows:
            assert row["distance_m"] == pytest.approx(float(row["name"].split()[-1]), abs=0.2)
    finally:
        infrastructure.set_data_path(None)
