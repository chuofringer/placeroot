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


def test_wildcards_in_query_are_literal():
    # #165: find_places(name=) escapes ILIKE metacharacters; geocode must
    # treat the same string the same way. Pre-fix, 'Brook%' ranked
    # 'Brooklyn' as an *exact* (tier 0) match and 'Brook_yn' matched it via
    # the single-char wildcard; both must be literal and match nothing.
    assert geocode.geocode("Brook%", limit=5) == []
    assert geocode.geocode("Brook_yn", limit=5) == []


def test_wildcards_in_query_are_literal_on_local_divisions_table(geocode_cache):
    # Same contract on the #43 local-table path, which builds its SQL in
    # _query_divisions_from_local rather than the direct-fixture scan.
    assert geocode.geocode("Brook%", limit=5) == []
    assert geocode.geocode("Brook_yn", limit=5) == []


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


# --- #216: the places fallback never anchors on a stopword-only residual ---
# Live repro: "the Met" anchored on a division named "Met" and then searched
# place names for the residual "the" -- 50.2s of S3 scanning, answering with
# "The Core IAS". The scan itself is the bug, so these count scans.


def _count_places_fallback(monkeypatch):
    """Record every places-theme scan; returns the (growing) list of calls."""
    calls = []
    real = geocode._query_places_fallback

    def counted(query, anchor=None):
        calls.append((query, anchor))
        return real(query, anchor=anchor)

    monkeypatch.setattr(geocode, "_query_places_fallback", counted)
    return calls


def test_stopword_only_residual_runs_no_places_scan_at_all(monkeypatch):
    # "Met" substring-matches the fixture division "Metropolis", so the
    # trailing-token anchor resolves exactly as it did live -- leaving "the"
    # as the thing we must refuse to search place names for.
    calls = _count_places_fallback(monkeypatch)

    result = geocode.geocode_detailed("the Met", limit=5)

    assert calls == [], "a stopword-only residual must not touch the places theme"
    assert result["results"] == []
    assert "skipped" in result["note"]


def test_stopword_residual_note_reaches_the_geocode_tool(monkeypatch):
    _count_places_fallback(monkeypatch)
    result = server.geocode("the Met", limit=5)
    assert result["results"] == []
    assert "find_places" in result["note"]


def test_anchored_query_with_a_real_residual_still_scans_places(monkeypatch):
    # The gate is about emptiness, not about anchoring: "Blue Bottle" is a
    # perfectly good thing to search place names for, so this must run the
    # (bbox-bounded) scan exactly as #83 left it.
    calls = _count_places_fallback(monkeypatch)

    results = geocode.geocode("Blue Bottle Roastery Brooklyn", limit=5)

    assert [q for q, _ in calls] == ["Blue Bottle Roastery"]
    assert calls[0][1] is not None, "and still bounded by the Brooklyn anchor"
    assert "Blue Bottle Roastery" in [r["name"] for r in results]


def test_short_residual_tokens_are_as_insignificant_as_stopwords(monkeypatch):
    # Two-char residuals carry no more signal than "the" does and cost the
    # same full-theme scan.
    calls = _count_places_fallback(monkeypatch)
    result = geocode.geocode_detailed("of a Met", limit=5)
    assert calls == []
    assert result["results"] == []


def test_significant_words_shares_the_resolve_place_rule():
    # The residuals #216 has to reject: stopwords and sub-3-char words only.
    assert geocode._significant_words("the") == []
    assert geocode._significant_words("of a") == []
    assert geocode._significant_words("Blue Bottle in Brooklyn") == ["Blue", "Bottle", "Brooklyn"]
    # _significant_tokens keeps its never-search-nothing fallback on top of
    # the same rule.
    assert geocode._significant_tokens("the") == ["the"]
    assert geocode._significant_tokens("the Blue Bottle") == ["Blue", "Bottle"]


def test_misspelled_prefix_reaches_the_fallback_without_the_fuzzy_tier(monkeypatch):
    # This was #216's test_misspelled_prefix_still_reaches_the_fallback_pending_215.
    # #215 landed, so the expectation moves: the typo residual only reaches
    # the places scan where the fuzzy tier can't run at all — no local
    # divisions table (cache off, as the default fixtures run), which is
    # what this test still pins. With the table available it doesn't; see
    # test_typo_query_runs_no_places_scan_at_all below.
    calls = _count_places_fallback(monkeypatch)
    geocode.geocode_detailed("Sna Brooklyn", limit=5)
    assert [q for q, _ in calls] == ["Sna"]


# --- #215: fuzzy fallback tier for typos ----------------------------------
# Live repro: "Berekley"/"Cinncinati" returned nothing, "Sna Francisco"
# answered "Snags N Burgs Cafe" off the places fallback. The tier only runs
# against the #43 local table, so every test here takes `geocode_cache`.


