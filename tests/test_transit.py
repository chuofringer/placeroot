"""Issue #453: transit_stops_near against a small synthetic base/infrastructure
fixture — a filtered view of the same theme=base/type=infrastructure dataset
infrastructure_at reads.

Builds its fixture locally via duckdb (WKT -> WKB BLOB), exactly as
tests/test_infrastructure.py does for the same theme=base type; the two
modules are meant to never diverge on the dataset override, so this reuses
infrastructure.set_data_path (see transit.py's module docstring) rather than
adding a second one.
"""

import duckdb
import pytest

from placeroot import infrastructure, overture, server, transit

from .conftest import CENTER_LAT, CENTER_LON

# A point nowhere near any fixture feature.
NOWHERE_LAT, NOWHERE_LON = 10.0, 10.0

# Roughly metres-per-degree at CENTER_LAT, used only to place fixture
# features at recognisably different distances from the query point.
_M_PER_DEG_LAT = 111_320.0


def _north(meters: float, lon_offset: float = 0.0) -> tuple[float, float]:
    return CENTER_LAT + meters / _M_PER_DEG_LAT, CENTER_LON + lon_offset


# id, (lat, lon), subtype, class, name
_BUS_NEAR = ("transit-bus-near", _north(80), "transit", "bus_stop", "Near Bus Stop")
_STATION = ("transit-station", _north(300), "transit", "railway_station", "Test Station")
_STOP_POSITION = (
    "transit-stopposition",
    _north(150, 0.001),
    "transit",
    "stop_position",
    "A Stop Position",
)
_PLATFORM = ("transit-platform", _north(200, 0.001), "transit", "platform", "A Platform")
_BIKE_PARKING = (
    "transit-bike",
    _north(120, -0.001),
    "transit",
    "bicycle_parking",
    "Bike Parking",
)
_CAR_PARKING = ("transit-parking", _north(130, -0.001), "transit", "parking", "Car Parking")
_BUS_FAR = ("transit-bus-far", _north(5000), "transit", "bus_stop", "Far Bus Stop")

# Fixture for the "default call, everything in one basket" tests: a real
# stop of each of the two nearest-worthy classes, one of each fallback
# class, one of each excluded class, and one real stop far outside the
# default radius.
_MIXED_FEATURES = [
    _BUS_NEAR,
    _STATION,
    _STOP_POSITION,
    _PLATFORM,
    _BIKE_PARKING,
    _CAR_PARKING,
    _BUS_FAR,
]

# Fixture for the fallback tests: no real stop class at all, just the two
# OSM per-geometry duplicates (plus a bike rack, to prove even the fallback
# path never returns parking/bicycle classes).
_FALLBACK_ONLY_FEATURES = [_STOP_POSITION, _PLATFORM, _BIKE_PARKING]


