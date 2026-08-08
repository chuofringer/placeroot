"""address_at: nearest Overture address points to a coordinate (issue #188).

Runs against the committed fixtures (tests/fixtures/addresses.parquet, built
by scripts/build_geocode_fixture.py): a 300-point grid of US addresses around
the fixture centre, a GB division with no address points anywhere near it
(the uncovered-country path), and two deliberately awkward rows at the centre
— a non-numeric house number with a unit, and a neighbour carrying no
postcode/postal_city/address_levels at all.
"""

import duckdb
import pytest

from placeroot import addresses, overture, server

from .conftest import ADDRESSES_FIXTURE_PATH, CENTER_LAT, CENTER_LON

# scripts/build_geocode_fixture.py's UNCOVERED_LAT/LON: a GB coordinate, in a
# country that is not in addresses.COVERED_COUNTRIES.
UNCOVERED_LAT, UNCOVERED_LON = 51.5000, -0.1900

# Springfield, IL — a fixture division in a *covered* country (US) with no
# address point within any search radius.
COVERED_BUT_EMPTY_LAT, COVERED_BUT_EMPTY_LON = 39.78, -89.65


def _fixture_missing_column(path, column, tmp_path, out_name):
    out = tmp_path / out_name
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * EXCLUDE ({column}) FROM read_parquet('{path}')) "
        f"TO '{out}' (FORMAT PARQUET)"
    )
    return out


def _point_at(path):
    overture.set_data_path(str(path), theme="addresses", type_="address")


@pytest.fixture
def restore_addresses_path():
    yield
    _point_at(ADDRESSES_FIXTURE_PATH)


# --- the covered, populated case ---


def test_returns_nearest_addresses_ranked_by_distance():
    result = addresses.address_at(CENTER_LAT, CENTER_LON)
    rows = result["results"]
    assert rows, "the fixture grid sits on the centre; something must be found"
    assert len(rows) == addresses.DEFAULT_LIMIT
    distances = [r["distance_m"] for r in rows]
    assert distances == sorted(distances)
    assert distances[0] == 0.0
    assert "note" not in result


def test_rows_carry_the_documented_address_fields():
    rows = addresses.address_at(CENTER_LAT, CENTER_LON, limit=5)["results"]
    nearest = rows[0]
    assert nearest["street"] == "Main St"
    assert nearest["country"] == "US"
    assert nearest["postcode"] == "11201"
    assert nearest["postal_city"] == "Brooklyn"
    assert nearest["address_levels"] == ["NY"]
    assert nearest["unit"] == "Apt 3"


def test_non_numeric_house_number_survives_verbatim():
    """Overture's `number` is a string; "74B" must not be parsed or dropped."""
    rows = addresses.address_at(CENTER_LAT, CENTER_LON, limit=5)["results"]
    assert rows[0]["number"] == "74B"
    assert all(isinstance(r["number"], str) for r in rows)


def test_optional_fields_are_omitted_rather_than_emitted_as_nulls():
    rows = addresses.address_at(CENTER_LAT, CENTER_LON, limit=5)["results"]
    # gers-addr-00151: the centre's immediate neighbour, built with no
    # postcode/postal_city/address_levels and no unit.
    sparse = [r for r in rows if r["street"] == "Oak Ave" and r["number"] == "251"]
    assert sparse, f"expected the sparse fixture row among {rows}"
    row = sparse[0]
    for field in ("postcode", "postal_city", "address_levels", "unit"):
        assert field not in row
    # The non-optional fields are still there.
    assert row["country"] == "US"
    assert row["distance_m"] > 0


def test_no_address_id_is_ever_returned():
    """Overture address ids are not GERS-stable, so none is handed out."""
    rows = addresses.address_at(CENTER_LAT, CENTER_LON, limit=5)["results"]
    for row in rows:
        assert "id" not in row
        assert not any("id" in key for key in row)


# --- limit ---


def test_limit_is_capped_at_five():
    rows = addresses.address_at(CENTER_LAT, CENTER_LON, limit=50)["results"]
    assert len(rows) == addresses.MAX_LIMIT == 5


def test_limit_below_one_still_returns_one():
    assert len(addresses.address_at(CENTER_LAT, CENTER_LON, limit=0)["results"]) == 1


def test_limit_is_honored_between_the_bounds():
    assert len(addresses.address_at(CENTER_LAT, CENTER_LON, limit=2)["results"]) == 2


# --- coverage ---


def test_uncovered_country_returns_a_structured_empty_result_with_a_note():
    result = addresses.address_at(UNCOVERED_LAT, UNCOVERED_LON)
    assert result["results"] == []
    assert "error" not in result
    note = result["note"]
    assert "no Overture address coverage" in note
    assert "United Kingdom" in note and "GB" in note


def test_uncovered_country_note_says_no_data_not_no_addresses():
    note = addresses.address_at(UNCOVERED_LAT, UNCOVERED_LON)["note"]
    assert "no data, not no addresses" in note


