"""Tests for #410: result-language preference — return Overture

language-tagged name variants.

Two data sources carry the variants exercised here, both extended for this
issue rather than hand-edited:

- tests/fixtures/divisions.parquet already carried real names.common
  entries for #214's exonym corpus (München -> {en: Munich, ...}, Wien ->
  {en: Vienna, ...}, ...) — no fixture change was needed on the divisions
  side, only the query-layer plumbing (geocode.py's new lang_names table).
- tests/fixtures/places.parquet had no names.common column at all before
  this issue (see test_poi_name_tiers.py's module docstring). scripts/
  build_fixture.py now writes one, empty on every row except one isolated
  far-away place ("Kaffeehaus Wien", 5.0/100.0 — chosen away from every
  other fixture cluster and test radius so it cannot change any existing
  count-based assertion), which is what the place_details tests below use.

Divisions-side lookups need the #43 local table (and the #214 alt-name
table beside it, and now the lang table beside that), hence `geocode_cache`
throughout the geocode()/resolve_place() tests, mirroring
test_geocode.py's #214 section.
"""

import duckdb

from placeroot import geocode, overture, preferences, server

# --- preferences: lang field -------------------------------------------------


def test_missing_lang_is_none_by_default():
    assert preferences.load()["lang"] is None


def test_lang_round_trips_through_the_tool():
    updated = server.preferences(lang="de")
    assert updated["lang"] == "de"
    assert server.preferences()["lang"] == "de"


def test_lang_is_lowercased_and_stripped():
    server.preferences(lang=" DE ")
    assert server.preferences()["lang"] == "de"


def test_lang_accepts_a_three_letter_code():
    server.preferences(lang="fra")
    assert server.preferences()["lang"] == "fra"


def test_junk_lang_is_rejected_like_an_unsupported_mode():
    result = server.preferences(lang="english")
    assert result["error"] == "bad_request"
    # Rejected before it ever reaches the stored document.
    assert server.preferences()["lang"] is None


def test_single_letter_lang_is_rejected():
    result = server.preferences(lang="e")
    assert result["error"] == "bad_request"


def test_lang_with_digits_is_rejected():
    result = server.preferences(lang="d3")
    assert result["error"] == "bad_request"


def test_clear_resets_lang_too():
    server.preferences(lang="de")
    cleared = server.preferences(clear=True)
    assert cleared["lang"] is None


def test_resolve_lang_explicit_wins_over_stored():
    preferences.update(lang="de")
    assert preferences.resolve_lang("en") == "en"


def test_resolve_lang_falls_back_to_stored_when_omitted():
    preferences.update(lang="de")
    assert preferences.resolve_lang(None) == "de"


def test_resolve_lang_is_none_with_nothing_stored():
    assert preferences.resolve_lang(None) is None


# --- geocode()/geocode_detailed(): division-side variant swap ---------------


def test_no_lang_is_byte_identical(geocode_cache):
    """Guardrail: omitting lang must not change a single byte of the answer."""
    with_no_lang = geocode.geocode("München", limit=1)
    assert with_no_lang[0]["name"] == "München"
    assert "name_primary" not in with_no_lang[0]


def test_lang_swaps_in_the_requested_variant(geocode_cache):
    results = geocode.geocode("München", limit=1, lang="en")
    assert results[0]["name"] == "Munich"
    assert results[0]["name_primary"] == "München"


def test_lang_with_no_variant_leaves_primary_untouched(geocode_cache):
    """München's names.common has no "zz" entry — primary stands, no note,
    no name_primary noise."""
    results = geocode.geocode("München", limit=1, lang="zz")
    assert results[0]["name"] == "München"
    assert "name_primary" not in results[0]


def test_lang_and_alt_name_match_compose_cleanly(geocode_cache):
    """Querying the exonym itself ("Munich", #214's alt-name tier) and also
    asking for the "en" variant both fire on the same row without
    conflicting: matched_name still names what was searched, name_primary
    still names the (different) canonical spelling."""
    top = geocode.geocode("Munich", limit=5, lang="en")[0]
    assert top["name"] == "Munich"
    assert top["matched_name"] == "Munich"
    assert top["name_primary"] == "München"


