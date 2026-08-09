import duckdb
import pytest

from placeroot import geocode, overture, release, server

from .conftest import ADDRESSES_FIXTURE_PATH, CENTER_LAT, CENTER_LON, DIVISIONS_FIXTURE_PATH

# The geocode_cache fixture (local #43 divisions table) lives in conftest.py
# — test_geocode_ranking.py's #221 corpus needs the same one.


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
    # Text unique to this note, not the #105 one -- both mention find_places
    # and both say "skipped", so only the distinctive half proves which of
    # the two skip paths the caller was actually told about.
    assert "nothing distinctive is left" in result["note"]
    assert "the Metropolitan Museum of Art" in result["note"]
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


def test_multi_word_stopword_residual_is_rejected_too(monkeypatch):
    # Every word being a stopword is the rejection condition, not just the
    # single-word case.
    calls = _count_places_fallback(monkeypatch)
    result = geocode.geocode_detailed("of a Met", limit=5)
    assert calls == []
    assert result["results"] == []


@pytest.mark.parametrize("query, residual", [
    # Names made entirely of words too short for _significant_tokens' >=3
    # rule, which the gate must NOT borrow: rejecting here returns nothing
    # at all, and these are real, distinctive things to search names for.
    ("H&M Brooklyn", "H&M"),
    ("Q&A Brooklyn", "Q&A"),
    # Two characters is a whole word in Chinese/Japanese/Korean -- the
    # common case for those names, not an edge case. ("Forbidden City")
    ("故宫 Brooklyn", "故宫"),
])
def test_short_but_meaningful_residual_still_scans_places(monkeypatch, query, residual):
    calls = _count_places_fallback(monkeypatch)

    result = geocode.geocode_detailed(query, limit=5)

    assert [q for q, _ in calls] == [residual], "a short name is not an empty one"
    assert calls[0][1] is not None, "and is still bounded by the Brooklyn anchor"
    assert "note" not in result, "nothing was skipped, so there is nothing to explain"


def test_short_residual_gate_matches_the_note_it_would_have_shown():
    # The unit-level rule behind the two tests above: stopwords only, never
    # length. Keeps _nothing_but_stopwords honest about what the note claims
    # ("only common words like the or of").
    assert geocode._nothing_but_stopwords("the")
    assert geocode._nothing_but_stopwords("of a")
    assert geocode._nothing_but_stopwords("   ")
    assert not geocode._nothing_but_stopwords("H&M")
    assert not geocode._nothing_but_stopwords("故宫")
    assert not geocode._nothing_but_stopwords("the Blue Bottle")
    # _significant_tokens keeps resolve_place's own (stricter) rule, plus its
    # never-search-nothing fallback -- unchanged by #216.
    assert geocode._significant_tokens("the") == ["the"]
    assert geocode._significant_tokens("the Blue Bottle") == ["Blue", "Bottle"]


def test_misspelled_prefix_reaches_the_fallback_without_the_fuzzy_tier(monkeypatch):
    # This was #216's test_misspelled_prefix_still_reaches_the_fallback_pending_215.
    # A typo residual isn't a stopword, so this gate never rejects it -- and
    # #215 has landed, so the expectation moves: the typo reaches the places
    # scan only where the fuzzy tier can't run at all, i.e. no local
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
    #
    # Build the local table first, then cut upstream off entirely for the
    # query itself: patching the two upstream entry points alone wouldn't
    # prove much, since with a local table in hand the literal search
    # wouldn't call them either. Blocking upstream_glob covers any path
    # that would reach for the remote dataset at all.
    geocode._local_divisions_table()

    def fail(*args, **kwargs):
        raise AssertionError("the fuzzy tier must never scan upstream")

    monkeypatch.setattr(geocode, "_query_divisions_from_upstream", fail)
    monkeypatch.setattr(geocode, "_query_places_fallback", fail)
    monkeypatch.setattr(overture, "upstream_glob", fail)

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