def test_typo_resolves_to_the_correctly_spelled_division(geocode_cache):
    results = geocode.geocode("Berekley", limit=5)
    assert [r["name"] for r in results] == ["Berkeley"]
    assert results[0]["id"] == "gers-div-berkeley"


def test_typo_resolves_for_each_live_verified_probe(geocode_cache):
    # The three probes from #215's live measurement, top-1 each.
    for query, expected in [
        ("Berekley", "Berkeley"),
        ("Cinncinati", "Cincinnati"),
        ("Sna Francisco", "San Francisco"),
    ]:
        results = geocode.geocode(query, limit=5)
        assert results, f"{query!r} found nothing"
        assert results[0]["name"] == expected, query


def test_fuzzy_results_carry_a_note_naming_the_corrected_spelling(geocode_cache):
    result = geocode.geocode_detailed("Cinncinati", limit=5)
    assert result["results"][0]["name"] == "Cincinnati"
    assert '"Cincinnati"' in result["note"]
    assert "Cinncinati" in result["note"]


def test_fuzzy_note_reaches_the_geocode_tool(geocode_cache):
    payload = server.geocode("Berekley", limit=5)
    assert payload["results"][0]["name"] == "Berkeley"
    assert "Berkeley" in payload["note"]


def test_typo_query_runs_no_places_scan_at_all(geocode_cache, monkeypatch):
    # The #216 failure mode this closes: "Sna Francisco" must not reach a
    # substring scan of the places theme for "Sna" (or for anything else).
    calls = _count_places_fallback(monkeypatch)

    results = geocode.geocode("Sna Francisco", limit=5)

    assert calls == [], "a corrected typo must not fall through to the places theme"
    assert results[0]["name"] == "San Francisco"


def test_fuzzy_pass_never_scans_upstream(geocode_cache, monkeypatch):
    # The tier is local-table-only by construction; assert it, since a
    # similarity predicate against S3 has nothing to prune by and would
    # read the whole divisions theme over the network.
    def fail(*args, **kwargs):
        raise AssertionError("the fuzzy tier must never scan upstream")

    monkeypatch.setattr(geocode, "_query_divisions_from_upstream", fail)
    monkeypatch.setattr(geocode, "_query_places_fallback", fail)

    assert geocode.geocode("Berekley", limit=5)[0]["name"] == "Berkeley"


def test_no_fuzzy_tier_without_a_local_table(monkeypatch):
    # Cache off (the default fixture setup): no local table, so the fuzzy
    # tier is simply unavailable rather than degrading to an upstream
    # similarity scan.
    calls = []
    monkeypatch.setattr(
        geocode, "_query_divisions_fuzzy",
        lambda *a, **k: calls.append(a) or [],
    )
    assert geocode.geocode("Berekley", limit=5) == []
    assert calls == []


def test_literal_matches_are_untouched_by_the_fuzzy_tier(geocode_cache, monkeypatch):
    # Regression guard: with the local table live, every query that already
    # matched something literally must return exactly what it did before
    # #215, and must not run the fuzzy pass at all.
    def fail(*args, **kwargs):
        raise AssertionError("fuzzy pass ran despite a literal match")

    monkeypatch.setattr(geocode, "_query_divisions_fuzzy", fail)

    assert [r["name"] for r in geocode.geocode("Springfield", limit=10)] == [
        "Springfield", "Springfield",
    ]
    assert geocode.geocode("Brooklyn", limit=5)[0]["name"] == "Brooklyn"
    # A substring-only match ("Brook" -> "Brooklyn"/"Downtown Brooklyn") is
    # still a literal answer to what was typed: weak, but not a typo, so the
    # trigger stays on emptiness alone.
    assert "Brooklyn" in [r["name"] for r in geocode.geocode("Brook", limit=5)]
    assert "note" not in geocode.geocode_detailed("Brooklyn", limit=5)


def test_fuzzy_hits_rank_below_every_literal_tier(geocode_cache):
    # A query that matches one division literally (substring, the weakest
    # tier) and would fuzzy-match another: the literal row must come first
    # and score higher, whatever the fuzzy row's population.
    fuzzy_row = {
        "id": "z", "name": "Berkeley", "subtype": "locality", "population": 10_000_000,
        "admin_context": [], "_fuzzy": True, "_similarity": 0.99, "_tier": 1,
    }
    literal_row = {
        "id": "a", "name": "Downtown Brooklyn", "subtype": "neighborhood",
        "population": None, "admin_context": [],
    }
    assert geocode._rank_key(literal_row, "Brooklyn", {}) < geocode._rank_key(
        fuzzy_row, "Brooklyn", {}
    )
    assert geocode._rank_score(fuzzy_row, "Brooklyn") < geocode._rank_score(
        literal_row, "Brooklyn"
    )
    assert geocode._rank_score(fuzzy_row, "Brooklyn") < 0.4