def test_lang_applies_across_the_live_verified_exonym_corpus(geocode_cache):
    for query, lang, variant in [
        ("München", "en", "Munich"),
        ("東京都", "en", "Tokyo"),
        ("Москва", "en", "Moscow"),
        ("Wien", "en", "Vienna"),
        ("Bratislava", "de", "Preßburg"),
    ]:
        results = geocode.geocode(query, limit=5, lang=lang)
        assert results, f"{query!r} found nothing"
        assert results[0]["name"] == variant, query
        assert results[0]["name_primary"] == query, query


def test_geocode_detailed_results_key_carries_the_swap(geocode_cache):
    result = geocode.geocode_detailed("München", limit=1, lang="en")
    assert result["results"][0]["name"] == "Munich"


def test_places_fallback_rows_are_unaffected_by_lang(geocode_cache):
    """#410 deliberately scopes out places-fallback rows this round (the
    same line find_places' own docstring draws) — a places-fallback row
    (row["_category"] set) must keep its primary name even when lang is
    requested."""
    result = geocode.geocode_detailed("Blue Bottle Roastery", limit=5, lang="en")
    place_rows = [r for r in result["results"] if r.get("type") == "place"]
    for row in place_rows:
        assert "name_primary" not in row


# --- server.geocode(): preference wiring ------------------------------------


def test_server_geocode_no_lang_configured_is_byte_identical(geocode_cache):
    with_lang_call = server.geocode(query="München", limit=1)
    assert with_lang_call["results"][0]["name"] == "München"
    assert "name_primary" not in with_lang_call["results"][0]


def test_server_geocode_uses_stored_lang_when_omitted(geocode_cache):
    preferences.update(lang="en")
    result = server.geocode(query="München", limit=1)
    assert result["results"][0]["name"] == "Munich"
    assert result["results"][0]["name_primary"] == "München"


def test_server_geocode_per_call_lang_beats_stored_preference(geocode_cache):
    preferences.update(lang="de")
    result = server.geocode(query="München", limit=1, lang="en")
    assert result["results"][0]["name"] == "Munich"
    assert result["results"][0]["name_primary"] == "München"


# --- resolve_place(): division-kind rows only -------------------------------


def test_resolve_place_lang_swaps_division_candidate(geocode_cache):
    rows = geocode.resolve_place(
        "München", near_lat=48.14, near_lon=11.58, limit=3, lang="en",
    )
    division_rows = [r for r in rows if r["kind"] == "division"]
    assert division_rows, "expected at least one division candidate"
    top = division_rows[0]
    assert top["name"] == "Munich"
    assert top["name_primary"] == "München"


def test_resolve_place_lang_cache_key_does_not_leak_across_langs(geocode_cache):
    """The #410 resolve cache is keyed on lang (see geocode._resolve_cache_key)
    — a lang="en" call must not be replayed for a later call with no lang."""
    lang_rows = geocode.resolve_place(
        "München", near_lat=48.14, near_lon=11.58, limit=3, lang="en",
    )
    plain_rows = geocode.resolve_place(
        "München", near_lat=48.14, near_lon=11.58, limit=3,
    )
    lang_top = next(r for r in lang_rows if r["kind"] == "division")
    plain_top = next(r for r in plain_rows if r["kind"] == "division")
    assert lang_top["name"] == "Munich"
    assert plain_top["name"] == "München"
    assert "name_primary" not in plain_top


def test_server_resolve_place_no_lang_is_byte_identical(geocode_cache):
    result = server.resolve_place(query="München", near_lat=48.14, near_lon=11.58, limit=3)
    top = next(r for r in result["results"] if r["kind"] == "division")
    assert top["name"] == "München"
    assert "name_primary" not in top


def test_server_resolve_place_uses_stored_lang(geocode_cache):
    preferences.update(lang="en")
    result = server.resolve_place(query="München", near_lat=48.14, near_lon=11.58, limit=3)
    top = next(r for r in result["results"] if r["kind"] == "division")
    assert top["name"] == "Munich"
    assert top["name_primary"] == "München"


# --- place_details(): places-side variant swap ------------------------------

_WIEN_LAT, _WIEN_LON = 5.0, 100.0


def test_place_details_no_lang_is_byte_identical():
    result = overture.place_details(name="Kaffeehaus Wien", lat=_WIEN_LAT, lon=_WIEN_LON)
    assert result["name"] == "Kaffeehaus Wien"
    assert "name_primary" not in result


def test_place_details_lang_swaps_in_the_variant():
    result = overture.place_details(
        name="Kaffeehaus Wien", lat=_WIEN_LAT, lon=_WIEN_LON, lang="en",
    )
    assert result["name"] == "Vienna Coffee House"
    assert result["name_primary"] == "Kaffeehaus Wien"


