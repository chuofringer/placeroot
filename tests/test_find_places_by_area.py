"""Issue #123: find_places(area="...") — free-text area name -> division polygon.

Two groups of tests here:

1. Resolution (geocode.resolve_area) against the committed divisions
   fixture, which already contains the two shapes that matter: a uniquely
   named division ("Brooklyn") and a genuinely ambiguous one ("London",
   which exists in both Ohio and Ontario with no population on either row,
   so geocode's population tiebreak can't separate them).

2. Equivalence: that find_places(area=...) returns exactly what
   find_places(division_id=...) returns for the resolved id. That needs a
   division (for name resolution) and a division_area (for the polygon)
   linked by division_id, plus places inside the polygon — the committed
   division_areas fixture carries no division_id, so this builds a small
   synthetic set the same way test_find_places_in_area.py does.
"""

import duckdb
import pytest

from placeroot import geocode, overture, server
from placeroot.errors import AmbiguousArea

AREA_WKT = "POLYGON((0 0, 0 10, 10 10, 10 0, 0 0))"
AREA_DIVISION_ID = "gers-div-notchville"

INSIDE_LON, INSIDE_LAT = 2.0, 2.0
OUTSIDE_LON, OUTSIDE_LAT = 50.0, 50.0


# --- resolution against the committed fixture ------------------------------


def test_unique_area_name_resolves_to_its_division():
    resolved = geocode.resolve_area("Brooklyn")
    assert resolved["division_id"] == "gers-div-brooklyn"
    assert resolved["name"] == "Brooklyn"
    # admin_context is the *containing* chain (self excluded), which is what
    # disambiguates two same-named divisions for the caller.
    assert resolved["admin_context"] == ["United States", "New York"]


def test_ambiguous_area_name_raises_with_candidates():
    """Both fixture Londons (US-OH, CA-ON) are population-less localities,
    so geocode's prominence tiebreak genuinely can't choose — that must
    surface as candidates, not an arbitrary pick."""
    with pytest.raises(AmbiguousArea) as excinfo:
        geocode.resolve_area("London")
    ids = {c["division_id"] for c in excinfo.value.candidates}
    assert ids == {"gers-div-london-oh", "gers-div-london-on"}
    assert all("admin_context" in c for c in excinfo.value.candidates)


def test_area_with_a_prominence_winner_is_not_ambiguous():
    """Two same-named divisions are only ambiguous when nothing separates
    them. Both fixture Springfields carry a population, so one wins outright
    and this must resolve rather than erroring."""
    resolved = geocode.resolve_area("Springfield")
    assert resolved["division_id"] in {"gers-div-springfield-il", "gers-div-springfield-ma"}


def test_unresolvable_area_returns_none():
    assert geocode.resolve_area("Definitely Not A Real Place XYZ") is None


def test_empty_area_returns_none():
    assert geocode.resolve_area("   ") is None


# --- server-level behavior on the committed fixture ------------------------


def test_server_ambiguous_area_returns_structured_error():
    result = server.find_places(area="London")
    assert result["error"] == "ambiguous_area"
    ids = {c["division_id"] for c in result["candidates"]}
    assert ids == {"gers-div-london-oh", "gers-div-london-on"}


def test_server_unresolvable_area_is_not_found_not_empty_results():
    """An unresolvable name must not read as 'this area has no such places'."""
    result = server.find_places(area="Definitely Not A Real Place XYZ")
    assert result["error"] == "not_found"
    assert "results" not in result
    assert result["detail"] == "no division matched area 'Definitely Not A Real Place XYZ'"


def test_server_unknown_division_id_echoes_the_offending_value():
    """Issue #278: the division_id sibling of the area not_found error must
    also echo the value that failed to match, not a generic message."""
    result = server.find_places(division_id="gers-div-does-not-exist")
    assert result["error"] == "not_found"
    assert result["detail"] == "no division matched division_id 'gers-div-does-not-exist'"


def test_server_rejects_area_combined_with_point():
    result = server.find_places(lat=1.0, lon=1.0, area="Brooklyn")
    assert result["error"] == "bad_request"


def test_server_rejects_area_combined_with_division_id():
    result = server.find_places(area="Brooklyn", division_id="gers-div-brooklyn")
    assert result["error"] == "bad_request"


def test_server_still_rejects_no_mode_at_all():
    assert server.find_places()["error"] == "bad_request"


# --- equivalence with the division_id path ---------------------------------


def _wkb(con: duckdb.DuckDBPyConnection, wkt: str) -> bytes:
    (b,) = con.execute(f"SELECT ST_AsWKB(ST_GeomFromText('{wkt}'))").fetchone()
    return b


