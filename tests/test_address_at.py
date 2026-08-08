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

from .conftest import (
    ADDRESSES_FIXTURE_PATH,
    CENTER_LAT,
    CENTER_LON,
    DIVISION_AREAS_FIXTURE_PATH,
    DIVISIONS_FIXTURE_PATH,
)

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
    # The country label comes from the polygon that *contains* the point in
    # division_areas.parquet, which build_fixture.py names "United Testland".
    assert "United Testland (US)" in note


def test_unknown_country_note_when_no_division_identifies_the_point():
    """Null Island: no fixture division within any lookup radius."""
    result = addresses.address_at(0.0, 0.0)
    assert result["results"] == []
    assert "could not be checked" in result["note"]


# --- how the country behind the note is resolved ---
#
# The reviewer's shape for the border bug: a *foreign* division point sits
# nearer the query than any domestic one, while the polygon that actually
# contains the query is domestic. Nearest-division answers GB and reports the
# coordinate as uncovered; containment answers US and reports it as covered.

BORDER_LAT, BORDER_LON = 40.75, -73.50  # far from the address grid: no rows


def _write_border_divisions(tmp_path):
    """(division_area path, division path) for the nearest-vs-containing case.

    One US country polygon covering BORDER_LAT/LON; two division *points* —
    a GB one 2.2 km south (the nearest label) and a US one 5.5 km north.
    """
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    areas = tmp_path / "border_division_areas.parquet"
    divisions = tmp_path / "border_divisions.parquet"
    con.execute(f"""
        COPY (
            SELECT 'gers-div-area-us' AS id,
                   {{'primary': 'United States'}} AS names,
                   'country' AS subtype,
                   'US' AS country,
                   {{'xmin': -73.8, 'ymin': 40.5, 'xmax': -73.2, 'ymax': 41.0}} AS bbox,
                   ST_AsWKB(ST_GeomFromText(
                       'POLYGON((-73.8 40.5, -73.2 40.5, -73.2 41.0, -73.8 41.0, -73.8 40.5))'
                   )) AS geometry
        ) TO '{areas}' (FORMAT PARQUET)
    """)
    con.execute(f"""
        COPY (
            SELECT * FROM (VALUES
                ('gers-div-gb-near', {{'xmin': -73.5, 'ymin': 40.73,
                                       'xmax': -73.5, 'ymax': 40.73}},
                 'GB', [[{{'name': 'United Kingdom'}}]]),
                ('gers-div-us-far',  {{'xmin': -73.5, 'ymin': 40.80,
                                       'xmax': -73.5, 'ymax': 40.80}},
                 'US', [[{{'name': 'United States'}}]])
            ) AS t(id, bbox, country, hierarchies)
        ) TO '{divisions}' (FORMAT PARQUET)
    """)
    return areas, divisions


def _point_divisions_at(areas, divisions):
    # The bare theme override is what type=division_area lookups resolve to;
    # the per-type one is what the nearest-division fallback reads.
    overture.set_data_path(str(areas), theme="divisions")
    overture.set_data_path(str(divisions), theme="divisions", type_="division")
    overture.clear_division_geometry_cache()


def test_country_comes_from_the_containing_polygon_not_the_nearest_label(tmp_path):
    """Regression: a nearer foreign division must not decide the country.

    Pre-fix this resolved GB (2.2 km) over US (5.5 km) and reported a covered
    coordinate as having no Overture address coverage at all.
    """
    _point_divisions_at(*_write_border_divisions(tmp_path))
    country = addresses._country_at(BORDER_LAT, BORDER_LON)
    assert country.status == addresses.RESOLVED
    assert country.code == "US"
    assert country.name == "United States"


def test_border_coordinate_is_reported_as_covered_not_uncovered(tmp_path):
    """The same case end to end: the note must not claim the theme has no data."""
    _point_divisions_at(*_write_border_divisions(tmp_path))
    result = addresses.address_at(BORDER_LAT, BORDER_LON)
    assert result["results"] == []
    note = result["note"]
    assert "no Overture address coverage" not in note
    assert "United States (US) is covered by" in note


def test_nearest_division_is_still_the_fallback_when_nothing_contains_the_point(tmp_path):
    """Containment can legitimately find nothing (offshore); the label then wins."""
    _areas, divisions = _write_border_divisions(tmp_path)
    empty_areas = tmp_path / "no_areas.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_parquet('{_areas}') WHERE false) "
        f"TO '{empty_areas}' (FORMAT PARQUET)"
    )
    _point_divisions_at(empty_areas, divisions)
    country = addresses._country_at(BORDER_LAT, BORDER_LON)
    assert country.status == addresses.RESOLVED
    assert country.code == "GB"


