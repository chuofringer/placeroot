"""Issue #122: find_places constrained to a division's boundary polygon
(division_id) instead of a point+radius circle.

Builds its own synthetic division_area + places fixtures (rather than
reusing tests/fixtures/division_areas.parquet, which has no division_id or
bbox column and only rectangular polygons) so this can prove two things the
existing admin_lookup fixture can't:

1. True polygon containment, not a bounding-box approximation: the division
   polygon here is a square with a rectangular notch bitten out of one
   edge, and a place sits inside that notch — inside the polygon's bbox,
   but outside the polygon itself. Only ST_Contains (not the bbox
   prefilter) can tell the two apart.
2. Multi-row divisions: division_area carries multiple polygon rows per
   division (e.g. land/maritime variants) sharing one division_id. Two
   far-apart squares share DIV_MULTI here; a place inside either one must
   come back, proving find_places_in_division unions every matching row
   instead of only testing the first.
"""

import duckdb
import pytest

from placeroot import overture, server

NOTCH_WKT = "POLYGON((0 0, 0 10, 10 10, 10 0, 6 0, 6 4, 4 4, 4 0, 0 0))"
SQUARE_A_WKT = "POLYGON((100 40, 100 45, 105 45, 105 40, 100 40))"
SQUARE_B_WKT = "POLYGON((110 50, 110 55, 115 55, 115 50, 110 50))"

DIV_NOTCH = "div-notch"
DIV_MULTI = "div-multi"
DIV_UNKNOWN = "div-does-not-exist"

# Inside the notch polygon's left rectangle (lon < 4) -- must be returned.
INSIDE_LON, INSIDE_LAT = 2.0, 2.0
# Inside the polygon's bbox (0..10, 0..10) but in the bitten-out notch
# (4 <= lon <= 6, 0 <= lat <= 4) -- must NOT be returned.
NOTCH_LON, NOTCH_LAT = 5.0, 2.0
# Outside the polygon's bbox entirely -- excluded by the bbox prefilter alone.
FAR_LON, FAR_LAT = 50.0, 50.0
COFFEE_LON, COFFEE_LAT = 3.0, 3.0
BANK_LON, BANK_LAT = 2.0, 8.0
# Inside the polygon body, but low-confidence and permanently closed -- the
# only row min_confidence/operating_status can discriminate on.
FAINT_LON, FAINT_LAT = 1.0, 9.0
# Inside SQUARE_A only.
MULTI_A_LON, MULTI_A_LAT = 102.0, 42.0
# Inside SQUARE_B only -- far from SQUARE_A, provable only if both rows unioned.
MULTI_B_LON, MULTI_B_LAT = 112.0, 52.0


def _wkb(con: duckdb.DuckDBPyConnection, wkt: str) -> bytes:
    (b,) = con.execute(f"SELECT ST_AsWKB(ST_GeomFromText('{wkt}'))").fetchone()
    return b


def _place_row(
    id_, name, lon, lat, category="shop", basic_category="shop",
    operating_status="open", confidence=0.9,
):
    return (
        id_,
        {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat},
        {"primary": name},
        {"primary": category, "alternates": []},
        basic_category,
        operating_status,
        confidence,
        [], [], [], [], None, [],
    )


@pytest.fixture
def polygon_fixtures(tmp_path):
    """Points find overture/divisions at a synthetic non-circular division
    polygon (DIV_NOTCH) and a multi-row union case (DIV_MULTI)."""
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    division_rows = [
        ("area-notch-1", {"primary": "Notch Division"}, "neighborhood",
         _wkb(con, NOTCH_WKT), DIV_NOTCH),
        ("area-multi-1", {"primary": "Multi Division (land)"}, "locality",
         _wkb(con, SQUARE_A_WKT), DIV_MULTI),
        ("area-multi-2", {"primary": "Multi Division (maritime)"}, "locality",
         _wkb(con, SQUARE_B_WKT), DIV_MULTI),
    ]
    con.execute("""
        CREATE TABLE division_areas (
            id VARCHAR,
            names STRUCT("primary" VARCHAR),
            subtype VARCHAR,
            geometry BLOB,
            division_id VARCHAR
        )
    """)
    con.executemany("INSERT INTO division_areas VALUES (?, ?, ?, ?, ?)", division_rows)
    division_path = tmp_path / "division_areas.parquet"
    con.execute(f"COPY division_areas TO '{division_path}' (FORMAT PARQUET)")

    place_rows = [
        _place_row("place-inside", "Inside Place", INSIDE_LON, INSIDE_LAT),
        _place_row("place-notch", "Notch Place", NOTCH_LON, NOTCH_LAT),
        _place_row("place-far", "Outside Bbox Place", FAR_LON, FAR_LAT),
        _place_row("place-coffee", "Inside Coffee", COFFEE_LON, COFFEE_LAT,
                    category="coffee_shop", basic_category="coffee_shop"),
        _place_row("place-bank", "Inside Bank", BANK_LON, BANK_LAT,
                    category="bank", basic_category="bank"),
        _place_row("place-faint", "Faint Place", FAINT_LON, FAINT_LAT,
                    operating_status="closed_permanently", confidence=0.2),
        _place_row("place-multi-a", "Multi A", MULTI_A_LON, MULTI_A_LAT),
        _place_row("place-multi-b", "Multi B", MULTI_B_LON, MULTI_B_LAT),
    ]
    con.execute("""
        CREATE TABLE places (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            names STRUCT("primary" VARCHAR),
            taxonomy STRUCT("primary" VARCHAR, alternates VARCHAR[]),
            basic_category VARCHAR,
            operating_status VARCHAR,
            confidence DOUBLE,
            addresses STRUCT(
                freeform VARCHAR, locality VARCHAR, region VARCHAR,
                postcode VARCHAR, country VARCHAR
            )[],
            websites VARCHAR[],
            phones VARCHAR[],
            socials VARCHAR[],
            brand STRUCT(names STRUCT("primary" VARCHAR)),
            sources STRUCT(dataset VARCHAR, record_id VARCHAR)[]
        )
    """)
    con.executemany(
        "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", place_rows
    )
    places_path = tmp_path / "places.parquet"
    con.execute(f"COPY places TO '{places_path}' (FORMAT PARQUET)")

    overture.set_data_path(str(places_path))
    overture.set_data_path(str(division_path), theme="divisions")
    yield


