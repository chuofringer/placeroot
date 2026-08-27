"""The typo tier refuses a match it only half earned (issue #431).

resolve_named_place("Gare du Nord") answered "Garen Du", a hamlet in
Côtes-d'Armor: _parse_region_suffix read the trailing "Nord" as Cameroon's
CM-NO, the region-filtered fuzzy pass found nothing, and the retry that
drops the filter then scored 0.975 against what was left of the query —
"Gare du". A third of the string was never matched by anything, and
from_to("Louvre Museum" -> "Gare du Nord") routed 430 km to the hamlet and
said too_far.

The refusal takes two measures together (see the #431 block in geocode.py
for the calibration table): a fuzzy row is dropped only when the row's name
fails to account for every query token AND its whole-query similarity is
under _FUZZY_WHOLE_QUERY_FLOOR. Either measure alone cuts through the
middle of the measured data — coverage alone refuses "Sna Francisco"
("Sna"/"san" is 0.556 as a lone token), similarity alone cannot be set
anywhere between corrections that reach down to 0.939 and homonyms that
reach up to 0.948.

The fixture analog of the live defect is "Berkely Texas": the same shape
one for one — a trailing token parsed as a region, no match inside it, the
filter dropped, and the surviving fuzzy row sitting in a different region
altogether. Its partner "Berekley, CA" is the case that must keep working,
and differs in exactly one respect: the row it finds really is in the
region the suffix named.
"""

import pytest

from placeroot import geocode, server

# The fixture pair. Both are a misspelling plus a US state; only the second
# names the state the answer is actually in.
REFUSED = "Berkely Texas"
KEPT = "Berekley, CA"


# --------------------------------------------------------------------
# the precondition: the fuzzy row is still offered
# --------------------------------------------------------------------


def test_the_half_matched_row_is_still_what_the_tier_finds(geocode_cache):
    """Without this, the refusal below would prove nothing: the query has
    to reach a fuzzy answer for refusing it to mean anything."""
    rows = geocode.geocode(REFUSED, limit=5)
    assert rows, f"{REFUSED!r} found nothing at all"
    assert rows[0]["name"] == "Berkeley"
    assert rows[0]["matched_by"] == "fuzzy"
    assert "California" in rows[0]["admin_context"]


# --------------------------------------------------------------------
# the refusal
# --------------------------------------------------------------------


def test_a_half_matched_fuzzy_row_is_not_the_resolver_s_answer(geocode_cache):
    assert geocode.resolve_named_place(REFUSED) is None


def test_the_gare_du_nord_shape_is_refused_offline(monkeypatch):
    """The live row itself, offered directly to the resolver. "Garen Du"
    covers "Gare" and "du" and says nothing about "Nord"; against the whole
    string it scores 0.883, well under the floor."""
    row = {
        "name": "Garen Du", "lat": 48.463711, "lon": -3.48781, "id": "garen-du",
        "type": "locality", "rank_score": 0.34, "matched_by": "fuzzy",
        "admin_context": ["France", "Bretagne", "Côtes-d'Armor"],
    }
    monkeypatch.setattr(geocode, "geocode", lambda query, limit=None, lang=None: [row])
    assert geocode.resolve_named_place("Gare du Nord") is None


def test_refusal_needs_both_measures_to_fail(geocode_cache):
    """The rule, stated as the two measures rather than as its outcome.

    "Berkely Texas" fails both against the Berkeley row: "Texas" is
    accounted for by nothing in the name, and the whole-query similarity is
    0.898. Move either one and the row survives.
    """
    row = geocode.geocode(REFUSED, limit=5)[0]
    assert not geocode._fuzzy_name_covers_tokens(row["name"], REFUSED.split())
    whole = geocode._jaro_winkler(
        geocode._normalize_for_match(row["name"]),
        geocode._normalize_for_match(REFUSED),
    )
    assert whole == pytest.approx(0.898, abs=0.001)
    assert whole < geocode._FUZZY_WHOLE_QUERY_FLOOR
    assert geocode._fuzzy_row_is_too_weak(REFUSED, row)


# --------------------------------------------------------------------
# what must not move
# --------------------------------------------------------------------


def test_known_good_typo_corrections_still_resolve(geocode_cache):
    """The #215 corpus through the named-place resolver. "Sna Francisco" is
    the one that needs the similarity escape hatch: its typo token is
    unrecognizable on its own, so coverage alone would refuse it."""
    for query, expected in [
        ("Berekley", "Berkeley"),
        ("Cinncinati", "Cincinnati"),
        ("Sna Francisco", "San Francisco"),
        ("Sao Paluo", "São Paulo"),
    ]:
        resolved = geocode.resolve_named_place(query)
        assert resolved is not None, query
        assert resolved["name"] == expected, query


def test_a_region_suffix_the_answer_honors_is_not_held_against_it(geocode_cache):
    """The kept half of the pair: "Berekley, CA" and "Berkely Texas" differ
    only in whether the row
    lies in the region the suffix named. Berkeley is in California, so "CA"
    is not an unmatched token — it is the part of the query the search
    already used."""
    assert geocode.resolve_named_place(KEPT)["name"] == "Berkeley"
    assert geocode.resolve_named_place("Cinncinati, OH")["name"] == "Cincinnati"


def test_single_token_queries_are_untouched(geocode_cache):
    """The failure mode is a query whose remainder went unaccounted for; a
    one-word query has no remainder. Asserted at the predicate so this holds
    however weak the row's similarity is."""
    row = geocode.geocode("Berekley", limit=5)[0]
    assert row["matched_by"] == "fuzzy"
    assert not geocode._fuzzy_row_is_too_weak("Berekley", row)
    assert not geocode._fuzzy_row_is_too_weak("Berekley", dict(row, name="Bxrkeley"))


def test_literal_matches_are_never_refused(geocode_cache):
    """Only fuzzy rows are in scope. A substring or prefix answer is a
    literal answer to what was typed — weak, but not a guess."""
    for query in ("Downtown Brooklyn", "Mountain View", "Saint Louis"):
        assert geocode.resolve_named_place(query) is not None, query


# --------------------------------------------------------------------
# the composes surface the honest error
# --------------------------------------------------------------------


def test_find_near_reports_not_found_with_the_try_hint(geocode_cache):
    payload = server.find_near(category="cafe", near=REFUSED, radius_m=500)
    assert payload["error"] == "not_found"
    assert REFUSED in payload["detail"]
    assert "resolve_place" in payload["try"]
    assert "near_lat" in payload["try"] and "city" in payload["try"]


def test_from_to_reports_not_found_with_the_try_hint(geocode_cache):
    payload = server.from_to(REFUSED, "Brooklyn")
    assert payload["error"] == "not_found"
    assert REFUSED in payload["detail"]
    assert "resolve_place" in payload["try"]