# --- #214: alternate names (Overture's names.common) ----------------------
# Live repro on release 2026-07-22.0: "Munich" answered Munich, North
# Dakota; "Tokyo" answered Tokyo, Papua New Guinea; "Moskva" answered
# Moskva, Tajikistan. The fixture carries each endonym with its names.common
# alternates *and* the small namesake the live probe actually returned, so
# these pin the ranking and not just "the alternate matched". Alt matching
# needs the #43 local table (and the alt table beside it), hence
# `geocode_cache` throughout.


def _alt_table_path(cache_dir):
    (table,) = cache_dir.rglob(geocode._DIVISIONS_TABLE_FILENAME)
    return table.with_name(geocode._ALT_NAMES_TABLE_FILENAME)


def test_exonym_resolves_to_the_endonym_division(geocode_cache):
    results = geocode.geocode("Munich", limit=5)
    assert results[0]["name"] == "München"
    assert results[0]["id"] == "gers-div-munchen"


def test_each_live_verified_exonym_probe_resolves(geocode_cache):
    for query, expected in [
        ("Munich", "München"),
        ("Tokyo", "東京都"),
        ("Moskva", "Москва"),
        ("Vienna", "Wien"),
    ]:
        results = geocode.geocode(query, limit=5)
        assert results, f"{query!r} found nothing"
        assert results[0]["name"] == expected, query


def test_exonym_hit_outranks_the_populationless_literal_namesake(geocode_cache):
    # Munich, ND is a *literal* exact match; München is only an alternate
    # one. The alternate wins the #47 way — same tier, and it is the one
    # carrying a population — not by outranking literal matches as a class.
    names = [r["name"] for r in geocode.geocode("Munich", limit=5)]
    assert names.index("München") < names.index("Munich")


def test_alt_hit_returns_the_canonical_name_and_names_what_matched(geocode_cache):
    top = geocode.geocode("Munich", limit=5)[0]
    assert top["name"] == "München"
    assert top["matched_name"] == "Munich"


def test_matched_name_is_absent_on_ordinary_literal_matches(geocode_cache):
    # The only response-shape change #214 makes is this optional field, and
    # it must not appear on rows that matched names.primary.
    for row in geocode.geocode("Brooklyn", limit=5):
        assert "matched_name" not in row


def test_matched_name_reaches_the_geocode_tool(geocode_cache):
    payload = server.geocode("Vienna", limit=5)
    assert payload["results"][0]["name"] == "Wien"
    assert payload["results"][0]["matched_name"] == "Vienna"


def test_alt_match_folds_letters_strip_accents_leaves_alone(geocode_cache):
    # duckdb#15706: strip_accents() doesn't touch ß, so the German exonym
    # "Preßburg" is only reachable from a plain-ASCII query through the
    # explicit _UNFOLDED_LETTERS map — applied identically at build time in
    # SQL and at query time in Python.
    results = geocode.geocode("Pressburg", limit=5)
    assert results[0]["name"] == "Bratislava"
    assert results[0]["matched_name"] == "Preßburg"


# Strings picked to stress every way the two folds could drift apart: each
# _UNFOLDED_LETTERS entry in both cases, letters whose uppercase form has no
# single-character lowercase (ẞ), the dotted/dotless Turkish I pair (where
# Python's str.lower and DuckDB's lower() are documented to differ on İ),
# titlecase digraphs, compatibility ligatures, fullwidth forms, and
# non-Latin scripts that must pass through untouched.
_FOLD_CORPUS = [
    "Preßburg", "Łódź", "Malmø", "München", "Ísafjörður", "Straße", "STRAẞE", "ẞ",
    "İstanbul", "İzmir", "ı", "I", "Kœnigsberg", "Ærøskøbing", "Þingvellir", "Đakovo",
    "Ħamrun", "Ðurđevac", "ǅakovo", "ǇUBLJANA", "ﬁrenze", "Ｔｏｋｙｏ", "ØSTERBRO",
    "ŁÓDŹ", "ÆRØ", "ÞÓRSHÖFN", "Nürnberg", "Reykjavík", "Kraków", "Aš", "Ḥalab",
    "Αθήνα", "ΑΘΗΝΑ", "Москва", "東京都", "São Paulo",
]


