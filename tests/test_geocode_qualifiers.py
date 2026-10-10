"""Bare trailing-word qualifiers, the resolve cache's limit, tier folding,
explicit country= on the degrade paths, word-boundary prefixes, and the
per-table memos — all exercised offline against the pure helpers (and, where
a whole resolve is needed, with the query functions monkeypatched the way
test_resolve_place.py does)."""

import pytest

from placeroot import geocode, overture

# ---------------------------------------------------------------------------
# resolve cache: the key carries no limit, so the value must not either
# ---------------------------------------------------------------------------


def _three_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
    return [
        {
            "id": f"place-{i}", "name": f"Example {i}", "category": "cafe",
            "basic_category": "cafe", "operating_status": "open",
            "confidence": 0.5, "lat": 1.0, "lon": 2.0, "distance_m": 10 * i,
        }
        for i in range(3)
    ]


def test_a_small_limit_does_not_truncate_what_the_cache_serves_later(monkeypatch):
    monkeypatch.setattr(geocode, "geocode", lambda *a, **k: [])
    monkeypatch.setattr(overture, "find_places", _three_places)
    geocode.clear_resolve_session()

    first = geocode.resolve_place("Example", near_lat=1.0, near_lon=2.0, limit=1)
    assert len(first) == 1
    # Cut the data source off: anything that comes back now came from the cache.
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [])
    again = geocode.resolve_place("Example", near_lat=1.0, near_lon=2.0, limit=3)
    assert [r["id"] for r in again] == ["place-0", "place-1", "place-2"]
    assert geocode.resolve_place("Example", near_lat=1.0, near_lon=2.0, limit=2) == again[:2]


# ---------------------------------------------------------------------------
# _match_tier folds case, diacritics and punctuation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, query",
    [
        ("Notre-Dame", "notre dame"),
        ("Straße", "STRASSE"),
        ("São Paulo", "sao paulo"),
        ("Saint-Étienne", "saint etienne"),
    ],
)
def test_match_tier_is_exact_across_case_diacritics_and_punctuation(name, query):
    assert geocode._match_tier(name, query) == 3


def test_match_tier_prefix_and_substring_floor():
    assert geocode._match_tier("Notre-Dame de Paris", "notre dame") == 2
    assert geocode._match_tier("Dame", "notre dame") == 1
    assert geocode._match_tier("New York", "york") == 1
    # Never below 1: the caller already filtered non-matches out.
    assert geocode._match_tier("Anything", "???") == 1


# ---------------------------------------------------------------------------
# prefix labels need a word boundary
# ---------------------------------------------------------------------------


def test_prefix_label_requires_a_word_boundary():
    assert geocode._place_match_label("Mall of America", "Ma", set()) == "contains"
    assert geocode._place_match_label("Mall of America", "Mall", set()) == "prefix"
    assert geocode._place_match_label("Mall of America", "mall of", set()) == "prefix"
    assert geocode._place_match_label("Mall", "Mall of America", set()) == "prefix"
    assert geocode._place_match_label("Mall of America", "Mall of America", set()) == "exact"


def test_is_word_prefix():
    assert geocode._is_word_prefix("mall", "mall of america")
    assert geocode._is_word_prefix("mall", "mall")
    assert geocode._is_word_prefix("mall", "mall-mart")
    assert not geocode._is_word_prefix("ma", "mall of america")
    assert not geocode._is_word_prefix("", "mall")
