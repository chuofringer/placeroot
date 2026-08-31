"""Issue #11: point -> containing admin hierarchy, against the synthetic
division_area fixture (tests/fixtures/division_areas.parquet, built by
scripts/build_fixture.py) — five nested rectangles around places.parquet's
downtown cluster, plus one unrelated polygon that must never appear."""

import duckdb
import pytest

from placeroot import divisions, overture, server

from .conftest import CENTER_LAT, CENTER_LON, DIVISION_AREAS_FIXTURE_PATH


def test_chain_is_smallest_first_and_complete():
    result = divisions.admin_lookup(CENTER_LAT, CENTER_LON)
    types = [d["type"] for d in result["chain"]]
    assert types == ["neighborhood", "locality", "county", "region", "country"]
    assert result["chain"][0]["name"] == "Downtown"
    assert result["chain"][-1]["name"] == "United Testland"


def test_every_entry_carries_a_gers_id():
    result = divisions.admin_lookup(CENTER_LAT, CENTER_LON)
    assert all(d["id"] for d in result["chain"])


def test_chain_rows_carry_iso_codes_where_the_fixture_has_them():
    # #446: every US row in the fixture nests inside the "Empire State"
    # (US-NY) box and carries that region; the country-level row has a
    # country but no region of its own.
    result = divisions.admin_lookup(CENTER_LAT, CENTER_LON)
    by_type = {d["type"]: d for d in result["chain"]}
    assert by_type["neighborhood"]["country"] == "US"
    assert by_type["neighborhood"]["region"] == "US-NY"
    assert by_type["country"]["country"] == "US"
    assert "region" not in by_type["country"]


def test_missing_country_and_region_columns_omit_the_keys(tmp_path):
    out = tmp_path / "no_iso_codes.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (country, region) FROM read_parquet("
        f"'{DIVISION_AREAS_FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out), theme="divisions")
    result = divisions.admin_lookup(CENTER_LAT, CENTER_LON)
    assert result["chain"]  # still answers, just without the ISO fields
    for entry in result["chain"]:
        assert "country" not in entry
        assert "region" not in entry
    overture.set_data_path(str(DIVISION_AREAS_FIXTURE_PATH), theme="divisions")


def test_unrelated_division_is_excluded():
    result = divisions.admin_lookup(CENTER_LAT, CENTER_LON)
    names = {d["name"] for d in result["chain"]}
    assert "Arctica" not in names


def test_point_outside_every_division_returns_empty_chain():
    result = divisions.admin_lookup(-33.0, 151.0)  # nowhere near any fixture polygon
    assert result == {"chain": []}


def test_point_in_the_unrelated_polygon_only_returns_that_one():
    result = divisions.admin_lookup(78.0, 15.0)  # inside "Arctica", nothing else
    names = [d["name"] for d in result["chain"]]
    assert names == ["Arctica"]


def test_missing_geometry_raises_schema_degraded(tmp_path):
    out = tmp_path / "missing_geometry.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (geometry) FROM read_parquet("
        f"'{DIVISION_AREAS_FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out), theme="divisions")
    with pytest.raises(overture.SchemaDegraded) as exc_info:
        divisions.admin_lookup(CENTER_LAT, CENTER_LON)
    assert "geometry" in exc_info.value.missing


def test_server_happy_path():
    result = server.admin_lookup(CENTER_LAT, CENTER_LON)
    assert "error" not in result
    assert len(result["chain"]) == 5


def test_server_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"), theme="divisions")
    result = server.admin_lookup(CENTER_LAT, CENTER_LON)
    assert result["error"] == "upstream_unavailable"


def test_server_structured_error_on_missing_geometry(tmp_path):
    out = tmp_path / "missing_geometry.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (geometry) FROM read_parquet("
        f"'{DIVISION_AREAS_FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out), theme="divisions")
    result = server.admin_lookup(CENTER_LAT, CENTER_LON)
    assert result["error"] == "schema_degraded"
    assert "geometry" in result["missing_columns"]