def test_alt_fold_matches_between_python_and_sql():
    # The whole #214 design rests on this: alternates are folded once in SQL
    # at materialization time and the query is folded in Python per call, and
    # the two only ever meet as an ILIKE comparison — so a divergence on any
    # letter silently makes those alternates unreachable rather than raising.
    # Run the real SQL fold through DuckDB and compare, rather than asserting
    # the Python side against itself.
    con = duckdb.connect()
    sql = f"SELECT {geocode._fold_alt_name_sql('?')}"
    for name in _FOLD_CORPUS:
        assert con.execute(sql, [name]).fetchone()[0] == geocode._fold_alt_name(name), name
    assert geocode._fold_alt_name("Preßburg") == "pressburg"
    assert geocode._fold_alt_name("Łódź") == "lodz"
    assert geocode._fold_alt_name("Malmø") == "malmo"


def test_endonym_queries_are_unaffected_by_the_alt_table(geocode_cache):
    # The regression this feature is most at risk of: querying the canonical
    # spelling must still return it as a plain literal match.
    for query, expected in [("München", "München"), ("Wien", "Wien"), ("Москва", "Москва")]:
        top = geocode.geocode(query, limit=5)[0]
        assert top["name"] == expected
        assert "matched_name" not in top


def test_one_result_per_division_however_many_alternates_match(geocode_cache):
    # A division has one alt row per distinct folded spelling — Москва
    # carries "moscow", "moskau", "moskva", "moskwa" — and a substring query
    # matches several at once. Without the per-id de-duplication that came
    # back as the same GERS id three times over, which at a small `limit`
    # crowded every other division out of the answer entirely.
    results = geocode.geocode("Mosk", limit=10)
    ids = [r["id"] for r in results]
    assert len(ids) == len(set(ids)), results
    assert "gers-div-moskva-ru" in ids and "gers-div-moskva-tj" in ids
    # The surviving alternate is the one that matched best, not an arbitrary
    # one of the four: "Moskva" is an exact match for the query's fold.
    top = geocode.geocode("Moskva", limit=5)[0]
    assert top["name"] == "Москва"
    assert top["matched_name"] == "Moskva"


def test_alternate_crowding_does_not_consume_the_limit(geocode_cache):
    # The user-visible half of the same bug: three of the caller's three
    # slots went to one city.
    names = [r["name"] for r in geocode.geocode("Mosk", limit=3)]
    assert names.count("Москва") == 1
    assert "Moskva" in names  # the Tajik namesake still gets a slot


def test_query_that_folds_to_nothing_matches_no_alternates(geocode_cache):
    # A query of only combining marks folds away to "" (that is what
    # stripping diacritics does to it), which as an ILIKE pattern is a bare
    # '%%' matching every alternate in the table — answering a nonsense
    # query with whichever divisions happen to be most prominent. The
    # literal search, which matches raw names, returns nothing for these.
    #
    # The #215 fuzzy tier had the same hole independently — DuckDB scores
    # jaro_winkler_similarity(name, '') above the threshold — and the query
    # reaches it precisely because the literal and alternate passes both
    # come back empty, so both guards are needed for this to hold.
    assert geocode._fold_alt_name("́") == ""
    for query in ["́", "́̈"]:
        assert geocode.geocode(query, limit=5) == [], query