def test_notch_excludes_bbox_corner_but_includes_polygon_body(polygon_fixtures):
    rows = overture.find_places_in_division(DIV_NOTCH)
    names = {r["name"] for r in rows}
    assert "Inside Place" in names
    assert "Inside Coffee" in names
    assert "Inside Bank" in names
    # In the polygon's bbox, but bitten out of the polygon itself.
    assert "Notch Place" not in names
    # Outside the polygon's bbox entirely.
    assert "Outside Bbox Place" not in names


def test_multi_row_division_unions_both_polygon_variants(polygon_fixtures):
    """A place inside EITHER of the two rows sharing DIV_MULTI's division_id
    must come back -- proves ST_Union_Agg merges every row, not just the
    first (smallest-area) one the way admin_lookup's dedup does."""
    rows = overture.find_places_in_division(DIV_MULTI)
    names = {r["name"] for r in rows}
    assert names == {"Multi A", "Multi B"}


def test_category_filter_composes_with_division_id(polygon_fixtures):
    rows = overture.find_places_in_division(DIV_NOTCH, category="coffee_shop")
    names = {r["name"] for r in rows}
    assert names == {"Inside Coffee"}


def test_unknown_division_id_returns_none(polygon_fixtures):
    assert overture.find_places_in_division(DIV_UNKNOWN) is None


def test_server_not_found_for_unknown_division_id(polygon_fixtures):
    result = server.find_places(division_id=DIV_UNKNOWN)
    assert result["error"] == "not_found"


def test_server_division_id_path_matches_query_layer(polygon_fixtures):
    result = server.find_places(division_id=DIV_NOTCH)
    assert "error" not in result
    names = {r["name"] for r in result["results"]}
    assert "Inside Place" in names
    assert "Notch Place" not in names


def test_server_rejects_both_lat_lon_and_division_id():
    result = server.find_places(lat=1.0, lon=1.0, division_id=DIV_NOTCH)
    assert result["error"] == "bad_request"


def test_server_rejects_neither_point_nor_division_id():
    result = server.find_places()
    assert result["error"] == "bad_request"


def test_server_rejects_lat_without_lon():
    result = server.find_places(lat=1.0)
    assert result["error"] == "bad_request"


def test_min_confidence_composes_with_division_id(polygon_fixtures):
    """The find_places tool exposes one signature for both modes, so a
    filter passed with division_id has to actually apply — silently
    ignoring it on the polygon path would be worse than rejecting it."""
    unfiltered = {r["name"] for r in overture.find_places_in_division(DIV_NOTCH)}
    assert "Faint Place" in unfiltered

    rows = overture.find_places_in_division(DIV_NOTCH, min_confidence=0.5)
    names = {r["name"] for r in rows}
    assert "Faint Place" not in names
    assert "Inside Place" in names


def test_operating_status_composes_with_division_id(polygon_fixtures):
    rows = overture.find_places_in_division(DIV_NOTCH, operating_status="permanently closed")
    assert {r["name"] for r in rows} == {"Faint Place"}


def test_server_division_id_honors_min_confidence(polygon_fixtures):
    result = server.find_places(division_id=DIV_NOTCH, min_confidence=0.5)
    assert "error" not in result
    assert "Faint Place" not in {r["name"] for r in result["results"]}


def test_server_division_id_rejects_bad_filter_values(polygon_fixtures):
    assert server.find_places(division_id=DIV_NOTCH, min_confidence=1.5)["error"] == "bad_request"
    assert (
        server.find_places(division_id=DIV_NOTCH, operating_status="banana")["error"]
        == "bad_request"
    )