def _build_transit_fixture(path, features, include_class=True) -> None:
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
    for id_, (lat, lon), subtype, class_, name in features:
        wkt = f"POINT({lon} {lat})"
        # bbox comes straight from the geometry, as it does upstream.
        wkb, xmin, ymin, xmax, ymax = con.execute(f"""
            SELECT ST_AsWKB(g), ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g)
            FROM (SELECT ST_GeomFromText('{wkt}') AS g)
        """).fetchone()
        bbox = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
        values = (
            [id_, wkb, bbox, subtype, class_, {"primary": name}]
            if include_class
            else [id_, wkb, bbox, subtype, {"primary": name}]
        )
        placeholders = ", ".join(["?"] * len(values))
        con.execute(f"INSERT INTO infrastructure VALUES ({placeholders})", values)
    con.execute(f"COPY infrastructure TO '{path}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def transit_fixture(tmp_path):
    """Points infrastructure.py (and therefore transit.py) at the mixed fixture."""
    path = tmp_path / "infrastructure.parquet"
    _build_transit_fixture(path, _MIXED_FEATURES)
    infrastructure.set_data_path(str(path))
    try:
        yield path
    finally:
        infrastructure.set_data_path(None)


@pytest.fixture
def fallback_only_fixture(tmp_path):
    path = tmp_path / "infrastructure_fallback_only.parquet"
    _build_transit_fixture(path, _FALLBACK_ONLY_FEATURES)
    infrastructure.set_data_path(str(path))
    try:
        yield path
    finally:
        infrastructure.set_data_path(None)


# --- 1. default call: real stops only, nearest first -----------------------


def test_default_call_returns_only_stop_classes_nearest_first(transit_fixture):
    rows, radius_m, total_in_range, fallback_used, class_missing = transit.transit_stops_near(
        CENTER_LAT, CENTER_LON
    )
    assert radius_m == transit.DEFAULT_RADIUS_M
    assert not fallback_used
    assert not class_missing
    assert [r["name"] for r in rows] == ["Near Bus Stop", "Test Station"]
    assert [r["kind"] for r in rows] == ["bus_stop", "railway_station"]
    assert total_in_range == 2
    distances = [r["distance_m"] for r in rows]
    assert distances == sorted(distances)
    assert set(rows[0]) == {"id", "kind", "name", "distance_m"}


def test_server_default_call_excludes_platform_stopposition_bike_and_parking(transit_fixture):
    result = server.transit_stops_near(CENTER_LAT, CENTER_LON)
    assert "error" not in result
    assert result["center"] == {"lat": CENTER_LAT, "lon": CENTER_LON}
    assert result["radius_m"] == transit.DEFAULT_RADIUS_M
    names = {r["name"] for r in result["results"]}
    assert names == {"Near Bus Stop", "Test Station"}
    assert result["total_in_range"] == 2
    assert "truncated" not in result
    excluded = {"A Stop Position", "A Platform", "Bike Parking", "Car Parking", "Far Bus Stop"}
    assert not (excluded & names)


# --- 2. kind restricts to exactly that class --------------------------------


def test_kind_restricts_to_one_class(transit_fixture):
    result = server.transit_stops_near(CENTER_LAT, CENTER_LON, kind="railway_station")
    assert "error" not in result
    assert [r["name"] for r in result["results"]] == ["Test Station"]
    assert [r["kind"] for r in result["results"]] == ["railway_station"]


# --- 3. bad kind -------------------------------------------------------------


def test_unrecognized_kind_is_a_bad_request(transit_fixture):
    result = server.transit_stops_near(CENTER_LAT, CENTER_LON, kind="bogus")
    assert result["error"] == "bad_request"
    assert "bogus" in result["detail"]
    for allowed in transit.ALLOWED_KINDS:
        assert allowed in result["detail"]


# --- 4. far away is an empty answer, not an error ---------------------------


def test_far_away_point_returns_empty_results_with_a_note(transit_fixture):
    result = server.transit_stops_near(NOWHERE_LAT, NOWHERE_LON)
    assert "error" not in result
    assert result["results"] == []
    assert result["total_in_range"] == 0
    assert "note" in result
    assert "no transit stop" in result["note"]


def test_far_away_point_is_not_an_error_at_module_level(transit_fixture):
    rows, _, total_in_range, fallback_used, class_missing = transit.transit_stops_near(
        NOWHERE_LAT, NOWHERE_LON
    )
    assert rows == []
    assert total_in_range == 0
    assert not fallback_used
    assert not class_missing


# --- 5. fallback to platform/stop_position ----------------------------------


def test_fallback_returns_platform_and_stopposition_when_no_real_stop_in_range(
    fallback_only_fixture,
):
    rows, _, total_in_range, fallback_used, class_missing = transit.transit_stops_near(
        CENTER_LAT, CENTER_LON
    )
    assert fallback_used
    assert not class_missing
    assert total_in_range == 2
    assert {r["name"] for r in rows} == {"A Stop Position", "A Platform"}
    assert {r["kind"] for r in rows} == {"stop_position", "platform"}
    assert "Bike Parking" not in {r["name"] for r in rows}


def test_server_fallback_note_mentions_platform_and_stop_position(fallback_only_fixture):
    result = server.transit_stops_near(CENTER_LAT, CENTER_LON)
    assert "error" not in result
    names = {r["name"] for r in result["results"]}
    assert names == {"A Stop Position", "A Platform"}
    assert "note" in result
    assert "platform" in result["note"].lower()
    assert "stop_position" in result["note"].lower()


# --- 6. limit + truncation ---------------------------------------------------


def test_limit_truncates_and_reports_total_in_range(transit_fixture):
    result = server.transit_stops_near(CENTER_LAT, CENTER_LON, limit=1)
    assert "error" not in result
    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "Near Bus Stop"
    assert result["truncated"] is True
    assert result["total_in_range"] == 2
    assert "note" in result


# --- 7. missing class column degrades gracefully ----------------------------


def test_missing_class_column_degrades_to_empty_with_a_note(tmp_path):
    path = tmp_path / "infrastructure_no_class.parquet"
    _build_transit_fixture(path, _MIXED_FEATURES, include_class=False)
    infrastructure.set_data_path(str(path))
    try:
        rows, _, total_in_range, fallback_used, class_missing = transit.transit_stops_near(
            CENTER_LAT, CENTER_LON
        )
        assert rows == []
        assert total_in_range == 0
        assert not fallback_used
        assert class_missing

        result = server.transit_stops_near(CENTER_LAT, CENTER_LON)
        assert "error" not in result
        assert result["results"] == []
        assert "note" in result
        assert "class" in result["note"]
    finally:
        infrastructure.set_data_path(None)


# --- schema degrade (essential columns) -------------------------------------


def test_missing_geometry_raises_schema_degraded(tmp_path):
    path = tmp_path / "infrastructure.parquet"
    _build_transit_fixture(path, _MIXED_FEATURES)
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
            transit.transit_stops_near(CENTER_LAT, CENTER_LON)
        assert "geometry" in exc_info.value.missing

        result = server.transit_stops_near(CENTER_LAT, CENTER_LON)
        assert result["error"] == "schema_degraded"
    finally:
        infrastructure.set_data_path(None)


def test_server_rejects_out_of_range_coordinate(transit_fixture):
    result = server.transit_stops_near(lat=91.0, lon=0.0)
    assert result["error"] == "bad_request"