def test_alt_table_is_materialized_beside_the_divisions_table(geocode_cache):
    geocode.geocode("Brooklyn", limit=1)
    alt = _alt_table_path(geocode_cache)
    assert alt.exists()
    rows = duckdb.connect().execute(
        f"SELECT alt_name, alt_display FROM read_parquet('{alt}') ORDER BY alt_name"
    ).fetchall()
    assert ("munich", "Munich") in rows
    # Folded, so one row for the several languages that spell it "Munich",
    # and none at all for an alternate that folds to the primary name.
    assert [r for r in rows if r[0] == "munich"] == [("munich", "Munich")]
    assert "münchen" not in [r[0] for r in rows]
    assert "munchen" not in [r[0] for r in rows]


def test_missing_alt_table_degrades_to_primary_only(geocode_cache, monkeypatch):
    # A cache directory written before #214: divisions table present, alt
    # table absent, and the one rebuild attempt fails (offline). Must answer
    # from primary names alone rather than erroring.
    geocode.geocode("Brooklyn", limit=1)
    _alt_table_path(geocode_cache).unlink()
    monkeypatch.setattr(geocode, "_ALT_BUILD_ATTEMPTED", set())

    def boom(path, glob):
        raise duckdb.Error("no upstream here")

    monkeypatch.setattr(geocode, "_materialize_alt_names_table", boom)

    results = geocode.geocode("Munich", limit=5)

    assert [r["name"] for r in results] == ["Munich"]  # the North Dakota one
    assert "matched_name" not in results[0]


def test_missing_alt_table_is_rebuilt_once_per_process(geocode_cache, monkeypatch):
    # The other half of the pre-#214 cache story: existing installs get the
    # feature without waiting for the next Overture release, and a failing
    # build costs one attempt, not one per geocode call.
    geocode.geocode("Brooklyn", limit=1)
    _alt_table_path(geocode_cache).unlink()
    monkeypatch.setattr(geocode, "_ALT_BUILD_ATTEMPTED", set())
    attempts = []
    real = geocode._materialize_alt_names_table

    def counted(path, glob):
        attempts.append(glob)
        raise duckdb.Error("no upstream here")

    monkeypatch.setattr(geocode, "_materialize_alt_names_table", counted)
    geocode.geocode("Munich", limit=5)
    geocode.geocode("Vienna", limit=5)
    assert len(attempts) == 1

    # And once it can succeed, the rebuilt table is picked up.
    monkeypatch.setattr(geocode, "_ALT_BUILD_ATTEMPTED", set())
    monkeypatch.setattr(geocode, "_materialize_alt_names_table", real)
    assert geocode.geocode("Munich", limit=5)[0]["name"] == "München"


def test_alt_search_never_runs_without_a_local_table(monkeypatch):
    # No geocode_cache fixture, so PLACEROOT_CACHE=off: there is no local
    # table to join against and nowhere to put an alt one, so this stays the
    # pre-#214 upstream primary-name scan rather than degrading to something
    # that reads names.common off S3 per query.
    def boom(*a, **kw):
        raise AssertionError("alt-name query must not run without a local table")

    monkeypatch.setattr(geocode, "_query_alt_names", boom)
    assert [r["name"] for r in geocode.geocode("Munich", limit=5)] == ["Munich"]


def test_alt_hits_rank_like_variant_hits_not_above_literal_ones(geocode_cache):
    # An alt row is tagged _variant, so against a same-tier literal row with
    # the same prominence the literal one wins — the #53 tiebreak, unchanged.
    literal = {
        "id": "a", "name": "Vienna", "subtype": "locality", "region": "US-IL",
        "admin_context": ["a", "b"], "population": 5_000,
    }
    alt = {
        "id": "b", "name": "Wien", "subtype": "locality", "region": "AT-9",
        "admin_context": ["a", "b"], "population": 5_000,
        "_variant": True, "_tier": 3, "_matched_name": "Vienna",
    }
    ranked = sorted([alt, literal], key=lambda r: geocode._rank_key(r, "Vienna", {}))
    assert [r["id"] for r in ranked] == ["a", "b"]


def test_exact_alt_match_stands_down_the_places_fallback(geocode_cache, monkeypatch):
    # An exact match is an exact match whichever name column found it: no
    # reason to pay for a places scan to pad the answer out to `limit`.
    calls = _count_places_fallback(monkeypatch)
    geocode.geocode("Vienna", limit=5)
    assert calls == []