@pytest.fixture
def linked_area_fixtures(tmp_path):
    """A division (resolvable by name) linked by division_id to a
    division_area (the polygon), plus places inside and outside it."""
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    con.execute("""
        CREATE TABLE divisions (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            names STRUCT("primary" VARCHAR),
            subtype VARCHAR,
            country VARCHAR,
            region VARCHAR,
            hierarchies STRUCT("name" VARCHAR)[][],
            population BIGINT
        )
    """)
    con.execute(
        "INSERT INTO divisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            AREA_DIVISION_ID,
            {"xmin": 0.0, "ymin": 0.0, "xmax": 10.0, "ymax": 10.0},
            {"primary": "Notchville"},
            "locality",
            "US",
            "US-XX",
            [[{"name": "Notchville"}]],
            5000,
        ],
    )
    divisions_path = tmp_path / "divisions.parquet"
    con.execute(f"COPY divisions TO '{divisions_path}' (FORMAT PARQUET)")

    con.execute("""
        CREATE TABLE division_areas (
            id VARCHAR,
            names STRUCT("primary" VARCHAR),
            subtype VARCHAR,
            geometry BLOB,
            division_id VARCHAR
        )
    """)
    con.execute(
        "INSERT INTO division_areas VALUES (?, ?, ?, ?, ?)",
        ["area-notchville-1", {"primary": "Notchville"}, "locality",
         _wkb(con, AREA_WKT), AREA_DIVISION_ID],
    )
    division_areas_path = tmp_path / "division_areas.parquet"
    con.execute(f"COPY division_areas TO '{division_areas_path}' (FORMAT PARQUET)")

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
        "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("place-in", {"xmin": INSIDE_LON, "ymin": INSIDE_LAT,
                          "xmax": INSIDE_LON, "ymax": INSIDE_LAT},
             {"primary": "Inside Cafe"}, {"primary": "coffee_shop", "alternates": []},
             "coffee_shop", "open", 0.9, [], [], [], [], None, []),
            ("place-out", {"xmin": OUTSIDE_LON, "ymin": OUTSIDE_LAT,
                           "xmax": OUTSIDE_LON, "ymax": OUTSIDE_LAT},
             {"primary": "Outside Cafe"}, {"primary": "coffee_shop", "alternates": []},
             "coffee_shop", "open", 0.9, [], [], [], [], None, []),
        ],
    )
    places_path = tmp_path / "places.parquet"
    con.execute(f"COPY places TO '{places_path}' (FORMAT PARQUET)")

    overture.set_data_path(str(places_path))
    overture.set_data_path(str(division_areas_path), theme="divisions")
    overture.set_data_path(str(divisions_path), theme="divisions", type_="division")
    yield


def test_area_matches_the_division_id_path_exactly(linked_area_fixtures):
    """The acceptance criterion: area= is sugar over division_id=, not a
    second search path that could drift from it."""
    by_area = server.find_places(area="Notchville")
    by_id = server.find_places(division_id=AREA_DIVISION_ID)

    assert "error" not in by_area
    assert by_area["results"] == by_id["results"]
    assert {r["name"] for r in by_area["results"]} == {"Inside Cafe"}


def test_area_echoes_which_division_it_resolved_to(linked_area_fixtures):
    result = server.find_places(area="Notchville")
    assert result["area"]["division_id"] == AREA_DIVISION_ID
    assert result["area"]["name"] == "Notchville"


def test_division_id_path_does_not_echo_an_area(linked_area_fixtures):
    """Only the area path resolved a name, so only it reports one."""
    assert "area" not in server.find_places(division_id=AREA_DIVISION_ID)


def test_filters_compose_with_area(linked_area_fixtures):
    """area delegates to the polygon path, so it inherits every filter that
    path already honors."""
    assert server.find_places(area="Notchville", category="coffee_shop")["results"]
    empty = server.find_places(area="Notchville", category="bank")
    assert empty["results"] == []
    # The unknown-category hint still fires on the area path.
    assert "search_categories" in empty["note"]


def test_server_rejects_a_blank_area():
    """A blank area is a malformed call, not an unresolvable place."""
    assert server.find_places(area="   ")["error"] == "bad_request"


def test_resolve_area_skips_divisions_without_an_id(monkeypatch):
    """A degraded dataset can yield a division row with no id; it can't be
    passed to the polygon search, so it must not become the chosen area."""
    monkeypatch.setattr(
        geocode, "geocode",
        lambda q, limit=None: [
            {"id": None, "name": "Ghost", "type": "locality",
             "admin_context": [], "rank_score": 0.99, "lat": 0.0, "lon": 0.0},
            {"id": "gers-real", "name": "Ghost", "type": "locality",
             "admin_context": ["Somewhere"], "rank_score": 0.5, "lat": 0.0, "lon": 0.0},
        ],
    )
    assert geocode.resolve_area("Ghost")["division_id"] == "gers-real"
