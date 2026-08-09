"""#221: the geocode ranking regression corpus.

One table, one assertion per row: for a query an agent would plausibly type,
which fixture division comes back *first*. It exists because #221 changes
_rank_key itself — the one search change that can move answers that are
already right — so every ranking behaviour the earlier issues established
needs to be pinned in a single place that fails loudly when it moves.

Every entry in RANKING_CORPUS is an answer the pre-#221 ranking already got
right and still gets right; FIXED_BY_221 holds the two it did not, kept
separate so the "nothing moved" half of the corpus stays readable as such.
Every entry is fixture-backed, so the corpus doubles as documentation of what
the divisions fixture is *for*: each row here is the reason some pair of
same-named rows exists in scripts/build_geocode_fixture.py.

What each group pins:

- population tiebreak (#47) between same-named, same-tier localities —
  Springfield, Cambridge, Portland. Both sides carry a population in every
  one of these, which is exactly the ordering #221 must not touch;
- the #47 no-population proxy chain — Fairview (region population),
  Hilltop (subtype rank);
- region-suffix parsing (#46) — "Portland, ME", "London, OH",
  "London, Ontario";
- the #53 variant retries — "St. Louis" -> Saint Louis, "Sao Paulo" ->
  São Paulo;
- the #214 alternate-name search and its prominence-over-namesake rule —
  Munich/Tokyo/Moskva/Vienna/Pressburg, each against the population-less
  literal namesake the live probe actually returned;
- the #215 fuzzy tier — the three live-verified typo probes;
- #221 itself (FIXED_BY_221) — "Zurich" and "東京".

Note Springfield resolves to the *Massachusetts* one here, not Missouri: the
fixture carries only the IL/MA pair (#47), with their real populations. The
corpus pins the fixture's own correct answer, not the real world's.
"""

import pytest

from placeroot import geocode

# (query, expected top-1 name, expected top-1 admin_context)
RANKING_CORPUS = [
    # #47: population decides between same-named, same-tier localities.
    ("Springfield", "Springfield", ["United States", "Massachusetts"]),
    ("Cambridge", "Cambridge", ["United Kingdom", "England"]),
    ("Portland", "Portland", ["United States", "Oregon"]),
    # #47: no population on either side — the proxy chain decides.
    ("Fairview", "Fairview", ["United States", "Illinois"]),
    ("Hilltop", "Hilltop", ["United States", "Illinois"]),
    # #46: an explicit region suffix overrides prominence entirely.
    ("Portland, ME", "Portland", ["United States", "Maine"]),
    ("London, OH", "London", ["United States", "Ohio"]),
    ("London, Ontario", "London", ["Canada", "Ontario"]),
    # #53: abbreviation and diacritic variant retries.
    ("St. Louis", "Saint Louis", ["United States", "Missouri"]),
    ("St Louis", "Saint Louis", ["United States", "Missouri"]),
    ("Saint Louis", "Saint Louis", ["United States", "Missouri"]),
    ("Sao Paulo", "São Paulo", ["Brazil"]),
    ("São Paulo", "São Paulo", ["Brazil"]),
    # #214: exonyms, each over the population-less literal namesake.
    ("Munich", "München", ["Germany", "Bavaria"]),
    ("Tokyo", "東京都", ["Japan"]),
    ("Moskva", "Москва", ["Russia"]),
    ("Vienna", "Wien", ["Austria"]),
    ("Pressburg", "Bratislava", ["Slovakia"]),
    # #215: typos, all three live-verified probes.
    ("Berekley", "Berkeley", ["United States", "California"]),
    ("Cinncinati", "Cincinnati", ["United States", "Ohio"]),
    ("Sna Francisco", "San Francisco", ["United States", "California"]),
    # Plain literal matches that must stay plain.
    ("San Francisco", "San Francisco", ["United States", "California"]),
    ("Berkeley", "Berkeley", ["United States", "California"]),
    ("New York", "New York", ["United States"]),
    ("Brooklyn", "Brooklyn", ["United States", "New York"]),
]

# The two answers #221 changes, kept out of the list above so that one reads
# as exactly what it is: the set that was already right, before and after.
# Appended below — the assertion is identical, the separation is documentation.
FIXED_BY_221 = [
    # A population-backed prefix match now outranks the exact-tier,
    # population-less namesake: 東京都's 13.9M over a Nagano neighborhood.
    ("東京", "東京都", ["Japan"]),
    # And the diacritic-folded pass runs even though "Zurich" literally
    # matched a populated (pop 190) Dutch village, which used to gate it off.
    ("Zurich", "Zürich", ["Switzerland"]),
]

RANKING_CORPUS += FIXED_BY_221


@pytest.mark.parametrize("query,name,admin_context", RANKING_CORPUS)
def test_ranking_corpus_top_1(geocode_cache, query, name, admin_context):
    results = geocode.geocode(query, limit=5)
    assert results, f"{query!r} returned nothing"
    assert results[0]["name"] == name
    assert results[0]["admin_context"] == admin_context


def test_corpus_queries_are_unique():
    """A duplicated query would silently weaken the corpus (two rows, one
    behaviour pinned) — and a contradictory pair would be worse."""
    queries = [row[0] for row in RANKING_CORPUS]
    assert len(queries) == len(set(queries))
