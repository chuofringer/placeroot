"""Tests for search_categories: bundled Overture taxonomy lookup (issue #116)."""

from placeroot import categories, server


def test_coffee_query_finds_coffee_shop_with_full_path():
    results = categories.search_categories("coffee")
    assert results
    slugs = {r["slug"] for r in results}
    assert "coffee_shop" in slugs
    for row in results:
        assert isinstance(row["path"], list)
        assert row["path"]
    coffee_shop = next(r for r in results if r["slug"] == "coffee_shop")
    assert coffee_shop["path"] == ["eat_and_drink", "cafe", "coffee_shop"]


def test_exact_slug_match_ranks_first():
    results = categories.search_categories("coffee_shop")
    assert results
    assert results[0]["slug"] == "coffee_shop"


def test_sibling_categories_are_disambiguated():
    results = categories.search_categories("grocery", limit=20)
    slugs = {r["slug"] for r in results}
    assert "grocery_store" in slugs
    assert "specialty_grocery_store" in slugs


def test_limit_is_respected():
    results = categories.search_categories("restaurant", limit=3)
    assert len(results) <= 3


def test_empty_query_returns_empty_results_via_server_tool():
    assert server.search_categories(query="") == {"results": []}
    assert server.search_categories(query="   ") == {"results": []}


def test_limit_out_of_range_is_a_bad_request():
    assert server.search_categories(query="coffee", limit=0) == {
        "error": "bad_request",
        "detail": "limit must be between 1 and 50",
    }
    assert server.search_categories(query="coffee", limit=51) == {
        "error": "bad_request",
        "detail": "limit must be between 1 and 50",
    }


def test_returned_slug_is_directly_usable_from_bundled_csv():
    results = categories.search_categories("coffee")
    all_slugs = {row["slug"] for row in categories._load_categories()}
    assert results[0]["slug"] in all_slugs