def test_alt_names_do_not_feed_the_fuzzy_tier(geocode_cache):
    # Stated limit of this change (see _query_divisions_fuzzy): the #215
    # threshold was calibrated on primary names, so a typo of an *exonym*
    # is not corrected. "Pressburg" resolves Bratislava through names.common;
    # "Pressbrug" — one transposition away, and nowhere near any *primary*
    # name in the fixture — finds nothing.
    assert geocode.geocode("Pressburg", limit=5)[0]["name"] == "Bratislava"
    assert [r["name"] for r in geocode.geocode("Pressbrug", limit=5)] == []


def test_typo_with_a_region_suffix_still_reaches_the_fuzzy_tier(geocode_cache, monkeypatch):
    # "City, ST" is the most common shape a caller writes, and a region
    # suffix must not cost the correction. The literal search drops the
    # region on a miss and retries the *whole* original string, so the
    # fuzzy pass has to match on the name half (base_query) instead --
    # nothing is within edit distance of "Berekley, CA".
    calls = _count_places_fallback(monkeypatch)

    result = geocode.geocode_detailed("Berekley, CA", limit=5)

    assert [r["name"] for r in result["results"]] == ["Berkeley"]
    assert calls == [], "a corrected typo must not fall through to the places theme"
    # The note names the string actually corrected, not the raw query with
    # its suffix still attached.
    assert '"Berekley"' in result["note"]
    assert '"Berkeley"' in result["note"]


def test_region_suffixed_typo_is_filtered_by_that_region_first(geocode_cache):
    # The suffix's region code narrows the pass, like it does for the
    # literal queries...
    table = geocode._local_divisions_table()
    assert [r["name"] for r in geocode._query_divisions_fuzzy(table, "Berekley", "US-CA")] == [
        "Berkeley",
    ]
    assert geocode._query_divisions_fuzzy(table, "Berekley", "US-NY") == []
    # ...and, like the literal search one step up, a region-constrained
    # miss degrades to an unconstrained retry rather than answering
    # nothing: a suffix that parsed as a region may simply have been read
    # wrong.
    assert [r["name"] for r in geocode.geocode("Berekley, NY", limit=5)] == ["Berkeley"]


def test_fuzzy_rows_are_labeled_fuzzy_not_substring(geocode_cache):
    # A corrected row does not contain the query at all, so resolve_place
    # must not report it as a "substring" match -- that field is exactly
    # what a caller reads to decide whether the answer is the string it
    # asked for. _match_tier grades any name it is handed as at least
    # "substring", so the label has to come from the row's provenance.
    assert geocode.geocode("Berekley", limit=5)[0]["matched_by"] == "fuzzy"
    assert "matched_by" not in geocode.geocode("Brooklyn", limit=5)[0]

    top = geocode.resolve_place("Berekley")[0]
    assert top["name"] == "Berkeley"
    assert top["match"] == "fuzzy"

    assert geocode.resolve_place("Sna Francisco")[0]["match"] == "fuzzy"
    assert server.resolve_place("Berekley")["results"][0]["match"] == "fuzzy"


def test_fuzzy_label_ranks_below_every_literal_label(geocode_cache):
    # Same ordering _rank_key enforces on the rows themselves, restated
    # for resolve_place's label vocabulary.
    ranks = geocode._MATCH_LABEL_RANK
    assert ranks["fuzzy"] < min(ranks[label] for label in ("exact", "prefix", "substring"))


