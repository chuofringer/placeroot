import duckdb
import pytest

from placeroot import geocode, overture, release, server

from .conftest import ADDRESSES_FIXTURE_PATH, CENTER_LAT, CENTER_LON, DIVISIONS_FIXTURE_PATH


@pytest.fixture
def geocode_cache(tmp_path, monkeypatch):
    """Enables the #43 local divisions table (default fixtures otherwise run
    with PLACEROOT_CACHE=off, see conftest.offline_data) at an isolated,
    per-test cache dir.
    """
    d = tmp_path / "placeroot-cache"
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(d))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    return d


def test_exact_name_match_ranks_above_prefix_and_substring():
    results = geocode.geocode("Springfield", limit=10)
    assert len(results) == 2  # both fixture Springfields, IL and MA
    assert all(r["name"] == "Springfield" for r in results)
    assert all(r["type"] == "locality" for r in results)


def test_prefix_and_substring_ranking_orders_exact_first():
    # "Brooklyn" exact match should outrank "Downtown Brooklyn" substring match.
    results = geocode.geocode("Brooklyn", limit=10)
    names = [r["name"] for r in results]
    assert names[0] == "Brooklyn"
    assert "Downtown Brooklyn" in names
    assert names.index("Brooklyn") < names.index("Downtown Brooklyn")


def test_never_more_than_limit_results():
    results = geocode.geocode("e", limit=3)  # matches nearly everything
    assert len(results) <= 3


def test_admin_context_reflects_hierarchy_chain():
    results = geocode.geocode("Riverside", limit=5)
    assert len(results) == 1
    assert results[0]["admin_context"] == ["United States", "Illinois", "Springfield"]


def test_results_carry_gers_id_and_coordinates():
    results = geocode.geocode("Brooklyn", limit=1)
    assert results[0]["id"] == "gers-div-brooklyn"
    assert results[0]["lat"] == pytest.approx(CENTER_LAT, abs=0.01)
    assert results[0]["lon"] == pytest.approx(CENTER_LON, abs=0.01)


def test_empty_query_returns_no_results():
    assert geocode.geocode("", limit=5) == []
    assert geocode.geocode("   ", limit=5) == []


def test_no_match_returns_empty_list():
    assert geocode.geocode("Nonexistentplacexyz123", limit=5) == []


def test_falls_back_to_places_when_divisions_dont_fill_limit():
    # "Blue Bottle Roastery" only exists in the places fixture, not divisions.
    results = geocode.geocode("Blue Bottle Roastery", limit=5)
    names = [r["name"] for r in results]
    assert "Blue Bottle Roastery" in names
    match = next(r for r in results if r["name"] == "Blue Bottle Roastery")
    assert match["type"] == "place"


def test_upstream_unavailable_raises(tmp_path):
    overture.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), theme="divisions", type_="division"
    )
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"), theme="places")
    with pytest.raises(overture.UpstreamUnavailable):
        geocode.geocode("Brooklyn", limit=5)
    overture.set_data_path(str(DIVISIONS_FIXTURE_PATH), theme="divisions", type_="division")


# --- reverse_geocode ---


def test_reverse_geocode_finds_nearest_address_and_admin_chain():
    result = geocode.reverse_geocode(CENTER_LAT, CENTER_LON)
    assert result["source"] == "address"
    assert result["address"]["street"]
    assert result["address"]["number"]
    assert result["admin_context"][-1] == "Brooklyn"


def test_reverse_geocode_degrades_when_addresses_theme_missing(tmp_path):
    overture.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), theme="addresses", type_="address"
    )
    result = geocode.reverse_geocode(CENTER_LAT, CENTER_LON)
    assert result["source"] == "divisions_only"
    assert result["address"] is None
    assert "note" in result
    assert result["admin_context"][-1] == "Brooklyn"
    overture.set_data_path(str(ADDRESSES_FIXTURE_PATH), theme="addresses", type_="address")


def test_reverse_geocode_far_from_everything_has_no_admin_context():
    result = geocode.reverse_geocode(0.0, 0.0)
    assert result["source"] == "divisions_only"
    assert result["admin_context"] == []


def _fixture_missing_column(path, column, tmp_path, out_name):
    out = tmp_path / out_name
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * EXCLUDE ({column}) FROM read_parquet('{path}')) "
        f"TO '{out}' (FORMAT PARQUET)"
    )
    return out


def test_reverse_geocode_degrades_when_addresses_missing_street_column(tmp_path):
    degraded = _fixture_missing_column(
        ADDRESSES_FIXTURE_PATH, "street", tmp_path, "addresses_no_street.parquet"
    )
    overture.set_data_path(str(degraded), theme="addresses", type_="address")
    result = geocode.reverse_geocode(CENTER_LAT, CENTER_LON)
    assert result["source"] == "divisions_only"
    overture.set_data_path(str(ADDRESSES_FIXTURE_PATH), theme="addresses", type_="address")


# --- server tools ---


def test_server_geocode_tool_wraps_results():
    result = server.geocode("Springfield", limit=10)
    assert "results" in result
    assert len(result["results"]) == 2