def test_place_details_lang_with_no_variant_leaves_primary_untouched():
    result = overture.place_details(
        name="Kaffeehaus Wien", lat=_WIEN_LAT, lon=_WIEN_LON, lang="de",
    )
    assert result["name"] == "Kaffeehaus Wien"
    assert "name_primary" not in result


def test_place_details_lang_on_a_row_with_no_names_common_at_all():
    """Every other fixture row's names.common is an empty map, not absent —
    but the query-layer fallback (a duckdb.Error caught in
    _run_place_details_query) also has to hold for a genuinely missing
    names.common. Exercised together with an ordinary place here rather
    than a second fixture: an empty map already answers "no variant"
    through the same code path as a missing column would."""
    result = overture.place_details(
        name="Blue Bottle Roastery", lat=40.700000, lon=-73.900000, lang="en",
    )
    assert result["name"] == "Blue Bottle Roastery"
    assert "name_primary" not in result


def test_server_place_details_uses_stored_lang(geocode_cache):
    preferences.update(lang="en")
    result = server.place_details(name="Kaffeehaus Wien", lat=_WIEN_LAT, lon=_WIEN_LON)
    assert result["name"] == "Vienna Coffee House"
    assert result["name_primary"] == "Kaffeehaus Wien"


def test_server_place_details_per_call_lang_beats_preference(geocode_cache):
    preferences.update(lang="de")
    result = server.place_details(
        name="Kaffeehaus Wien", lat=_WIEN_LAT, lon=_WIEN_LON, lang="en",
    )
    assert result["name"] == "Vienna Coffee House"
    assert result["name_primary"] == "Kaffeehaus Wien"


def test_server_place_details_no_lang_configured_is_byte_identical():
    result = server.place_details(name="Kaffeehaus Wien", lat=_WIEN_LAT, lon=_WIEN_LON)
    assert result["name"] == "Kaffeehaus Wien"
    assert "name_primary" not in result


# --- lang_names table machinery ---------------------------------------------


def test_lang_names_table_is_materialized_beside_the_divisions_table(geocode_cache):
    table = geocode._local_divisions_table()
    lang_table = geocode._local_lang_names_table(table)
    assert lang_table is not None
    rows = duckdb.connect().execute(
        f"SELECT id, lang, name FROM read_parquet('{lang_table}') "
        "WHERE id = 'gers-div-munchen' ORDER BY lang"
    ).fetchall()
    langs = {r[1]: r[2] for r in rows}
    assert langs["en"] == "Munich"
    assert langs["fr"] == "Munich"
    assert langs["it"] == "Monaco di Baviera"
    assert langs["es"] == "Múnich"


def test_lang_variants_for_empty_without_a_table_or_ids():
    assert geocode._lang_variants_for(None, ["gers-div-munchen"], "en") == {}
    assert geocode._lang_variants_for("/does/not/matter.parquet", [], "en") == {}


def test_per_call_lang_is_normalized_at_the_tool_layer(geocode_cache):
    """resolve_lang strips/lowercases an explicit lang the same way the
    preferences write path does — " EN " and "en" must behave alike."""
    result = server.geocode("München", limit=1, lang=" EN ")
    assert result["results"][0]["name"] == "Munich"


def test_junk_per_call_lang_disables_lang_for_that_call(geocode_cache):
    """An explicit lang that fails the shape check disables lang for the
    call outright — the stored preference is not silently substituted for a
    value the caller actually sent."""
    server.preferences(lang="en")
    result = server.geocode("München", limit=1, lang="english")
    top = result["results"][0]
    assert top["name"] == "München"
    assert "name_primary" not in top


def test_lang_does_not_change_division_match_labels_in_resolve_place(geocode_cache):
    """#410: match labels (and therefore ranking) grade the caller's query
    against the *primary* name — a lang-swapped "Munich" must not demote the
    division the query "München" matched exactly."""
    base = geocode.resolve_place("München", near_lat=48.14, near_lon=11.58, limit=3)
    swapped = geocode.resolve_place(
        "München", near_lat=48.14, near_lon=11.58, limit=3, lang="en",
    )
    base_div = next(r for r in base if r["kind"] == "division")
    swapped_div = next(
        r for r in swapped
        if r["kind"] == "division" and r.get("name_primary") == "München"
    )
    assert swapped_div["match"] == base_div["match"]