def test_resolve_area_accepts_a_corrected_spelling(geocode_cache):
    # Pins the knock-on effect of #215 on the area-constrained tools
    # (find_places/summarize_area go through here): a typo'd area now
    # resolves to the corrected division. resolve_area returns no note
    # channel, so this is deliberately the whole contract -- the
    # correction is silent by design, and this test is what makes that a
    # decision rather than an accident.
    assert geocode.resolve_area("Berekley") == {
        "division_id": "gers-div-berkeley",
        "name": "Berkeley",
        "admin_context": ["United States", "California"],
    }
    assert geocode.resolve_area("Sna Francisco")["name"] == "San Francisco"
    # An area that isn't a typo of anything still resolves to nothing.
    assert geocode.resolve_area("Nonexistentplacexyz123") is None

# --- #221: prominence can rescue a prefix match; the fold stops being gated
# The broad "did any of this move an answer that was already right" question
# is tests/test_geocode_ranking.py's; these pin the rule itself.


def _row(id_, name, tier, population=None, **kw):
    row = {
        "id": id_, "name": name, "subtype": "locality", "region": None,
        "population": population, "admin_context": [], "_tier": tier,
    }
    row.update(kw)
    return row


def _ranked(*rows):
    return [r["id"] for r in sorted(rows, key=lambda r: geocode._rank_key(r, "Example", {}))]


def test_populated_prefix_match_outranks_a_populationless_exact_one():
    # The 東京 shape: exact-tier namesake with no population vs. a prefix
    # match with millions behind it.
    assert _ranked(
        _row("a-exact-empty", "Example", 3),
        _row("z-prefix-populous", "Exampleton", 2, population=13_929_286),
    ) == ["z-prefix-populous", "a-exact-empty"]


def test_a_populated_exact_match_still_beats_a_more_populated_prefix_one():
    # The rescue is only *past a population-less row*. Two rows that both
    # carry a population still order by tier first, however lopsided the
    # populations are — "Portland" is not a worse answer than a bigger
    # "Portland Heights".
    assert _ranked(
        _row("z-exact-small", "Example", 3, population=1_000),
        _row("a-prefix-huge", "Exampleton", 2, population=10_000_000),
    ) == ["z-exact-small", "a-prefix-huge"]


def test_two_populationless_rows_still_order_by_tier():
    assert _ranked(
        _row("z-exact", "Example", 3),
        _row("a-prefix", "Exampleton", 2),
    ) == ["z-exact", "a-prefix"]


def test_a_population_of_zero_does_not_rescue_a_prefix_match():
    # The rescue is prominence, not column-completeness: Overture ships
    # divisions with an explicit population of 0 (abandoned/unincorporated
    # places), and a bare "population is not None" check would let one of
    # those leapfrog an exact match whose population is simply unrecorded —
    # reading a filled-in field as prominence when the value says otherwise.
    assert _ranked(
        _row("z-exact-unknown", "Example", 3),
        _row("a-prefix-zero", "Exampleton", 2, population=0),
    ) == ["z-exact-unknown", "a-prefix-zero"]


def test_the_smallest_nonzero_population_still_rescues():
    # The boundary is exactly zero — one recorded resident is still a
    # recorded population, and the rule stays a simple, explainable one
    # rather than an invented cutoff.
    assert _ranked(
        _row("z-exact-unknown", "Example", 3),
        _row("a-prefix-one", "Exampleton", 2, population=1),
    ) == ["a-prefix-one", "z-exact-unknown"]


def test_a_population_of_zero_still_outranks_an_unknown_one_at_the_same_tier():
    # #221 moves zero out of the *rescue* term only. Below the tier term the
    # #47 ordering is untouched: "we know it is 0" still sorts ahead of "we
    # do not know", which is what the no-population proxy chain hangs off.
    assert _ranked(
        _row("z-exact-zero", "Example", 3, population=0),
        _row("a-exact-unknown", "Example", 3),
    ) == ["z-exact-zero", "a-exact-unknown"]


def test_a_substring_match_cannot_leapfrog_a_strong_tier_however_populous():
    # Prominence only reorders *within* the exact/prefix group: "York" is a
    # substring of "New York", and a substring hit staying below every
    # exact/prefix one is what keeps that from swallowing the query.
    assert _ranked(
        _row("z-exact-empty", "Example", 3),
        _row("z-prefix-empty", "Exampleton", 2),
        _row("a-substring-huge", "Not An Example At All", 1, population=10_000_000),
    ) == ["z-exact-empty", "z-prefix-empty", "a-substring-huge"]