def test_server_reverse_geocode_tool():
    result = server.reverse_geocode(CENTER_LAT, CENTER_LON)
    assert result["source"] == "address"


def test_server_geocode_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), theme="divisions", type_="division"
    )
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"), theme="places")
    result = server.geocode("Brooklyn", limit=5)
    assert result["error"] == "upstream_unavailable"
    overture.set_data_path(str(DIVISIONS_FIXTURE_PATH), theme="divisions", type_="division")


# --- #43: local divisions name table -------------------------------------


def test_materializes_local_divisions_table_on_first_call(geocode_cache):
    path = geocode._local_divisions_table_path(release.PINNED_RELEASE)
    assert not path.exists()
    results = geocode.geocode("Brooklyn", limit=5)
    assert path.exists()
    assert any(r["name"] == "Brooklyn" for r in results)


def test_local_table_is_reused_without_rematerializing(geocode_cache):
    geocode.geocode("Brooklyn", limit=5)
    path = geocode._local_divisions_table_path(release.PINNED_RELEASE)
    mtime = path.stat().st_mtime
    geocode.geocode("Springfield", limit=5)
    assert path.stat().st_mtime == mtime  # same file, not rebuilt


def test_results_are_correct_via_local_table(geocode_cache):
    results = geocode.geocode("Springfield", limit=10)
    assert len(results) == 2
    assert all(r["name"] == "Springfield" for r in results)


def test_cache_off_skips_materialization_and_still_answers(monkeypatch, tmp_path):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "placeroot-cache"))
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    path = geocode._local_divisions_table_path(release.PINNED_RELEASE)
    results = geocode.geocode("Brooklyn", limit=5)
    assert not path.exists()
    assert any(r["name"] == "Brooklyn" for r in results)


# --- #46: "City, ST" / "City, Region" parsing -----------------------------


def test_city_comma_state_abbreviation_resolves_correct_region():
    results = geocode.geocode("Springfield, IL", limit=5)
    assert results
    springfields = [r for r in results if r["name"] == "Springfield"]
    assert springfields
    assert all(r["admin_context"] == ["United States", "Illinois"] for r in springfields)


def test_city_comma_state_disambiguates_the_other_same_named_city():
    results = geocode.geocode("Springfield, MA", limit=5)
    top = next(r for r in results if r["name"] == "Springfield")
    assert top["admin_context"] == ["United States", "Massachusetts"]
    # The Illinois Springfield must not appear at all — it's out of region.
    assert not any(
        r["name"] == "Springfield" and r["admin_context"] == ["United States", "Illinois"]
        for r in results
    )


def test_city_state_still_finds_the_only_matching_region():
    results = geocode.geocode("Brooklyn, NY", limit=5)
    assert results[0]["name"] == "Brooklyn"
    assert results[0]["admin_context"] == ["United States", "New York"]


def test_city_comma_general_region_name_resolves_via_local_table(geocode_cache):
    # "Ontario" isn't a US state — this only resolves through
    # _resolve_region_from_table, which needs the local table (#43).
    results = geocode.geocode("London, Ontario", limit=5)
    assert results
    assert results[0]["name"] == "London"
    assert results[0]["admin_context"] == ["Canada", "Ontario"]


def test_city_state_abbreviation_disambiguates_from_general_region(geocode_cache):
    results = geocode.geocode("London, OH", limit=5)
    assert results
    assert results[0]["name"] == "London"
    assert results[0]["admin_context"] == ["United States", "Ohio"]


def test_unparseable_suffix_degrades_to_todays_behavior():
    # "ZZ" isn't a real state/region — the whole literal string is searched,
    # matching nothing (division names are bare, never "City, ST").
    assert geocode.geocode("Springfield, ZZ", limit=5) == []


# --- #47: prominence disambiguation ---------------------------------------


def test_population_breaks_tie_between_same_named_localities():
    results = geocode.geocode("Springfield", limit=10)
    assert len(results) == 2
    # Real-world populations: Springfield, MA > Springfield, IL.
    assert results[0]["admin_context"] == ["United States", "Massachusetts"]
    assert results[1]["admin_context"] == ["United States", "Illinois"]


def test_region_population_proxy_breaks_tie_when_no_population(geocode_cache):
    # Neither fixture "Fairview" carries a population value; the ids are
    # deliberately ordered so an id-only tiebreak would pick the wrong one —
    # this only passes if the region-population proxy (Illinois > Massachusetts
    # in the fixture) actually ran.
    results = geocode.geocode("Fairview", limit=5)
    assert len(results) == 2
    assert results[0]["admin_context"] == ["United States", "Illinois"]
    assert results[1]["admin_context"] == ["United States", "Massachusetts"]


def test_subtype_rank_proxy_outranks_hierarchy_and_region_when_no_population():
    # Neither fixture "Hilltop" carries a population value; ids are again
    # deliberately inverted, so this only passes if subtype rank
    # (locality > neighborhood) actually decided the order.
    results = geocode.geocode("Hilltop", limit=5)
    assert len(results) == 2
    assert results[0]["type"] == "locality"
    assert results[1]["type"] == "neighborhood"