def test_covered_country_with_nothing_nearby_says_so_differently():
    result = addresses.address_at(COVERED_BUT_EMPTY_LAT, COVERED_BUT_EMPTY_LON)
    assert result["results"] == []
    note = result["note"]
    assert "no Overture address coverage" not in note
    assert "covered by" in note
    assert "United States" in note


def test_unknown_country_note_when_no_division_identifies_the_point():
    """Null Island: no fixture division within any lookup radius."""
    result = addresses.address_at(0.0, 0.0)
    assert result["results"] == []
    assert "could not be checked" in result["note"]


def test_covered_countries_matches_overtures_published_count():
    assert len(addresses.COVERED_COUNTRIES) == 39
    for uncovered in ("GB", "IE", "IN", "CN", "KR", "RU", "ZA", "NG", "AR", "PE"):
        assert uncovered not in addresses.COVERED_COUNTRIES
    for covered in ("US", "DE", "FR", "JP", "BR", "MX", "CL", "CO", "UY"):
        assert covered in addresses.COVERED_COUNTRIES


# --- degraded / unavailable datasets ---


def test_missing_street_column_is_a_schema_error(tmp_path, restore_addresses_path):
    degraded = _fixture_missing_column(
        ADDRESSES_FIXTURE_PATH, "street", tmp_path, "addresses_no_street.parquet"
    )
    _point_at(degraded)
    with pytest.raises(overture.SchemaDegraded) as excinfo:
        addresses.address_at(CENTER_LAT, CENTER_LON)
    assert "street" in excinfo.value.missing


def test_missing_bbox_column_is_a_schema_error(tmp_path, restore_addresses_path):
    degraded = _fixture_missing_column(
        ADDRESSES_FIXTURE_PATH, "bbox", tmp_path, "addresses_no_bbox.parquet"
    )
    _point_at(degraded)
    with pytest.raises(overture.SchemaDegraded):
        addresses.address_at(CENTER_LAT, CENTER_LON)


def test_missing_optional_column_degrades_to_null_and_is_reported(
    tmp_path, restore_addresses_path
):
    degraded = _fixture_missing_column(
        ADDRESSES_FIXTURE_PATH, "postal_city", tmp_path, "addresses_no_postal_city.parquet"
    )
    _point_at(degraded)
    result = addresses.address_at(CENTER_LAT, CENTER_LON)
    assert result["results"], "a missing optional column must not empty the answer"
    assert result["degraded_fields"] == ["postal_city"]
    assert all("postal_city" not in row for row in result["results"])
    assert result["results"][0]["street"] == "Main St"


def test_missing_address_levels_column_degrades(tmp_path, restore_addresses_path):
    """address_levels is the one column with a transform applied to it."""
    degraded = _fixture_missing_column(
        ADDRESSES_FIXTURE_PATH, "address_levels", tmp_path, "addresses_no_levels.parquet"
    )
    _point_at(degraded)
    result = addresses.address_at(CENTER_LAT, CENTER_LON)
    assert result["degraded_fields"] == ["address_levels"]
    assert all("address_levels" not in row for row in result["results"])


def test_upstream_outage_raises_upstream_unavailable(tmp_path, restore_addresses_path):
    _point_at(tmp_path / "does-not-exist" / "*.parquet")
    with pytest.raises(overture.UpstreamUnavailable):
        addresses.address_at(CENTER_LAT, CENTER_LON)


# --- server tool ---


def test_server_tool_returns_budgeted_results():
    result = server.address_at(CENTER_LAT, CENTER_LON)
    assert "error" not in result
    assert len(result["results"]) == addresses.DEFAULT_LIMIT
    assert result["results"][0]["street"] == "Main St"


def test_server_tool_rejects_out_of_range_coordinates():
    result = server.address_at(91.0, 0.0)
    assert result["error"] == "bad_request"
    assert "lat" in result["detail"]


def test_server_tool_maps_an_outage_to_a_structured_error(tmp_path, restore_addresses_path):
    _point_at(tmp_path / "does-not-exist" / "*.parquet")
    result = server.address_at(CENTER_LAT, CENTER_LON)
    assert result["error"] == "upstream_unavailable"
    assert result["retry_advised"] is True


def test_server_tool_maps_a_degraded_schema_to_a_structured_error(
    tmp_path, restore_addresses_path
):
    degraded = _fixture_missing_column(
        ADDRESSES_FIXTURE_PATH, "street", tmp_path, "addresses_no_street.parquet"
    )
    _point_at(degraded)
    result = server.address_at(CENTER_LAT, CENTER_LON)
    assert result["error"] == "schema_degraded"
    assert "street" in result["missing_columns"]


def test_server_tool_uncovered_country_is_not_an_error():
    result = server.address_at(UNCOVERED_LAT, UNCOVERED_LON)
    assert "error" not in result
    assert result["results"] == []
    assert "no Overture address coverage" in result["note"]


def test_reverse_geocode_still_works_against_the_widened_fixture():
    """The addresses fixture grew columns for #188; its original consumer
    must be unaffected."""
    result = server.reverse_geocode(CENTER_LAT, CENTER_LON)
    assert result["source"] == "address"
    assert result["address"]["street"] == "Main St"