def test_a_fuzzy_row_still_ranks_below_every_literal_tier():
    # #215's ordering is ahead of all of this and must stay there — a
    # correction of a *different* string can't be rescued by prominence.
    assert _ranked(
        _row("z-substring-empty", "Not An Example At All", 1),
        _row("a-fuzzy-huge", "Exemple", 3, population=10_000_000, _fuzzy=True),
    ) == ["z-substring-empty", "a-fuzzy-huge"]


def test_prominent_prefix_division_wins_end_to_end(geocode_cache):
    # Live (2026-07-22.0) this returned the Nagano neighborhood: 東京 is
    # exactly its name and only a prefix of 東京都's.
    results = geocode.geocode("東京", limit=5)
    assert results[0]["name"] == "東京都"
    assert results[0]["type"] == "locality"
    # The namesake is not dropped, only ranked under it.
    assert "東京" in [r["name"] for r in results]


def test_diacritic_fold_runs_even_when_the_literal_match_has_a_population(
    geocode_cache,
):
    # The gate #221 removed: "Zurich" literally matches a Dutch village that
    # carries a population (190), which used to read as "the literal search
    # already found something real" — so the folded pass never ran and
    # Zürich (443k) was absent from the results entirely, not merely below.
    results = geocode.geocode("Zurich", limit=5)
    assert results[0]["name"] == "Zürich"
    assert results[0]["admin_context"] == ["Switzerland"]
    assert "Zurich" in [r["name"] for r in results]


def test_the_unconditional_fold_is_scoped_to_the_local_table():
    # Without a #43 table the fold stays gated on prominence exactly as it
    # was before #221, so this query keeps its old (worse) answer: upstream
    # the extra pass is an unprunable full-theme ILIKE, not the 0.2s local
    # predicate the change was measured on, and paying it on every query
    # that already has a good literal answer is the cost class #105/#216
    # exist to avoid. Warming the cache is what buys the fix.
    results = geocode.geocode("Zurich", limit=5)
    assert [r["name"] for r in results] == ["Zurich"]


def test_the_fold_still_runs_without_a_local_table_when_the_literal_search_is_weak():
    # The gate, not the fold, is what's local-only: with no prominent
    # literal match the folded pass runs upstream the same as it always
    # did, which is what "Sao Paulo" -> "São Paulo" rides on. Pinned here
    # so the #221 scoping above can't quietly turn into "no fold upstream".
    results = geocode.geocode("Sao Paulo", limit=5)
    assert results[0]["name"] == "São Paulo"


def test_an_accented_query_still_finds_itself(geocode_cache):
    results = geocode.geocode("Zürich", limit=5)
    assert results[0]["name"] == "Zürich"


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


def test_vanished_local_table_mid_query_falls_back_to_upstream(geocode_cache):
    """#230: the local table being deleted between _local_divisions_table()'s
    exists() check and the read (external cleanup, or a cache sweep) must
    degrade to a direct upstream scan, not surface a false
    upstream_unavailable."""
    geocode.geocode("Brooklyn", limit=5)  # materialize the table
    path = geocode._local_divisions_table_path(release.PINNED_RELEASE)
    path.unlink()  # simulate the sweep winning the race
    results = geocode._query_divisions("Brooklyn", None, str(path))
    assert any(r["name"] == "Brooklyn" for r in results)


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


def test_literal_exact_match_skips_the_abbreviation_retries():
    # "Saint Louis" is itself an exact literal match, so the abbreviation
    # half of the second pass never runs (#221 kept that gate; only the
    # diacritic fold became unconditional). Nothing to prove here beyond:
    # it still finds the row via the plain literal path, same as any other
    # exact query.
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
