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


# --- #463: a namesake locality outranks the region it sits in ---------------
#
# White-box, hand-made rows: the fixture carries no region/locality pair that
# shares a name (its "New York" is the state alone), and the live pair that
# motivated the rule — São Paulo state (45.5M) over São Paulo city (11.5M),
# 259 km apart — is exactly what the tuple below reproduces.


def _row(id_, name, subtype, population, region, country="BR", tier=3, **kw):
    row = {
        "id": id_,
        "name": name,
        "subtype": subtype,
        "country": country,
        "region": region,
        "population": population,
        "admin_context": [],
        "lat": 0.0,
        "lon": 0.0,
        "_tier": tier,
    }
    row.update(kw)
    return row


def _ranked_ids(rows, query, region_population=None):
    geocode._flag_namesake_localities(rows, query)
    ordered = sorted(rows, key=lambda r: geocode._rank_key(r, query, region_population or {}))
    return [r["id"] for r in ordered]


def test_namesake_locality_outranks_the_region_it_sits_in():
    # The g05 shape: both exact-tier, both populated, state ~4x the city.
    state = _row("sp-state", "São Paulo", "region", 46_000_000, "BR-SP")
    city = _row("sp-city", "São Paulo", "locality", 12_300_000, "BR-SP")
    assert _ranked_ids([state, city], "São Paulo") == ["sp-city", "sp-state"]
    # ...and the region stays the runner-up: an unrelated strong-tier
    # namesake hamlet does not ride the rule past it.
    hamlet = _row("sp-hamlet", "São Paulo", "locality", 865, "BR-PA")
    assert _ranked_ids([state, hamlet, city], "São Paulo") == ["sp-city", "sp-state", "sp-hamlet"]


def test_namesake_locality_moves_ahead_of_its_region_and_nothing_else():
    # The live "Mexico" shape: Overture carries Ciudad de México both as a
    # locality and as the region MX-CMX (same 9.2M), under the country México
    # (129.9M). The city must pass its region, not the country.
    country = _row("mx", "México", "country", 129_900_000, None, country="MX")
    cdmx_region = _row(
        "cdmx-region", "Ciudad de México", "region", 9_209_944, "MX-CMX", country="MX"
    )
    cdmx_city = _row("cdmx-city", "Ciudad de México", "locality", 9_209_944, "MX-CMX", country="MX")
    rows = [cdmx_region, cdmx_city, country]
    assert _ranked_ids(rows, "Ciudad de México") == ["mx", "cdmx-city", "cdmx-region"]
    assert cdmx_city["_namesake_locality"] == 9_209_944


def test_namesake_locality_of_a_country():
    country = _row("sg", "Singapore", "country", 4_839_400, None, country="SG")
    city = _row("sg-city", "Singapore", "locality", 4_000_000, None, country="SG")
    assert _ranked_ids([country, city], "Singapore") == ["sg-city", "sg"]


def test_namesake_locality_below_share_threshold_does_not_displace_region():
    state = _row("ks-state", "Kansas", "region", 2_970_606, "US-KS", country="US")
    town = _row("ks-town", "Kansas", "locality", 5_000, "US-KS", country="US")
    assert _ranked_ids([town, state], "Kansas") == ["ks-state", "ks-town"]


def test_region_without_namesake_locality_orders_as_before():
    # "Where is Kansas": the state over a same-named village in *another*
    # state, decided by population exactly as before #463.
    state = _row("ks-state", "Kansas", "region", 2_970_606, "US-KS", country="US")
    village = _row("ks-il", "Kansas", "locality", 827, "US-IL", country="US")
    rows = [village, state]
    geocode._flag_namesake_localities(rows, "Kansas")
    assert not any(k.startswith("_namesake") for r in rows for k in r)
    assert _ranked_ids(rows, "Kansas") == ["ks-state", "ks-il"]


def test_namesake_rule_leaves_the_tokyo_221_case_alone():
    # #221: a population-less exact-tier neighborhood vs the populated
    # prefix-tier prefecture. Neither is a region/locality pair sharing a
    # name, so the pre-pass flags nothing and the prefecture still wins.
    prefecture = _row("tokyo-to", "東京都", "locality", 13_929_286, "JP-13", country="JP", tier=2)
    neighborhood = _row("tokyo-nagano", "東京", "neighborhood", None, "JP-20", country="JP", tier=3)
    assert _ranked_ids([neighborhood, prefecture], "東京") == ["tokyo-to", "tokyo-nagano"]


def test_namesake_locality_in_another_region_does_not_promote():
    # A populous "São Paulo" that is NOT inside São Paulo state must not be
    # read as the state's namesake city.
    state = _row("sp-state", "São Paulo", "region", 46_000_000, "BR-SP")
    elsewhere = _row("sp-elsewhere", "São Paulo", "locality", 12_300_000, "BR-RS")
    assert _ranked_ids([elsewhere, state], "São Paulo") == ["sp-state", "sp-elsewhere"]


def test_namesake_rule_ignores_fuzzy_and_weak_tier_rows():
    state = _row("sp-state", "São Paulo", "region", 46_000_000, "BR-SP")
    # Same city, but reached through the #215 fuzzy tier: not a literal match.
    fuzzy_city = _row(
        "sp-fuzzy",
        "São Paulo",
        "locality",
        12_300_000,
        "BR-SP",
        tier=1,
        _fuzzy=True,
        _similarity=0.9,
    )
    assert _ranked_ids([fuzzy_city, state], "Sao Paolo") == ["sp-state", "sp-fuzzy"]
    # A substring-tier region is not "the caller's string names this place".
    weak_state = _row("sp-weak", "São Paulo", "region", 46_000_000, "BR-SP", tier=1)
    city = _row("sp-city", "São Paulo", "locality", 12_300_000, "BR-SP", tier=1)
    rows = [weak_state, city]
    geocode._flag_namesake_localities(rows, "Paulo")
    assert not any(k.startswith("_namesake") for r in rows for k in r)


def test_namesake_flag_never_reaches_returned_rows(geocode_cache, monkeypatch):
    state = _row("sp-state", "São Paulo", "region", 46_000_000, "BR-SP", lat=-22.07, lon=-48.43)
    city = _row("sp-city", "São Paulo", "locality", 12_300_000, "BR-SP", lat=-23.55, lon=-46.63)
    monkeypatch.setattr(geocode, "_query_divisions", lambda *a, **k: [dict(state), dict(city)])
    results = geocode.geocode("São Paulo", limit=5)
    assert [r["id"] for r in results[:2]] == ["sp-city", "sp-state"]
    for r in results:
        assert not any(k.startswith("_") for k in r), r
