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


# --- #83: places fallback is bounded to an anchor's vicinity ---------------


def test_places_fallback_bounded_via_trailing_division_word():
    # "Blue Bottle Roastery" alone never matches a division; appending the
    # real fixture division "Brooklyn" gives _fallback_anchor something to
    # resolve, bounding the places search to Brooklyn's vicinity (#83) —
    # this must still find the (nearby) fixture place.
    results = geocode.geocode("Blue Bottle Roastery Brooklyn", limit=5)
    names = [r["name"] for r in results]
    assert "Blue Bottle Roastery" in names


def test_places_fallback_excludes_matches_outside_anchor_vicinity():
    # "Arctic Place 0" is a real fixture place, ~4000km from the fixture's
    # "Brooklyn" division. Before #83, the fallback scanned the places
    # theme unconstrained and would have found it by name alone; the
    # anchor-bounded search must exclude it as out of vicinity.
    results = geocode.geocode("Arctic Place 0 Brooklyn", limit=5)
    assert not any(r["name"] == "Arctic Place 0" for r in results)


def test_places_fallback_still_runs_unbounded_with_no_location_context():
    # No division/region word anywhere in the query — no anchor can be
    # derived (#83's own documented fallback: still search, just without a
    # bbox, rather than dropping a genuine name-only query to nothing).
    results = geocode.geocode("Blue Bottle Roastery", limit=5)
    names = [r["name"] for r in results]
    assert "Blue Bottle Roastery" in names


def test_division_only_queries_unaffected_by_places_fallback_change():
    # An exact division match still skips the places fallback path
    # entirely, same as before #83.
    results = geocode.geocode("Brooklyn", limit=5)
    assert results[0]["name"] == "Brooklyn"
    assert results[0]["type"] == "locality"


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


# --- #110: geocode_batch ---------------------------------------------------


def test_geocode_batch_happy_path_returns_one_row_per_query_in_order():
    result = server.geocode_batch(["Brooklyn", "Springfield, IL", "Riverside"])
    assert "results" in result
    rows = result["results"]
    assert len(rows) == 3
    assert [r["query"] for r in rows] == ["Brooklyn", "Springfield, IL", "Riverside"]
    for r in rows:
        assert "error" not in r
        assert r["name"]
        assert isinstance(r["lat"], float)
        assert isinstance(r["lon"], float)
        assert r["id"]
    assert rows[0]["name"] == "Brooklyn"
    assert rows[2]["name"] == "Riverside"


def test_geocode_batch_mixed_resolvable_and_no_match():
    result = server.geocode_batch(["Brooklyn", "Nonexistentplacexyz123"])
    rows = result["results"]
    assert len(rows) == 2
    assert rows[0]["query"] == "Brooklyn"
    assert "error" not in rows[0]
    assert rows[0]["name"] == "Brooklyn"
    assert rows[1] == {"query": "Nonexistentplacexyz123", "error": "no match"}


def test_geocode_batch_over_cap_returns_error_not_partial_results():
    result = server.geocode_batch(["Brooklyn"] * 21)
    assert "error" in result
    assert "results" not in result


def test_geocode_batch_empty_list_returns_empty_results():
    assert server.geocode_batch([]) == {"results": []}


def test_geocode_batch_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), theme="divisions", type_="division"
    )
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"), theme="places")
    result = server.geocode_batch(["Brooklyn"])
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


# --- #53: name-variant normalization (St./Saint, diacritics, ...) --------


def test_abbreviated_query_finds_expanded_canonical_name():
    # Fixture canonical name is "Saint Louis"; the literal query "St. Louis"
    # doesn't ILIKE-match it at all, so this only passes if the
    # abbreviation-variant retry actually ran.
    results = geocode.geocode("St. Louis", limit=5)
    assert results
    assert results[0]["name"] == "Saint Louis"
    assert results[0]["admin_context"] == ["United States", "Missouri"]


def test_abbreviated_query_without_period_also_matches():
    results = geocode.geocode("St Louis", limit=5)
    assert results
    assert results[0]["name"] == "Saint Louis"


def test_diacritic_query_finds_accented_canonical_name():
    # Fixture canonical name is "São Paulo"; plain-ASCII "Sao Paulo" doesn't
    # ILIKE-match it, so this only passes if the diacritic-folded retry ran.
    results = geocode.geocode("Sao Paulo", limit=5)
    assert results
    assert results[0]["name"] == "São Paulo"


def test_diacritic_query_is_reversible():
    # The accented spelling also has to find itself (sanity check that
    # _match_tier's own diacritic folding doesn't only work one way).
    results = geocode.geocode("São Paulo", limit=5)
    assert results
    assert results[0]["name"] == "São Paulo"


def test_literal_exact_match_skips_variant_retry_entirely():
    # "Saint Louis" is itself an exact literal match — the second pass
    # should never even run (nothing to prove here beyond: it still finds
    # the row via the plain literal path, same as any other exact query).
    results = geocode.geocode("Saint Louis", limit=5)
    assert results[0]["name"] == "Saint Louis"


def test_variant_sourced_row_ranks_below_literal_only_as_a_last_resort_tie():
    # White-box: literal-vs-variant is _rank_key's *last* tiebreak (#53) —
    # it only decides when every #47 signal (population, subtype, hierarchy
    # depth, region population) is otherwise tied.
    region_population: dict[str, int] = {}
    literal_row = {
        "id": "z-literal", "name": "Example", "subtype": "locality",
        "region": None, "population": None, "admin_context": [],
    }
    variant_row = {
        "id": "a-variant", "name": "Example", "subtype": "locality",
        "region": None, "population": None, "admin_context": [], "_variant": True,
    }
    rows = [variant_row, literal_row]
    rows.sort(key=lambda r: geocode._rank_key(r, "Example", region_population))
    assert rows[0]["id"] == literal_row["id"]
    assert rows[1]["id"] == variant_row["id"]


def test_variant_sourced_row_still_wins_on_real_prominence():
    # The literal-over-variant tiebreak must NOT override a genuine
    # population/prominence signal — a variant match for a real, populous
    # place has to beat a literal match against an unrelated, unpopulated
    # namesake (this is exactly the real-world "St. Louis" shape: a tiny
    # literally-named village vs. the famous city found only via variant).
    region_population: dict[str, int] = {}
    literal_row = {
        "id": "z-tiny-village", "name": "Example", "subtype": "locality",
        "region": None, "population": None, "admin_context": [],
    }
    variant_row = {
        "id": "a-famous-city", "name": "Example", "subtype": "locality",
        "region": None, "population": 1_000_000, "admin_context": [], "_variant": True,
    }
    rows = [literal_row, variant_row]
    rows.sort(key=lambda r: geocode._rank_key(r, "Example", region_population))
    assert rows[0]["id"] == variant_row["id"]
    assert rows[1]["id"] == literal_row["id"]


def test_abbreviation_variant_queries_are_bidirectional_and_leading_only():
    assert "Saint Louis" in geocode._abbreviation_variant_queries("St. Louis")
    assert "St." in geocode._abbreviation_variant_queries("Saint")
    assert "Fort Worth" in geocode._abbreviation_variant_queries("Ft. Worth")
    assert "N. Hollywood" in geocode._abbreviation_variant_queries("North Hollywood")
    # Cardinal expansion only applies to the leading token.
    assert not any(
        "West" in v for v in geocode._abbreviation_variant_queries("Fort W")
    )
