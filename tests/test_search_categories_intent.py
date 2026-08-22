"""Tests for search_categories' lexical phrase-intent fallback (#355)."""

import csv
import importlib.resources

from placeroot import categories, server


def test_phrase_intent_finds_mobile_phone_repair():
    results = categories.search_categories("fix my cracked phone screen")
    assert results
    assert results[0]["slug"] == "mobile_phone_repair"
    assert results[0]["confidence"] > 0


def test_phrase_intent_finds_a_gym():
    results = categories.search_categories("place to work out")
    assert results
    slugs = {r["slug"] for r in results}
    assert slugs & {"gym", "boxing_gym", "rock_climbing_gym", "sports_and_fitness_instruction"}


def test_stopword_only_query_returns_empty():
    assert categories.search_categories("the a to for") == []


def test_single_word_ranking_unchanged_from_whole_query_tiers():
    # Same ordering/tiers as before token matching was added: exact slug >
    # prefix > substring > path-segment. If these ever fail, whole-query
    # matching stopped being tried before the token fallback.
    for query in ("coffee_shop", "coffee", "grocery", "cafe"):
        results = categories.search_categories(query, limit=20)
        assert results
        for row in results:
            assert 0 <= row["confidence"] <= 1

    exact = categories.search_categories("coffee_shop")
    assert exact[0]["slug"] == "coffee_shop"
    assert exact[0]["confidence"] == 1.0


def test_confidence_present_and_descending():
    for query in ("coffee", "fix my cracked phone screen", "place to work out"):
        results = categories.search_categories(query, limit=20)
        assert results
        confidences = [r["confidence"] for r in results]
        assert confidences == sorted(confidences, reverse=True)
        for r in results:
            assert isinstance(r["confidence"], float)
            assert 0 <= r["confidence"] <= 1


def test_high_document_frequency_token_alone_stays_bounded_by_substring_tiers():
    # "shop" is a substring of many slugs (bike_repair... no, but many
    # slugs literally contain "shop"), so it should still hit the
    # whole-query substring tier (not explode via the token fallback) and
    # stay ranked by the old tiers.
    results = categories.search_categories("shop", limit=50)
    assert results
    for row in results:
        assert "shop" in row["slug"] or any("shop" in seg for seg in row["path"])


def test_lexicon_slugs_all_exist_in_taxonomy():
    all_slugs = {row["slug"] for row in categories._load_categories()}
    csv_path = importlib.resources.files("placeroot") / "data" / "category_synonyms.csv"
    with csv_path.open("r", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=";")
        rows = list(reader)
    assert rows, "lexicon should not be empty"
    data_rows = rows[1:]
    assert len(data_rows) >= 50, "lexicon should cover a meaningful number of intents"
    missing = []
    for row in data_rows:
        if not row or not row[0].strip():
            continue
        slug = row[0].strip()
        if slug not in all_slugs:
            missing.append(slug)
    assert not missing, f"lexicon slugs missing from taxonomy: {missing}"


def test_server_tool_passes_confidence_through():
    result = server.search_categories(query="fix my cracked phone screen")
    assert "error" not in result
    assert result["results"]
    for row in result["results"]:
        assert "confidence" in row
        assert 0 <= row["confidence"] <= 1


def test_grocery_stores_resolves_to_grocery_store_not_a_sibling():
    # rice_shop inherits {grocery, store} from the grocery_store segment of
    # its taxonomy path; without slug-word priority it tied grocery_store
    # on coverage and won on shorter slug, and _category_slug accepted the
    # raw plural phrase's lexical guess before ever trying the singularized
    # exact slug (#357).
    results = categories.search_categories("grocery stores")
    assert results
    assert results[0]["slug"] == "grocery_store"
    assert server._category_slug("grocery stores") == "grocery_store"


def test_common_plural_phrases_resolve_to_canonical_slugs():
    assert server._category_slug("gas stations") == "gas_station"
    assert server._category_slug("book stores") == "bookstore"
    assert server._category_slug("coffee shops") == "coffee_shop"


def test_lexical_fallback_confidence_stays_below_whole_query_tiers():
    # The docs promise a synonym/token hit never outranks any whole-query
    # tier; _TOKEN_MATCH_MAX = 0.7 silently broke that against tier 3's
    # 0.6 (#357).
    assert categories._TOKEN_MATCH_MAX < min(categories._TIER_CONFIDENCE.values())


def test_plural_query_folds_to_the_singular_slug():
    # "coffee shops" must score 2/2 against coffee_shop's {coffee, shop},
    # beating single-token coffee-synonym rows like cafe — the regression
    # server._category_slug("coffee shops") caught when the fold was missing.
    results = categories.search_categories("coffee shops")
    assert results[0]["slug"] == "coffee_shop"
    # length-3 "gas" must not be stemmed into nonsense.
    assert categories.search_categories("gas")[0]["slug"].startswith("gas")