# --- a failed lookup is not a fact about the data ---


def test_failed_country_lookup_is_worded_as_a_lookup_failure(tmp_path):
    missing = tmp_path / "gone" / "*.parquet"
    _point_divisions_at(missing, missing)
    result = addresses.address_at(UNCOVERED_LAT, UNCOVERED_LON)
    assert result["results"] == []
    note = result["note"]
    assert "did not complete" in note
    assert "retrying may resolve it" in note
    # The claim it must NOT make: that the dataset has no division here.
    assert "no division in the active dataset" not in note
    assert "no Overture address coverage" not in note


def test_failed_country_lookup_status_is_distinct_from_not_found(tmp_path):
    """A scan that errors reports lookup_failed; empty data reports not_found."""
    missing = tmp_path / "gone" / "*.parquet"
    _point_divisions_at(missing, missing)
    assert addresses._country_at(CENTER_LAT, CENTER_LON).status == addresses.LOOKUP_FAILED
    # Null Island against the real fixtures: nothing contains it, nothing is near.
    _point_divisions_at(DIVISION_AREAS_FIXTURE_PATH, DIVISIONS_FIXTURE_PATH)
    assert addresses._country_at(0.0, 0.0).status == addresses.NOT_FOUND


def test_a_containment_failure_does_not_become_a_not_found_verdict(tmp_path, monkeypatch):
    """Containment broken + fallback finds nothing must stay lookup_failed."""
    monkeypatch.setattr(
        addresses, "_country_by_containment", lambda lat, lon: addresses.Country(
            addresses.LOOKUP_FAILED
        )
    )
    assert addresses._country_at(0.0, 0.0).status == addresses.LOOKUP_FAILED


# --- scan count ---


def _count_address_scans(monkeypatch):
    calls = []
    original = addresses._scan_addresses

    def counting(*args, **kwargs):
        calls.append(args[3:5])
        return original(*args, **kwargs)

    monkeypatch.setattr(addresses, "_scan_addresses", counting)
    return calls


def test_uncovered_country_stops_after_the_first_address_radius(monkeypatch):
    """Widening cannot find data the theme does not carry for this country."""
    calls = _count_address_scans(monkeypatch)
    result = addresses.address_at(UNCOVERED_LAT, UNCOVERED_LON)
    assert "no Overture address coverage" in result["note"]
    assert len(calls) == 1, f"expected one address scan, got {calls}"


def test_covered_country_still_widens_through_every_radius(monkeypatch):
    calls = _count_address_scans(monkeypatch)
    addresses.address_at(COVERED_BUT_EMPTY_LAT, COVERED_BUT_EMPTY_LON)
    assert len(calls) == len(addresses.SEARCH_RADII_M)


def test_a_populated_coordinate_needs_only_the_narrowest_radius(monkeypatch):
    calls = _count_address_scans(monkeypatch)
    addresses.address_at(CENTER_LAT, CENTER_LON)
    assert len(calls) == 1


# --- dependent territories ---


def test_territories_resolve_coverage_through_their_parent_feed():
    """Martinique's address points are filed under FR, so MQ is covered."""
    for territory in ("MQ", "GP", "RE", "GF", "YT", "NC", "PF", "PM", "BL", "MF"):
        assert addresses._TERRITORY_PARENT[territory] == "FR"
        assert addresses._is_covered(territory)
    assert addresses._is_covered("AX")  # Aland, filed under FI
    assert addresses._is_covered("NF")  # Norfolk Island, filed under AU


def test_territories_without_address_data_are_not_claimed_as_covered():
    """Verified live against 2026-07-22.0: zero address rows in any of these."""
    for territory in ("PR", "VI", "GU", "MP", "AS", "AW", "CW", "BQ"):
        assert territory not in addresses._TERRITORY_PARENT
        assert not addresses._is_covered(territory)


def test_a_territory_note_names_the_territory_and_the_parent_feed():
    note = addresses._coverage_note(
        addresses.Country(addresses.RESOLVED, "MQ", "Martinique")
    )
    assert "Martinique (MQ) is covered" in note
    assert "carried under FR" in note


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