def test_fuzzy_threshold_rejects_merely_similar_names(geocode_cache):
    # "Sna Brooklyn" vs "Brooklyn" is 0.89 jaro-winkler -- close, but not a
    # typo of it, and below the threshold. Nothing comes back rather than a
    # confident wrong answer.
    assert geocode._FUZZY_SIMILARITY_THRESHOLD == 0.92
    assert geocode.geocode("Nonexistentplacexyz123", limit=5) == []
    rows = geocode._query_divisions_fuzzy(geocode._local_divisions_table(), "Sna Brooklyn")
    assert rows == []


def test_wildcard_queries_are_not_fuzzy_matched_either(geocode_cache):
    # #165 made ILIKE metacharacters literal, so "Brook_yn" matches
    # nothing; jaro-winkler would happily read the "_" as a typo and answer
    # "Brooklyn" anyway, undoing that. Pinned here as well as in the #165
    # test above, since this is the reason the exclusion exists.
    assert geocode._has_like_metacharacter("Brook_yn")
    assert not geocode._has_like_metacharacter("Berekley")
    assert geocode.geocode("Brook_yn", limit=5) == []
    assert geocode.geocode("Brook%", limit=5) == []


def test_fuzzy_matching_folds_case_and_diacritics(geocode_cache):
    # Same folding #53 uses: the fixture's canonical "São Paulo" has to be
    # reachable from a plain-ASCII typo of it.
    results = geocode.geocode("Sao Paluo", limit=5)
    assert results[0]["name"] == "São Paulo"


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


# --- #105: the anchorless places scan is skipped against a REMOTE dataset ---
# Measured live (2026-07-22.0): an anchorless name query took 216s end-to-end
# and returned nothing, because there's no bbox to prune the global places
# theme by. Removing the ORDER BY didn't help (219s) -- a LIMIT can't
# short-circuit a scan that matches few or no rows.


def _pretend_places_are_remote(monkeypatch):
    """Make only the places glob look like S3; leave every other theme local."""
    real = geocode.overture.upstream_glob

    def fake(theme=None, type_=None, **kw):
        if theme == "places":
            return "s3://overturemaps-us-west-2/release/x/theme=places/type=place/*"
        return real(theme=theme, type_=type_, **kw) if theme else real(**kw)

    monkeypatch.setattr(geocode.overture, "upstream_glob", fake)


def _forbid_places_fallback(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the unbounded places scan ran against a remote dataset")

    monkeypatch.setattr(geocode, "_query_places_fallback", boom)


def test_anchorless_query_skips_the_global_scan_when_upstream_is_remote(monkeypatch):
    _pretend_places_are_remote(monkeypatch)
    _forbid_places_fallback(monkeypatch)

    result = geocode.geocode_detailed("Blue Bottle Roastery", limit=5)

    assert result["results"] == []
    assert "note" in result
    assert "location" in result["note"].lower()


def test_the_skip_note_reaches_the_geocode_tool(monkeypatch):
    _pretend_places_are_remote(monkeypatch)
    _forbid_places_fallback(monkeypatch)

    result = server.geocode("Blue Bottle Roastery", limit=5)

    assert result["results"] == []
    assert "search_categories" not in result.get("note", "")
    assert "find_places" in result["note"]


def test_anchored_query_still_searches_places_when_upstream_is_remote(monkeypatch):
    """The skip is only for queries with NO location context — an anchored
    one still gets its (bbox-pruned, cheap) places search."""
    _pretend_places_are_remote(monkeypatch)
    called = []
    # Record the anchor rather than running the query: the glob above is a
    # stand-in for S3 and isn't readable from a test.
    monkeypatch.setattr(
        geocode, "_query_places_fallback",
        lambda q, anchor=None: called.append(anchor) or [],
    )
    # "Brooklyn" matches a division, so an anchor is derivable.
    geocode.geocode_detailed("Blue Bottle Roastery Brooklyn", limit=5)
    assert called, "an anchored query must still run the places search"
    assert called[0] is not None, "and it must be bounded by that anchor"


def test_local_dataset_keeps_the_unbounded_name_search(monkeypatch):
    """#83's name-only search is preserved wherever it's actually cheap: the
    cost is the remote full-theme read, not the query shape."""
    result = geocode.geocode_detailed("Blue Bottle Roastery", limit=5)
    assert "Blue Bottle Roastery" in [r["name"] for r in result["results"]]
    assert "note" not in result


def test_env_override_forces_the_scan_even_when_remote(monkeypatch):
    monkeypatch.setenv("PLACEROOT_UNBOUNDED_NAME_SEARCH", "1")
    monkeypatch.setattr(geocode, "_is_remote", lambda glob: True)
    assert geocode._skip_unanchored_places_scan() is False


def test_remote_scheme_detection():
    assert geocode._is_remote("s3://bucket/key/*")
    assert geocode._is_remote("https://example.com/x/*")
    assert not geocode._is_remote("/tmp/fixtures/places.parquet")
    assert not geocode._is_remote("tests/fixtures/places.parquet")
