"""geocode / reverse_geocode / resolve_place on Overture data (#10) — no Nominatim, no
third-party geocoding calls.

geocode ranks divisions (locality/neighborhood/region/country) by name match
quality, falling back to places when divisions alone don't fill `limit`.
reverse_geocode finds the nearest address point and its containing division
chain, degrading to divisions-only when the addresses theme is unreachable
or missing (addresses is a much newer, less complete Overture theme than
places/divisions, so this is the realistic failure mode, not a hypothetical
one). resolve_place (#22) merges geocode()'s divisions with a name-filtered
find_places search into one typed, ranked list of GERS ids — see its own
docstring below for why that's a distinct tool from geocode() rather than
just a wider `type` filter on it (it needs a location hint to bound the
places search, which geocode() never has).

Ranking is deterministic and cheap on purpose: exact name match beats
prefix beats substring; within a tier, ties break on population (or, when
population is null/absent — see "#47" below — a documented proxy), then a
fixed subtype-size ordering (locality is what most free-text queries mean
by "a place"), then alphabetically by id for full determinism. No
Nominatim/geocoding API, no scoring model — just SQL ILIKE plus Python-side
ranking.

--- #43: a local name table, materialized once per release ---

Overture's divisions theme is small relative to places (~4-5M rows
worldwide), but every geocode() call was still scanning it live over S3 —
5-9s per query, dominated by the network round-trip and remote parquet
footer reads, not by row count. Unlike places (a point-radius tool, served
by cache.py's per-tile materialization keyed on where queries land),
geocode queries are name lookups with no spatial locality to exploit, so a
tile cache doesn't fit; instead, the *entire* divisions/type=division name
table is materialized locally, once per Overture release, the first time
geocode() runs. Only the columns geocode.py needs survive the copy, and
the `hierarchies` struct is flattened to a plain admin-chain name list —
keeping the raw nested struct roughly doubled the materialized table's
size for no benefit here (nothing downstream needs division_id/subtype
per hierarchy entry, only the names).

This blocks the first caller (unlike cache.py's places tiles, which hand a
missing-tile fetch to a background thread and answer the *triggering*
query from upstream directly — issue #31): a name-table build is a single
~20-30s COPY that happens once per release, not a per-query cost, and
geocode's answer isn't materially useful without it, so there's nothing
better to do with that first caller than wait (with a log line marking it,
so it's visible rather than a silent stall). PLACEROOT_CACHE=off skips
materialization entirely and falls back to direct upstream ILIKE scans —
the same slow-but-correct path this module had before #43.

The materialized table lives under cache.py's cache dir, release-keyed,
and is picked up by its size-based LRU eviction the same as places tiles
(it is not fenced off from that pool) — the tradeoff is documented, not
solved: a heavy geocode workload competing with a heavy places workload
for the same PLACEROOT_CACHE_MAX_MB budget may want that budget raised
(the divisions name table alone runs a couple hundred MB with ZSTD).

addresses does NOT get the same treatment: reverse_geocode's address
lookup (_nearest_address, below) is already a point-radius query with a
bbox-pushdown prefilter — the fast path for a spatial lookup. A name-table
materialization fixes a *name-search* being slow; addresses has no such
problem, since it's never searched by name.

--- #46: "City, ST" parsing ---

Overture division names are bare ("Chicago", never "Chicago, IL"), so a
literal query for "Chicago, IL" against names.primary matches nothing.
_parse_region_suffix splits a trailing ", ST" (or, without a comma, a
trailing " ST") region token off the query, resolving it against an
embedded 50-state abbreviation/full-name map first (the common case, and
free of any extra query), then falling back to a lookup against the
region-subtype rows of the (already-materialized, so cheap) local name
table for names the embedded map doesn't recognize — so "London, Ontario"
resolves too, not just US states. geocode() then constrains divisions
candidates to the resolved region using each row's own `region` column,
not a second string match against the query: Overture computes `region`
from the same containing-hierarchy chain admin_context reads, so filtering
on it is filtering by hierarchy membership. An unrecognized suffix (no
state/region match) is left on the query untouched and degrades to
today's plain substring behavior; a recognized suffix that turns up zero
region-constrained candidates also degrades to an unconstrained search of
the original query, rather than returning an empty result for a query
that would otherwise have matched something.

--- #47: prominence disambiguation ---

Overture's divisions rows do carry a `population` column (confirmed live:
present but null for most rows worldwide, populated for most well-known
localities/regions) — used directly as the primary tiebreak after match
tier. When neither candidate in a tied comparison has a population value,
ranking falls back to a documented, deterministic proxy chain: subtype
rank (the existing locality > neighborhood > ... ordering), then hierarchy
depth (shallower wins — a weak but explainable signal that a division
sitting closer to the top of its hierarchy is more likely the
canonical/primary entry for its name, since minor places tend to pick up
extra intermediate hierarchy layers), then the population of the row's own
containing region (a division in a more populous state/province edges out
one in a less populous one when nothing else distinguishes them). No ML,
no external prominence dataset — every input here already exists in
Overture's own divisions rows.

--- #53: name-variant normalization (St./Saint, diacritics, ...) ---

Overture's canonical division names pick one convention (never both) —
"Saint Louis" vs "St. Louis", "Sao Paulo" vs "Sao Paulo" with a tilde — but
a free-text query can spell it either way. geocode() runs the literal query
first, exactly as before; only when that literal query doesn't reach an
exact-or-prefix division match backed by real prominence (tier 3/2 *and*
carrying a population figure — see below for why population, specifically,
gates this) does a second pass retry with normalized variants: bidirectional
token swaps for a small set of common abbreviations (St./Saint, Ft./Fort,
Mt./Mount, and — leading-token only, since a bare "N" mid-query is too
ambiguous to touch — N./S./E./W. vs North/South/East/West), plus a
diacritic-folded pass (unicodedata NFD, combining marks stripped) using
DuckDB's own strip_accents() on the name column, so accented and unaccented
spellings match each other regardless of which one Overture's canonical
name uses.

The retry gate checks for population, not just tier, because a literal
exact match is not by itself proof the query has been "found": Overture's
divisions include plenty of tiny, unpopulated places sharing a name with
somewhere much more prominent — a literal query for "St. Louis" also
exact-matches a handful of small villages worldwide literally spelled that
way (verified live), while the famous Missouri city is canonically named
"Saint Louis" in Overture's own data and only turns up through the
abbreviation-variant retry. A tier-3/2 match with a real population value
behind it is a much stronger signal the literal search already found the
right thing, and skips the (otherwise pointless) extra query.

That gate no longer covers both halves of the second pass: #221 took the
diacritic-folded half out from under it whenever a local table (#43) is
available, because how prominent the *unfolded* spelling turned out to be
says nothing about whether the folded one is worth looking for ("Zurich"
literally matches a 190-person Dutch village, which was enough to hide
Zürich entirely). The abbreviation half stays gated on every path, and the
folded half stays gated when there is no local table to run it against
cheaply. See the #221 section at the end of this docstring.

Variant-sourced rows are tagged (_rank_key's *last* tiebreak, after the #47
population/proxy chain) so a variant match's own prominence still wins
against a same-tier literal match the normal #47 way — literal only breaks
a full tie, it doesn't override population. Returned names are always
Overture's untouched canonical spelling; only the matching step is
normalized.

--- #215: fuzzy fallback tier for typos ---

#53 fixes spellings we can enumerate (St./Saint, diacritics); it does
nothing for a plain typo. Live before this: "Berekley" and "Cinncinati"
returned nothing at all, and "Sna Francisco" fell through to the places
fallback and answered "Snags N Burgs Cafe" (the substring scan of a
misspelling #216's docstring calls out).

So: when the literal search — including the #53 variant retries — comes
back *empty*, run one more pass over the local materialized divisions
table (#43) matching on edit distance instead of substrings:
jaro_winkler_similarity(folded name, folded query) >=
_FUZZY_SIMILARITY_THRESHOLD, ordered by similarity then population. The
trigger is deliberately emptiness alone: a literal substring hit is a real
answer to the string the caller actually typed, and there is no honest
tier-based reading of "weak" that doesn't demote some of those.

What gets matched is the query's *name* half — "Berekley" out of
"Berekley, CA" — with the region the suffix named passed as a filter, and
dropped on a miss like the literal search drops it. The literal path
answers a region-constrained miss by retrying the whole original string,
suffix included, and nothing is within edit distance of that, so fuzzing
what the literal search happened to end up holding would lose the
correction on the most common shape a place gets written in.

Two properties this tier is built around:

- It never scans upstream. jaro_winkler over the whole local table is
  0.26s (measured, 4.65M names); the same predicate against S3 would be a
  full-theme read with nothing to prune by, which is exactly the cost
  #105/#216 exist to avoid. No local table (cache off, or materialization
  failed) means no fuzzy tier — an unavailable nicety, not a fallback to
  something expensive.
- A fuzzy hit answers a *different* string than the one typed, so it sorts
  below every literal tier (_rank_key's leading term, ahead of even tier
  3), scores below the substring tier, and says so twice over: a note
  naming the spelling it corrected to, and "matched_by": "fuzzy" on the
  row itself, so neither an agent reading prose nor resolve_place (which
  has no note to read) can mistake a correction for a match. A fuzzy hit also stands down the
  places fallback: the query text is a known misspelling at that point,
  and substring-matching a typo against the places theme is how
  "Snags N Burgs Cafe" happened.

--- #214: alternate names (Overture's names.common) ---

Everything above matches `names.primary` — the *endonym*, the name in the
local language. Overture also ships `names.common`, a ~100-language map of
localized names (4.29M alternates across 1.53M divisions, release
2026-07-22.0), which we used to discard at materialization time. Measured
live before this: "Munich" answered Munich, North Dakota (München absent
from the candidate pool entirely), "Tokyo" answered Tokyo, Papua New Guinea,
"Moskva" answered Moskva, Tajikistan.

So the #43 materialization writes a *second* local parquet next to the
divisions table: one row per (division id, folded alternate name), from
`unnest(map_values(names.common))`, grouped so each folded spelling appears
once per division and dropping alternates that fold to the same string as
the primary name. _query_divisions unions an ILIKE against that table
(joined back to the divisions table for the row's real columns) into the
literal search.

Alt rows are tagged `_variant`, exactly like #53's abbreviation/diacritic
retry hits, and for the same reason: they were found through a spelling the
caller typed but Overture doesn't call canonical, so their own prominence
(the #47 chain) decides against a same-tier literal match and the literal
tiebreak only settles a full tie. That is what makes "Munich" resolve to
München — both are exact-tier matches, and München carries a population
while Munich, ND doesn't. The returned `name` is always Overture's
canonical primary spelling; an optional `matched_name` says which alternate
actually matched, so an agent can see that "Munich" found "München" rather
than guessing whether the answer is a namesake.

Alternates are stored pre-folded (lowercased, accents stripped) and the
query is folded the same way in Python, so matching is case- and
diacritic-insensitive without a per-row function call at query time. Both
folds go through the explicit _UNFOLDED_LETTERS map on top of
strip_accents, which leaves ß/ø/Ł alone (duckdb#15706) — see that constant.

Cost, measured live on release 2026-07-22.0: 15.6s added to the one-time
materialization, 95.3MB ZSTD on disk against the primary table's 197.1MB,
0.17-0.42s per join lookup. Verified live on the same release: "Munich" ->
München, "Tokyo" -> 東京都, "Moskva" -> Москва, "Vienna" -> Wien,
"Pressburg" -> Bratislava. Graceful by construction: a cache directory
written before this feature has no alt table, and the alt query is simply
skipped — primary-only behavior, no error.

The #215 fuzzy tier deliberately stays on primary names only; see
_query_divisions_fuzzy.

--- #221: prominence over tier, and the fold stops being gated ---

Two coupled leftovers from the above, both measured live on 2026-07-22.0.

Tier used to dominate _rank_key outright, so an exact-tier match beat every
prefix-tier one regardless of what stood behind them: "東京" answered a
population-less neighborhood in 長野県, because 東京 is exactly its name and
only a prefix of 東京都's (13.6M). _rank_key now lets a nonzero population
outrank the tier *within* the exact/prefix group and only against a row
carrying no prominence at all — the same prominence-over-namesake judgement
#214 already made between literal and alternate-name hits, applied to the
tier ladder. Nonzero, not merely non-NULL: Overture ships divisions with an
explicit population of 0, and those must not rescue anything past an exact
match. Two populated rows still order by tier first, and a substring hit
still cannot leapfrog either. See _rank_key.

And the #53 second pass was gated on "no exact/prefix literal match carries
a population", which the diacritic half of it cannot live with: "Zurich"
literally matches a Dutch village of 190 people, so the gate declared the
literal search good enough and the folded pass never ran — Zürich (443k)
was absent from the candidate pool entirely, since ILIKE '%Zurich%' never
matches "Zürich". The folded pass now runs unconditionally whenever there
is a local table (#43) to run it against; without one it stays gated, since
upstream that extra pass is an unprunable full-theme ILIKE rather than the
0.2s local predicate the change was measured on. The abbreviation half
stays gated on every path. Live after (cache warm): "Zurich" -> Zürich, CH;
"東京" -> 東京都.

tests/test_geocode_ranking.py pins the answers all of the above already got
right, as a corpus, precisely because a _rank_key change can move them.
"""

import logging
import math
import os
import re
import time
import unicodedata
from pathlib import Path

import duckdb

from placeroot import addresses, cache, overture, release
from placeroot.errors import AmbiguousArea

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
MAX_LIMIT = 25
DIVISION_OVERFETCH = 50  # rows pulled per theme before Python-side ranking trims to `limit`

# #83: bbox radius for the places-theme fallback once an anchor point (a
# division match already in hand, or one derived from a trailing word in the
# query — see _fallback_anchor) is available. Same "same metro area" idea as
# resolve_place's own _RESOLVE_PLACE_RADIUS_M, just defined here too since
# geocode() needs it before that constant's #22 section further down.
_PLACES_FALLBACK_RADIUS_M = 30_000

# Subdirectory (under cache.cache_dir()/<release>/) for the #43 materialized
# divisions name table — kept distinct from cache.py's own places/<theme>
# tile layout so the two never collide on a filename, even though they
# share the same cache dir and eviction pool.
_DIVISIONS_TABLE_SUBDIR = "geocode-divisions"
_DIVISIONS_TABLE_FILENAME = "table.parquet"
# #214: the alternate-name table, written alongside the primary one.
_ALT_NAMES_TABLE_FILENAME = "alt_names.parquet"

# Bigger number = ranked higher among same name-match tier. Chosen so a
# free-text query like "Springfield" surfaces the city before a same-named
# neighborhood or the containing state, which is the common case for an
# agent asking "where is X". Used both as a direct tiebreak and (#47) as
# part of the no-population proxy chain.
_SUBTYPE_WEIGHT = {
    "locality": 4,
    "localadmin": 3,
    "neighborhood": 2,
    "region": 1,
    "county": 1,
    "country": 0,
    "dependency": 0,
}

# Embedded 50-state map (#46): abbreviation -> full name. Covers the common
# "City, ST" case without any extra query. Region suffixes this doesn't
# recognize (non-US regions, spelled-out names not listed here) fall back
# to _resolve_region_from_table.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_US_STATES_BY_NAME = {name.lower(): abbr for abbr, name in US_STATES.items()}


def _strip_diacritics(s: str) -> str:
    """NFD-normalize and drop combining marks (#53) — "São Paulo" -> "Sao Paulo".

    Only used for matching; canonical names returned to callers are never
    passed through this.
    """
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def _normalize_for_match(s: str) -> str:
    return _strip_diacritics(s).lower()


# #214: Latin letters that carry no combining mark of their own, so neither
# Python's NFD pass (_strip_diacritics) nor DuckDB's strip_accents() touches
# them (duckdb#15706): "München" folds to "munchen", but "Preßburg" stays
# "preßburg" and "Łódź" only loses the ź. Overture's names.common is full of
# them — the German, Nordic, Polish and Icelandic exonyms are exactly the
# alternates an English-speaking caller reaches for — and a plain-ASCII query
# ("Pressburg", "Lodz", "Malmo") never reaches the row without this.
#
# Kept to letters with an unambiguous ASCII expansion, and applied *after*
# lower(), so only the lowercase forms need listing. Deliberately scoped to
# the #214 alternate-name fold rather than retrofitted onto
# _normalize_for_match: the primary-name tiers (#53) and the #215 fuzzy
# threshold were both measured against strip_accents alone, and widening
# their folding is a separate change with its own regressions to justify.
_UNFOLDED_LETTERS = {
    "ß": "ss",
    "ø": "o",
    "ł": "l",
    "đ": "d",
    "ð": "d",
    "þ": "th",
    "æ": "ae",
    "œ": "oe",
    "ħ": "h",
    "ı": "i",
}


def _fold_alt_name(s: str) -> str:
    """Python side of the #214 alternate-name fold: lowercase, accents
    stripped, then _UNFOLDED_LETTERS applied. Must stay byte-identical to
    _fold_alt_name_sql, which folds the stored column at materialization
    time — the two only ever meet as an equality/ILIKE comparison.
    """
    folded = _normalize_for_match(s)
    for src, dst in _UNFOLDED_LETTERS.items():
        folded = folded.replace(src, dst)
    return folded


def _fold_alt_name_sql(expr: str) -> str:
    """SQL twin of _fold_alt_name over `expr`. Used once per alternate at
    materialization time so the query-time comparison is a plain ILIKE on a
    stored column rather than a per-row function chain."""
    sql = f"lower(strip_accents({expr}))"
    for src, dst in _UNFOLDED_LETTERS.items():
        sql = f"replace({sql}, '{src}', '{dst}')"
    return sql


def _match_tier(name: str, query: str) -> int:
    """3 = exact, 2 = prefix, 1 = substring, 0 = no match (caller already filtered those out).

    Diacritic-insensitive (#53): "Sao Paulo" and "São Paulo" compare equal.
    """
    n, q = _normalize_for_match(name), _normalize_for_match(query)
    if n == q:
        return 3
    if n.startswith(q):
        return 2
    return 1


def _effective_tier(row: dict, query: str) -> int:
    """Match tier for `row`, using its stored `_tier` (#53) when present.

    A variant-sourced row was found via a *different* literal string than
    `query` (e.g. "Saint Louis" matched against the "St. Louis" the caller
    actually typed) — recomputing _match_tier(row["name"], query) at rank
    time would grade it against text it was never matched on, and (since
    "Saint Louis" isn't a prefix/substring of "St. Louis") wrongly demote a
    genuine exact match down to the weak fallback tier. `_tier`, set when
    the row was fetched, is the tier it actually achieved.
    """
    stored = row.get("_tier")
    return stored if stored is not None else _match_tier(row["name"], query)


# #221: match tiers split into two groups for ranking. Exact (3) and prefix
# (2) are both "the caller's string names this place"; substring (1) is only
# "the caller's string occurs somewhere in this name", which is a far weaker
# claim — "York" is a substring of "New York" and of eleven other things.
# _rank_key orders by this group *before* prominence, and by the tier itself
# only after; see its docstring.
_STRONG_TIER = 2


def _rank_key(row: dict, query: str, region_population: dict[str, int]):
    """Sort key: (#215) literal-over-fuzzy, then (#221) strong-vs-substring
    tier group, then whether a *nonzero* population is known, then the match
    tier, then (#47) whether a population is known at all and its value,
    else a documented proxy chain of subtype rank / hierarchy depth / the
    row's own region's population, then (#53) literal-over-variant, then id
    for full determinism. All ascending (smaller sorts first).

    Tier vs prominence (#221). Tier used to dominate outright, so any
    exact-tier row beat every prefix-tier row no matter what stood behind
    them: live, "東京" put a population-less Nagano neighborhood above 東京都
    and its 13.9M people, because 東京 is exactly the neighborhood's name and
    only a prefix of the prefecture's. The rule now is that *real
    prominence* outranks the tier, but only within the strong (exact/prefix)
    group and only against a row with no prominence at all — the exact-tier
    namesakes this rescues past are Overture rows with population NULL, and
    that emptiness is itself the signal (#47) that the match is a spelling
    coincidence rather than the place anyone means.

    "Real prominence" is a population greater than zero, not merely a
    non-NULL one. Overture ships plenty of divisions carrying an explicit
    population of 0 (abandoned and unincorporated places), and a bare
    null-check would let one of those rescue a prefix match past an exact
    one — reading a filled-in column as prominence when the value says the
    opposite. A 0 therefore ranks with the NULLs *here*; it keeps its #47
    meaning below the tier term, where "we know it is 0" still orders ahead
    of "we do not know", which is the ordering #47 established and #221 does
    not touch.

    Everything else is unchanged and deliberately so: two populated rows
    still order by tier first, so an exact match with 10k people still beats
    a prefix match with 10M ("Portland" is not a worse answer than "Portland
    Heights" because the latter is bigger); two population-less rows still
    order by tier; and a substring match still cannot leapfrog either,
    however populous, because the group term sits ahead of the population
    one. This is the same judgement #214 already made one level down — an
    alternate-name hit (`_variant`) wins on its own prominence against a
    population-less literal namesake, which is why "Munich" resolves to
    München — applied to the tier ladder instead of the literal/variant one.

    rank_score deliberately does *not* follow this (see _rank_score): it
    answers "how well does this name match what you typed", where an exact
    match really is a better match, so the top-ranked result can carry a
    lower rank_score than the one below it. #53 already produced that shape
    for variant hits; #221 only widens it.

    The #215 fuzzy term leads, ahead of even the tier: a fuzzy row matched
    a *different* string than the caller typed, so it belongs below every
    row that matched the typed string somehow — including a bare substring
    match. Among themselves fuzzy rows order by similarity first (the SQL
    already picked them by it); for every literal row both terms are
    constant, so this prefix is a no-op on the pre-#215 ordering.

    The literal-over-variant tiebreak sits *after* the #47 chain, not
    before it: real data has plenty of tiny, unrelated places sharing a
    literal-exact name with a query ("St. Louis" also matches a handful of
    small towns/villages, worldwide, literally spelled that way) — a
    variant match's own population/prominence has to be allowed to win over
    those the same way it would against any other literal match. Literal
    only wins when every other signal is tied, which is the case #53 is
    actually documented to care about (two otherwise-identical candidates,
    one found straight, one found through a spelling variant).
    """
    tier = _effective_tier(row, query)
    population = row.get("population")
    weight = _SUBTYPE_WEIGHT.get(row.get("subtype"), 0)
    depth = len(row.get("admin_context") or [])
    region_pop = region_population.get(row.get("region")) or 0
    return (
        1 if row.get("_fuzzy") else 0,
        -(row.get("_similarity") or 0.0),
        0 if tier >= _STRONG_TIER else 1,
        0 if (population or 0) > 0 else 1,
        -tier,
        0 if population is not None else 1,
        -(population or 0),
        -weight,
        depth,
        -region_pop,
        1 if row.get("_variant") else 0,
        row["id"],
    )


def _rank_score(row: dict, query: str) -> float:
    tier = _effective_tier(row, query)
    # #215: a fuzzy row didn't match the typed string at any tier, so it
    # scores below the weakest literal one (substring, 0.4) — the bounded
    # subtype/population bonuses below can add at most 0.09, keeping every
    # fuzzy score under 0.4 no matter how prominent the corrected place is.
    tier_score = 0.3 if row.get("_fuzzy") else {3: 1.0, 2: 0.7, 1: 0.4}[tier]
    weight = _SUBTYPE_WEIGHT.get(row.get("subtype"), 0)
    population = row.get("population")
    # A small, bounded bonus so rank_score stays roughly consistent with
    # the tiebreak order above without population dominating the score's
    # scale — a locality of 10M people isn't "10x more correct" than one
    # of 10k, it's a tiebreak, not a confidence signal.
    population_bonus = min(0.05, math.log10(population + 1) / 140) if population else 0.0
    score = tier_score + weight * 0.01 + population_bonus
    if row.get("_variant"):
        # Small, fixed penalty (#53) so a variant-sourced row's rank_score
        # never ties a same-tier literal match's — consistent with the
        # ordering _rank_key already enforces.
        score -= 0.01
    return round(score, 3)


def _admin_context(hierarchies, self_name: str | None = None) -> list[str]:
    """Containing-chain names from the first hierarchy path, self excluded.

    hierarchies comes back from DuckDB as plain Python lists/dicts:
    list-of-paths, each path a list of {division_id, name, subtype} dicts
    ordered top-level ancestor first, the division itself last (verified
    against live Overture divisions data). self_name strips that trailing
    self-entry so admin_context is only what *contains* the result, not the
    result itself. Any structural surprise (schema drift across releases)
    degrades to an empty chain rather than raising, matching overture.py's
    degrade-don't-crash approach.
    """
    try:
        if not hierarchies:
            return []
        path = hierarchies[0]
        names = [entry["name"] for entry in path if entry and entry.get("name")]
        if self_name and names and names[-1] == self_name:
            names = names[:-1]
        return names
    except (TypeError, KeyError, AttributeError):
        return []


def _admin_chain_context(chain: list[str] | None, self_name: str | None = None) -> list[str]:
    """Same as _admin_context, but for the #43 local table's pre-flattened
    admin_chain column (a plain list of names, no per-entry struct)."""
    if not chain:
        return []
    names = [n for n in chain if n]
    if self_name and names and names[-1] == self_name:
        names = names[:-1]
    return names


# --- #43: local divisions name table -----------------------------------


def _local_divisions_table_path(active_release: str) -> Path:
    return cache.cache_dir() / active_release / _DIVISIONS_TABLE_SUBDIR / _DIVISIONS_TABLE_FILENAME


def _materialize_alt_names_table(path: Path, glob: str) -> None:
    """COPY the #214 alternate-name table — one row per (division id, folded
    `names.common` spelling) — into a local parquet at `path`.

    Shape is deliberately narrow: `id`, the folded name the ILIKE runs
    against, and one display spelling for `matched_name`. Everything else the
    result row needs comes from joining `id` back to the primary divisions
    table, so this stays the small side of the join.

    Three reductions keep it that way, all done here rather than at query
    time. GROUP BY (id, folded) collapses the many languages that agree on a
    spelling — Overture stores "Munich" separately for en/fr/it/… — into one
    row. Alternates that fold to the same string as the primary name are
    dropped: the literal search already finds those, and a duplicate row
    would only have to be de-duplicated again per query. And the fold itself
    (lower + strip_accents + _UNFOLDED_LETTERS) is applied once per alternate
    here instead of once per alternate per query.

    Measured live on release 2026-07-22.0: 4.29M raw alternates fold down to
    2.49M rows across 1.39M divisions; 15.6s added to the one-time build,
    95.3MB ZSTD on disk against the primary table's 197.1MB, and 0.17-0.42s
    per join lookup ("Vienna" .. "Tokyo"). Dropping alt_display would make it
    71.6MB at the same build time — 24MB is what naming the matched spelling
    in the answer costs, and it is worth it: without it `matched_name` can
    only echo the folded lowercase form.

    Raises duckdb.Error if names.common isn't there or isn't a map — the
    caller treats that as "no alt table" and searches primary names only.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    con = overture._new_connection()
    sql = f"""
        COPY (
            SELECT id, alt_name, min(alt) AS alt_display
            FROM (
                SELECT id, alt, primary_folded, {_fold_alt_name_sql("alt")} AS alt_name
                FROM (
                    SELECT id,
                           {_fold_alt_name_sql("names.primary")} AS primary_folded,
                           unnest(map_values(names.common)) AS alt
                    FROM read_parquet('{glob}', hive_partitioning=1)
                    WHERE names.common IS NOT NULL
                )
            )
            WHERE alt_name IS NOT NULL AND alt_name <> ''
              AND alt_name IS DISTINCT FROM primary_folded
            GROUP BY id, alt_name
        ) TO '{tmp_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    con.execute(sql)
    tmp_path.replace(path)


def _materialize_divisions_table(path: Path, glob: str) -> None:
    """COPY just the columns geocode.py needs, for every type=division row, into
    a local parquet file at `path`. Raises UpstreamUnavailable/duckdb.Error on
    failure — callers treat that as "materialization failed" and fall back to
    direct upstream scans, same as a missing/incompatible schema.

    Also writes the #214 alternate-name table beside it, from the same
    upstream read. That half is best-effort: `names.common` is an optional
    nested field this module can't probe for (probe_schema only sees
    top-level columns), and losing alternate-name search is a much smaller
    loss than losing the local table entirely — so a failure there is logged
    and swallowed, leaving the install on primary names only.
    """
    cols = overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        raise overture.UpstreamUnavailable("divisions dataset missing 'names' column")
    population_expr = "population" if cols is None or "population" in cols else "NULL AS population"
    hierarchies_expr = (
        "list_transform(hierarchies[1], x -> x.name) AS admin_chain"
        if cols is None or "hierarchies" in cols
        else "NULL AS admin_chain"
    )
    region_expr = "region" if cols is None or "region" in cols else "NULL AS region"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    con = overture._new_connection()
    sql = f"""
        COPY (
            SELECT id, names.primary AS name, subtype, country, {region_expr},
                   bbox.ymin AS lat, bbox.xmin AS lon, {population_expr},
                   {hierarchies_expr}
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE names.primary IS NOT NULL
        ) TO '{tmp_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    con.execute(sql)
    tmp_path.replace(path)
    _try_materialize_alt_names_table(path.with_name(_ALT_NAMES_TABLE_FILENAME), glob)


# #214: releases whose alt table this process has already tried (and failed)
# to build. A cache directory written before this feature has a divisions
# table but no alt table; rather than leaving those installs on primary-only
# search until the next Overture release rolls the cache directory over, the
# alt half is built on its own the first time it is found missing. Bounded to
# one attempt per release per process so a persistent failure (no network, a
# release without names.common) costs one try, not one per geocode call.
_ALT_BUILD_ATTEMPTED: set[str] = set()


def _try_materialize_alt_names_table(alt_path: Path, glob: str) -> None:
    """Build the alt table, logging and swallowing any failure — see
    _materialize_divisions_table on why this half is best-effort.

    OSError is caught alongside the query errors because the filesystem half
    of the build (mkdir, and the tmp-file rename) fails on its own terms: a
    read-only or full cache directory raises OSError, not duckdb.Error, and
    every caller of this treats a failed alt build as "search primary names
    only". Letting it escape would take down a geocode call whose primary
    table had already been written successfully.
    """
    t0 = time.time()
    try:
        _materialize_alt_names_table(alt_path, glob)
    except (duckdb.Error, overture.UpstreamUnavailable, OSError) as e:
        logger.warning(
            "alternate-name table materialization failed, geocode will search "
            "primary names only: %s", e,
        )
        return
    logger.info(
        "alternate-name table materialized in %.1fs -> %s", time.time() - t0, alt_path
    )


def _local_alt_names_table(local_table: str | None) -> str | None:
    """Path to the #214 alternate-name table sitting beside `local_table`, or
    None if there isn't one.

    None is not an error state: no local divisions table at all (cache off),
    a cache directory written before this feature, a dataset without
    names.common, or a failed best-effort build all land here, and every one
    of them means the same thing to the caller — search primary names only.
    """
    if local_table is None:
        return None
    path = Path(local_table).with_name(_ALT_NAMES_TABLE_FILENAME)
    if path.exists():
        return str(path)
    key = str(path)
    if key not in _ALT_BUILD_ATTEMPTED:
        _ALT_BUILD_ATTEMPTED.add(key)
        logger.info("no alternate-name table at %s (cache predates #214); building it", path)
        _try_materialize_alt_names_table(
            path, overture.upstream_glob(theme="divisions", type_="division")
        )
        if path.exists():
            return str(path)
    return None


def _local_divisions_table() -> str | None:
    """Path to the materialized local divisions name table for the active
    release, building it (blocking — see the module docstring's #43
    section for why) on first use. Returns None if caching is off
    (PLACEROOT_CACHE=off) or materialization fails, so callers fall back to
    a direct upstream scan — the pre-#43 behavior.
    """
    if not cache.enabled():
        return None
    active_release = release.resolve_release()
    path = _local_divisions_table_path(active_release)
    if path.exists():
        return str(path)
    glob = overture.upstream_glob(theme="divisions", type_="division")
    logger.info(
        "materializing local divisions name table for release %s "
        "(first geocode call this process; one-time cost per release)",
        active_release,
    )
    t0 = time.time()
    try:
        _materialize_divisions_table(path, glob)
    except (duckdb.Error, overture.UpstreamUnavailable) as e:
        logger.warning(
            "local divisions table materialization failed, falling back to "
            "direct upstream scans: %s", e,
        )
        return None
    logger.info(
        "local divisions table materialized in %.1fs -> %s", time.time() - t0, path
    )
    return str(path)


def _region_population_lookup(local_table: str | None) -> dict[str, int]:
    """region ISO/local code -> population, from region-subtype rows of the
    local divisions table. Empty if no local table is available (cache off,
    or materialization failed) — the #47 "more populous region" tiebreak
    degrades to a no-op in that case rather than paying for an extra live
    full-table scan just to build this map.
    """
    if not local_table:
        return {}
    sql = f"""
        SELECT region, population FROM read_parquet('{local_table}')
        WHERE subtype = 'region' AND region IS NOT NULL AND population IS NOT NULL
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql).fetchall()
    except duckdb.Error:
        return {}
    return dict(rows)


# --- #46: "City, ST" / "City, Region" parsing ---------------------------


def _resolve_us_state(token: str) -> tuple[str, str] | None:
    """token (case-insensitive, abbreviation or full name) -> (full_name,
    "US-XX"), or None if it isn't a recognized US state/DC."""
    t = token.strip().rstrip(".")
    upper = t.upper()
    if upper in US_STATES:
        return US_STATES[upper], f"US-{upper}"
    abbr = _US_STATES_BY_NAME.get(t.lower())
    if abbr:
        return US_STATES[abbr], f"US-{abbr}"
    return None


def _resolve_region_from_table(candidate: str, local_table: str) -> tuple[str, str] | None:
    """candidate (e.g. "Ontario") -> (name, region_code) if it exactly
    matches (case-insensitive) a region-subtype row's name in the local
    divisions table, else None. Covers region suffixes outside the
    embedded US state map (#46) — non-US regions, or spellings not listed.
    """
    sql = f"""
        SELECT name, region FROM read_parquet('{local_table}')
        WHERE subtype = 'region' AND region IS NOT NULL AND name ILIKE $name ESCAPE '\\'
        LIMIT 1
    """
    try:
        with overture._conn_lock:
            row = overture.conn().execute(
                sql, {"name": overture._like_escape(candidate)}
            ).fetchone()
    except duckdb.Error:
        return None
    if row is None:
        return None
    return row[0], row[1]


def _split_region_suffix(query: str) -> list[tuple[str, str]]:
    """(base, candidate_suffix) pairs to try, comma-suffix preferred over a
    bare trailing token (a comma is a much stronger "this is a region
    qualifier" signal than the last word of a multi-word query)."""
    candidates = []
    if "," in query:
        base, _, suffix = query.rpartition(",")
        if base.strip() and suffix.strip():
            candidates.append((base.strip(), suffix.strip()))
    parts = query.strip().rsplit(None, 1)
    if len(parts) == 2 and parts[0].strip():
        candidates.append((parts[0].strip(), parts[1].strip()))
    return candidates


def _parse_region_suffix(query: str, local_table: str | None) -> tuple[str, str | None, str | None]:
    """query -> (base_query, region_code, region_name). region_code/name are
    both None if no trailing token looks like a region — the caller then
    searches `query` unmodified, today's behavior.
    """
    for base, suffix in _split_region_suffix(query):
        resolved = _resolve_us_state(suffix)
        if resolved is None and local_table:
            resolved = _resolve_region_from_table(suffix, local_table)
        if resolved:
            name, code = resolved
            return base, code, name
    return query, None, None


# --- #53: name-variant normalization -------------------------------------

# token (lowercased, trailing "." stripped) -> alternate spellings to try,
# in preference order. Bidirectional: the abbreviation maps to the
# expansion and vice versa, so a query in either convention finds a
# canonical name written in the other.
_ABBR_VARIANTS: dict[str, list[str]] = {
    "st": ["Saint"], "saint": ["St.", "St"],
    "ft": ["Fort"], "fort": ["Ft.", "Ft"],
    "mt": ["Mount"], "mount": ["Mt.", "Mt"],
}

# Same idea, but only applied to a query's leading token — a bare "N" or
# "S" elsewhere in a multi-word query is too ambiguous (initials, a street
# suffix, ...) to safely expand.
_CARDINAL_VARIANTS: dict[str, list[str]] = {
    "n": ["North"], "north": ["N.", "N"],
    "s": ["South"], "south": ["S.", "S"],
    "e": ["East"], "east": ["E.", "E"],
    "w": ["West"], "west": ["W.", "W"],
}


def _token_variants(token: str, leading: bool) -> list[str]:
    key = token.strip(".").lower()
    variants = list(_ABBR_VARIANTS.get(key, []))
    if leading:
        variants += _CARDINAL_VARIANTS.get(key, [])
    return variants


def _abbreviation_variant_queries(query: str) -> list[str]:
    """query -> whole-query variants with one token swapped for a common
    abbreviation/expansion (#53) — "St. Louis" -> ["Saint Louis"], "North
    Hollywood" -> ["N. Hollywood", "N Hollywood"]. Deduplicated, excludes
    the original query itself (case-insensitively)."""
    tokens = query.split(" ")
    variants = []
    for i, tok in enumerate(tokens):
        for alt in _token_variants(tok, leading=(i == 0)):
            new_tokens = list(tokens)
            new_tokens[i] = alt
            variants.append(" ".join(new_tokens))
    seen = {query.lower()}
    out = []
    for v in variants:
        vl = v.lower()
        if vl not in seen:
            seen.add(vl)
            out.append(v)
    return out


def _match_tier_order_sql(name_expr: str) -> str:
    """SQL ORDER BY expression pushing exact/prefix matches — the most
    populous ones first — ahead of plain substring matches, *before* LIMIT
    DIVISION_OVERFETCH applies.

    Without this, a broad name against millions of worldwide rows (measured:
    122 places literally named "Los Angeles" alone, most of them small
    Latin American localities) can fill the whole LIMIT with an arbitrary
    scan-order sample of same-tier matches — Python-side ranking
    (_rank_key) only ever sees whatever made it into that sample, so the
    well-known Los Angeles, CA can be dropped before ranking ever runs.
    Sorting by tier, then by population (known-and-higher first, so the
    dataset's own prominence signal shapes *which* same-tier rows survive
    the LIMIT, not just their order once they do) fixes that; final
    ranking is still done in Python by _rank_key, which also carries the
    #47 no-population proxy chain this SQL ORDER BY doesn't need to know
    about.
    """
    tier_expr = (
        f"CASE WHEN {name_expr} ILIKE $exact ESCAPE '\\' THEN 0 "
        f"WHEN {name_expr} ILIKE $prefix ESCAPE '\\' THEN 1 ELSE 2 END"
    )
    return f"{tier_expr}, population DESC NULLS LAST"


def _query_divisions_from_local(
    table_path: str, query: str, region_code: str | None, name_match_expr: str = "name"
) -> list[dict]:
    """name_match_expr (#53) is the SQL expression matched against $pattern
    /$exact/$prefix — "name" for a plain literal search, or
    "strip_accents(name)" for the diacritic-folded second-pass query (caller
    passes an already diacritic-stripped `query` to match against it)."""
    region_filter = "AND region = $region_code" if region_code else ""
    q = overture._like_escape(query)
    params: dict = {"pattern": f"%{q}%", "exact": q, "prefix": f"{q}%"}
    if region_code:
        params["region_code"] = region_code
    sql = f"""
        SELECT id, name, subtype, country, region, lat, lon, admin_chain, population
        FROM read_parquet('{table_path}')
        WHERE {name_match_expr} ILIKE $pattern ESCAPE '\\'
        {region_filter}
        ORDER BY {_match_tier_order_sql(name_match_expr)}
        LIMIT {DIVISION_OVERFETCH}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _admin_chain_context(r[7], self_name=r[1]),
            "population": r[8],
        })
    return result


def _query_divisions_from_upstream(
    query: str, region_code: str | None, name_match_expr: str = "names.primary"
) -> list[dict]:
    """Direct upstream scan — the pre-#43 path, used when no local table is
    available (PLACEROOT_CACHE=off, or materialization failed).

    name_match_expr: see _query_divisions_from_local (#53).
    """
    # type=division (points + hierarchies), not divisions.py's type=division_area
    # (polygons) — the two share a theme but are read from different fixtures/globs.
    glob = overture.upstream_glob(theme="divisions", type_="division")
    cols = overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return []
    population_expr = "population" if cols is None or "population" in cols else "NULL AS population"
    region_filter = ""
    q = overture._like_escape(query)
    params: dict = {"pattern": f"%{q}%", "exact": q, "prefix": f"{q}%"}
    if region_code and (cols is None or "region" in cols):
        region_filter = "AND region = $region_code"
        params["region_code"] = region_code
    sql = f"""
        SELECT id, names.primary AS name, subtype, country, region,
               bbox.ymin AS lat, bbox.xmin AS lon, hierarchies, {population_expr}
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE {name_match_expr} ILIKE $pattern ESCAPE '\\'
        {region_filter}
        ORDER BY {_match_tier_order_sql(name_match_expr)}
        LIMIT {DIVISION_OVERFETCH}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _admin_context(r[7], self_name=r[1]),
            "population": r[8],
        })
    return result


def _query_alt_names(
    alt_table: str, table_path: str, query: str, region_code: str | None
) -> list[dict]:
    """#214: divisions whose *alternate* (names.common) spelling matches
    `query`, joined back to the local divisions table for the real row.

    The stored alt_name column is already folded (see
    _materialize_alt_names_table), so the query is folded the same way in
    Python and the predicate stays a plain ILIKE on a stored column —
    0.19s measured over 4.29M alternates, against the 197MB primary table.

    Rows come back tagged `_variant` like #53's retry hits — found through a
    spelling Overture doesn't call canonical — with `_tier` recorded against
    the alternate they actually matched (see _effective_tier: "Munich" is an
    exact match for München's alternate, and re-deriving a tier from the
    canonical "München" at rank time would silently demote it to the
    substring tier). `_matched_name` carries one real spelling of the
    alternate for the caller-visible `matched_name`.

    One row per division, not per matching alternate. A division has as many
    alternate rows as it has distinct folded spellings — Москва carries
    "moscow", "moskau", "moskva", "moskwa" — and a substring query ("Mosk")
    matches several of them at once, which without the QUALIFY below returns
    the same GERS id several times over and lets one division fill the
    caller's whole `limit`. The de-duplication is done in SQL rather than in
    Python so DIVISION_OVERFETCH bounds *divisions* and not near-duplicate
    rows: the same ranking that orders the result picks which alternate
    survives per id (best tier, then alt_name for determinism), so the row
    kept is the one that matched `query` best.

    Returns [] when the query folds away to nothing (a query of only
    combining marks does: _fold_alt_name strips them). The pattern would
    otherwise be a bare '%%' matching every alternate in the table, which
    answers a nonsense query with arbitrary prominent divisions — the
    literal search, matching raw names, returns nothing for those.
    """
    folded = _fold_alt_name(query)
    if not folded:
        return []
    q = overture._like_escape(folded)
    params: dict = {"pattern": f"%{q}%", "exact": q, "prefix": f"{q}%"}
    region_filter = ""
    if region_code:
        region_filter = "AND d.region = $region_code"
        params["region_code"] = region_code
    match_order = _match_tier_order_sql("a.alt_name")
    sql = f"""
        SELECT d.id, d.name, d.subtype, d.country, d.region, d.lat, d.lon,
               d.admin_chain, d.population, a.alt_name, a.alt_display
        FROM read_parquet('{alt_table}') a
        JOIN read_parquet('{table_path}') d ON d.id = a.id
        WHERE a.alt_name ILIKE $pattern ESCAPE '\\'
        {region_filter}
        QUALIFY row_number() OVER (
            PARTITION BY d.id ORDER BY {match_order}, a.alt_name
        ) = 1
        ORDER BY {match_order}
        LIMIT {DIVISION_OVERFETCH}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _admin_chain_context(r[7], self_name=r[1]),
            "population": r[8],
            "_variant": True,
            "_tier": _match_tier(r[9], folded),
            "_matched_name": r[10] or r[9],
        })
    return result


def _alt_rows_not_already_found(
    alt_table: str, table_path: str, query: str, region_code: str | None, found: list[dict]
) -> list[dict]:
    """#214 alternate-name matches for `query`, minus the divisions the
    literal pass already returned — a division found under its canonical
    name is not also reported as an alternate hit.

    A vanished alt table degrades to primary-only rather than failing the
    query, the same #230 shape _query_divisions handles for the primary
    table one level up. It is milder here: no alt table at all is already
    a supported state (a cache directory predating #214 is in it), so
    there is nothing to fall back *to* and nothing lost but the alternate
    spellings.
    """
    try:
        rows = _query_alt_names(alt_table, table_path, query, region_code)
    except overture.UpstreamUnavailable:
        if Path(alt_table).exists():
            raise
        logger.warning(
            "alternate-name table vanished mid-query (%s); answering from "
            "primary names only", alt_table,
        )
        return []
    seen = {r["id"] for r in found}
    return [r for r in rows if r["id"] not in seen]


def _query_divisions(
    query: str,
    region_code: str | None,
    local_table: str | None,
    fold_diacritics: bool = False,
    alt_table: str | None = None,
) -> list[dict]:
    """fold_diacritics (#53): match strip_accents(name) against `query`
    (which the caller must already have run through _strip_diacritics) —
    the diacritic-folded half of the second-pass variant retry.

    alt_table (#214): when given, union in the alternate-name matches for
    the same query. Passed only for the primary literal search — the #53
    variant retries leave it None, because the alternate side already folds
    case and diacritics itself, so re-running it on a diacritic-stripped
    spelling of the same query can only return rows this pass already has.
    """
    if local_table is not None:
        name_expr = "strip_accents(name)" if fold_diacritics else "name"
        try:
            rows = _query_divisions_from_local(local_table, query, region_code, name_expr)
        except overture.UpstreamUnavailable:
            if Path(local_table).exists():
                raise
            # The table was deleted out from under us between the exists()
            # check in _local_divisions_table() and the read (external
            # cleanup, or a cache sweep — #230). Upstream is not known to be
            # unavailable, so don't report it as such: degrade to the direct
            # upstream scan, same as if the table had never materialized.
            logger.warning(
                "local divisions table vanished mid-query (%s); falling back "
                "to a direct upstream scan", local_table,
            )
        else:
            if alt_table is not None:
                rows = rows + _alt_rows_not_already_found(
                    alt_table, local_table, query, region_code, rows
                )
            return rows
    name_expr = "strip_accents(names.primary)" if fold_diacritics else "names.primary"
    return _query_divisions_from_upstream(query, region_code, name_expr)


# --- #215: fuzzy fallback tier ------------------------------------------

# Minimum jaro_winkler_similarity (0..1) between a folded division name and
# the folded query for a fuzzy row to be offered at all.
#
# Calibrated against the live 2026-07-22.0 release (R26): the three probes
# this tier exists for resolve top-1 correct well above it — "Berekley" ->
# Berkeley at 0.97, "Cinncinati" -> Cincinnati at 0.98, "Sna Francisco" ->
# San Francisco at 0.98 — so 0.92 keeps headroom for longer or two-typo
# spellings without reaching down into the ~0.85 band, where short,
# unrelated names ("Erie"/"Eire", "Lima"/"Lome") start pairing up. Raising
# it towards the measured 0.97 would buy nothing but lost corrections;
# lowering it trades a real answer for a plausible-looking wrong one, which
# for a geocoder is the worse failure.
_FUZZY_SIMILARITY_THRESHOLD = 0.92

# The folded name expression fuzzy matching compares against: same
# case-and-diacritic folding _normalize_for_match applies to the query in
# Python (#53's strip_accents, plus lower()), so "Sao Paulo" and "São
# Paulo" are the same string to the similarity function.
_FOLDED_NAME_SQL = "lower(strip_accents(name))"


def _has_like_metacharacter(query: str) -> bool:
    """True if `query` contains an ILIKE wildcard character.

    #165 made those literal, so "Brook_yn" matches nothing and stays
    visibly a non-match. Jaro-winkler doesn't know "_" from a letter and
    would quietly answer that query with "Brooklyn" — the same output a
    wildcard would have produced, which is precisely the behavior #165
    removed. Real typos don't contain "%" or "_", so declining to fuzzy
    these costs nothing and keeps that guarantee legible.
    """
    return "%" in query or "_" in query


def _query_divisions_fuzzy(
    table_path: str, query: str, region_code: str | None = None
) -> list[dict]:
    """Divisions whose folded name is within _FUZZY_SIMILARITY_THRESHOLD
    jaro-winkler of the folded query — the #215 typo tier.

    `region_code` narrows the pass to one region, the same filter the
    literal queries take. The caller passes the code parsed off the
    query's own "City, ST" suffix and retries without it if that comes
    back empty, mirroring what the literal search one step up already does
    — a region-constrained miss can mean the suffix was misread as a
    region, and answering nothing would be the worse failure.

    Local table only, by construction: the caller passes a materialized
    table path or doesn't call this at all. A similarity predicate has
    nothing an upstream parquet reader can prune by, so running it against
    S3 would read the whole divisions theme over the network; locally the
    same full scan is 0.26s (measured, 4.65M names).

    Rows come back tagged `_fuzzy` with their `_similarity`, and `_tier` 1
    so tier-reading code (_effective_tier, and through it _rank_score)
    grades them as the weak match they are rather than re-deriving a tier
    from a name that doesn't contain the query at all. Ordering is
    similarity first, then population — between two names equally close to
    a typo, the one people are more likely to have meant is the bigger
    place.

    Primary names only, deliberately, even though #214 put 4.29M alternate
    spellings within reach of the same scan. Two reasons, neither of them
    cost: this predicate can't use an index either way, so the extra table
    roughly doubles a 0.26s pass, which is affordable. But
    _FUZZY_SIMILARITY_THRESHOLD was calibrated against primary names — 4.65M
    mostly-distinct strings — and Overture's alternates are a much denser,
    much shorter population (every language's rendering of every division),
    which is exactly the shape that produces spurious >=0.92 pairs. Choosing
    a threshold for it needs its own measurement, and a wrong fuzzy answer
    is the failure mode this tier is most careful about. Left for a
    follow-up with numbers behind it.

    Returns [] when the query folds to the empty string — a query of only
    combining marks does, since folding strips exactly those. DuckDB's
    jaro_winkler_similarity(name, '') scores above the threshold rather
    than at 0, so without this the tier answers a nonsense query with the
    most populous divisions in the dataset, presented as spelling
    corrections.
    """
    folded_query = _normalize_for_match(query)
    if not folded_query:
        return []
    params: dict = {"folded": folded_query}
    region_filter = ""
    if region_code:
        region_filter = "AND region = $region_code"
        params["region_code"] = region_code
    sql = f"""
        SELECT id, name, subtype, country, region, lat, lon, admin_chain, population,
               jaro_winkler_similarity({_FOLDED_NAME_SQL}, $folded) AS similarity
        FROM read_parquet('{table_path}')
        WHERE jaro_winkler_similarity({_FOLDED_NAME_SQL}, $folded)
              >= {_FUZZY_SIMILARITY_THRESHOLD}
        {region_filter}
        ORDER BY similarity DESC, population DESC NULLS LAST
        LIMIT {DIVISION_OVERFETCH}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _admin_chain_context(r[7], self_name=r[1]),
            "population": r[8],
            "_fuzzy": True, "_similarity": r[9], "_tier": 1,
        })
    return result


def _fuzzy_correction_note(rows: list[dict], query: str) -> str:
    """Note naming the spelling(s) a fuzzy answer corrected `query` to.

    Same idiom as the other notes in this module — plain prose saying what
    the search did and what the caller can do about it — but this one rides
    along with *results*, not instead of them: the results are real, they
    just answer a different spelling than the one asked for, and an agent
    that can't see the correction has no way to catch a wrong guess.
    """
    names = []
    for row in rows:
        if row["name"] not in names:
            names.append(row["name"])
    spellings = ", ".join(f'"{n}"' for n in names)
    return (
        f'no division is named "{query}"; these matched by close spelling instead '
        f"({spellings}). If that is not the place you meant, re-run geocode with the "
        "exact spelling, or use find_places with lat/lon to search a known area."
    )


def _fallback_anchor(
    search_query: str,
    divisions: list[dict],
    region_code: str | None,
    local_table: str | None,
) -> tuple[float, float, str | None] | None:
    """(lat, lon, name_query) to bound/aim the places fallback (#83), or None
    if no location context can be derived from the query at all — the
    caller (geocode()) then runs the fallback unbounded, same as before #83
    (row-capped via DIVISION_OVERFETCH's LIMIT, but not bbox-pruned) rather
    than dropping a genuine name-only query (e.g. "Blue Bottle Roastery",
    with no city/region in it anywhere) to no results.

    Prefers the best division match already in hand (`divisions`, already
    ranked by _rank_key) — name_query stays the full search_query in that
    case. Failing that, tries the query's trailing one/two words as a
    division lookup of their own — e.g. "Westfield Valley Fair San Jose"
    never matches a division as a whole string, but its trailing "San Jose"
    does (the #83 bug's actual repro case: a big-box place name followed by
    the city it's in). Mirrors the trailing-token idea _split_region_suffix
    already uses for region suffixes, just aimed at finding *any* location
    anchor rather than a region code specifically.

    When the anchor comes from trailing tokens like this, name_query is the
    remaining prefix ("Westfield Valley Fair") rather than the full query —
    matching the *whole* query (which appends the city name onto the place
    name) against a places row's own name would otherwise never match
    anything, defeating the point of finding an anchor at all.

    #216: that trailing-token split is only worth acting on when what's
    left over is something a place could plausibly be *named*. name_query
    comes back None when it isn't — "the Met" splits into anchor "Met"
    (which substring-matches a real division) and residual "the", and
    ILIKE '%the%' matches a large fraction of every place on Earth: 50.2s
    measured live, answering "the Met" with "The Core IAS". The caller
    treats a None name_query as "skip the places half entirely and say so",
    which is strictly better than that. The rule is _nothing_but_stopwords':
    reject only when *every* word left is a _STOPWORD. That is deliberately
    looser than _significant_tokens' >=3-chars-and-not-a-stopword rule --
    rejecting here means returning nothing at all, and short is not the same
    as empty ("H&M Brooklyn", or a two-character Chinese name, has to reach
    the anchored scan). See _nothing_but_stopwords.

    Note this gate is about *emptiness*, not correctness: a misspelling
    like "Sna Francisco" (anchor "Francisco", residual "Sna") clears it
    just fine — "Sna" is not a stopword. Typos are handled a step earlier
    instead: #215's fuzzy tier retries the whole query by edit distance
    and, when it finds a correction, stands the places fallback down
    before this function is ever reached. Without a local divisions table
    for that tier to read (cache off), such a query still lands here and
    still reaches the substring scan of its own typo.
    """
    if divisions:
        top = divisions[0]
        return top["lat"], top["lon"], search_query
    tokens = search_query.strip().split()
    for n in (2, 1):
        if len(tokens) <= n:
            continue
        candidate = " ".join(tokens[-n:])
        rows = _query_divisions(candidate, region_code, local_table)
        if not rows:
            continue
        best = min(rows, key=lambda r: _rank_key(r, candidate, {}))
        base = " ".join(tokens[:-n]).strip()
        name_query = None if _nothing_but_stopwords(base) else base
        return best["lat"], best["lon"], name_query
    return None


def _query_places_fallback(query: str, anchor: tuple[float, float] | None = None) -> list[dict]:
    """Supplement divisions with named places when divisions alone don't fill limit.

    #83: an unconstrained ILIKE scan over the places theme — Overture's
    largest, least row-group-prunable theme — measured 100s+ live against a
    query with no bbox pushdown to exploit (a global name search touches
    every row, worldwide). `anchor` (lat, lon), when the caller
    (geocode(), via _fallback_anchor) can derive one, bounds the search to a
    vicinity via the same bbox+distance predicate every other point-radius
    query in this codebase already uses (overture.area_geometry) — same fix
    resolve_place already applies to its own places search. Without an
    anchor, this still runs (row-capped by the existing LIMIT, same as
    before #83) rather than dropping a genuine name-only query to nothing.
    """
    glob = overture.upstream_glob(theme="places", type_="place")
    cols = overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return []
    filters = ["names.primary ILIKE $pattern ESCAPE '\\'"]
    params: dict = {"pattern": f"%{overture._like_escape(query)}%"}
    if anchor is not None:
        lat, lon = anchor
        bbox_filter, distance_filter, geo_params, _bbox, _radius_m = overture.area_geometry(
            lat, lon, _PLACES_FALLBACK_RADIUS_M
        )
        filters += [bbox_filter, distance_filter]
        params.update(geo_params)
    sql = f"""
        SELECT id, names.primary AS name, bbox.ymin AS lat, bbox.xmin AS lon,
               coalesce(confidence, 0) AS confidence
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE {' AND '.join(filters)}
        ORDER BY confidence DESC
        LIMIT {DIVISION_OVERFETCH}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": "place",
            "country": None, "region": None,
            "lat": round(r[2], 6), "lon": round(r[3], 6),
            "admin_context": [], "_confidence": r[4],
        })
    return result


def geocode(query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Free-text place name -> ranked candidates. See geocode_detailed."""
    return geocode_detailed(query, limit)["results"]


def geocode_detailed(query: str, limit: int = DEFAULT_LIMIT) -> dict:
    """Free-text place name -> ranked candidates, from Overture divisions (and places fallback).

    Returns {"results": [...]} and, when the places-name half of the search
    is skipped as not worth its cost — no derivable location context to
    bound it by (#105), or nothing but stopwords left to search names for
    (#216) — a "note" saying so and how to make the query answerable.

    A "note" also comes back *with* results when nothing matched literally
    and the answer came from the #215 fuzzy tier instead: it names the
    spelling the results were corrected to ("Berekley" -> "Berkeley"), so
    a caller can tell a correction from a match. Those rows also carry
    "matched_by": "fuzzy" individually, which is what resolve_place reads
    to label them (it returns no note of its own).

    Never more than `limit` results. Each result: {name, type, lat, lon, id
    (GERS), admin_context, rank_score, plus "matched_by" on a #215 fuzzy
    row, plus (#214) "matched_name" on a row found through one of
    Overture's alternate names rather than the canonical one — "Munich"
    answers München, with matched_name "Munich"}. Raises
    overture.UpstreamUnavailable if the remote scan fails after retries;
    the caller (server.py) turns that into a structured error like the
    other tools.

    Handles "City, ST" / "City, Region" suffixes (#46) and ranks same-tier
    ties by population/prominence (#47) — see the module docstring.
    """
    query = query.strip()
    limit = max(1, min(limit, MAX_LIMIT))
    if not query:
        return {"results": []}

    note = None
    local_table = _local_divisions_table()
    # #214: None whenever there is no alternate-name table to search — cache
    # off, a cache directory predating the feature, a dataset without
    # names.common — in which case every _query_divisions call below is
    # exactly the primary-name-only search it was before.
    alt_table = _local_alt_names_table(local_table)
    base_query, region_code, _region_name = _parse_region_suffix(query, local_table)
    search_query = base_query if region_code else query
    # `region_code` is cleared below if its constrained search comes up
    # empty; the #215 fuzzy pass still wants the code the query itself
    # carried, so keep it.
    suffix_region_code = region_code

    divisions = _query_divisions(search_query, region_code, local_table, alt_table=alt_table)
    if region_code and not divisions:
        # Recognized a region suffix, but nothing in this dataset matches
        # inside it — degrade to an unconstrained search of the original
        # query rather than returning empty for a query that would
        # otherwise have matched something.
        region_code = None
        search_query = query
        divisions = _query_divisions(search_query, None, local_table, alt_table=alt_table)

    # #53: literal query didn't reach an exact-or-prefix division match with
    # some real prominence behind it — retry with normalized variants
    # (abbreviation swaps + diacritic folding) and merge in whatever they
    # find. Rows sourced this way are tagged `_variant`, which _rank_key
    # only ever uses as the last tiebreak (after the #47 population/proxy
    # chain) — see _rank_key's docstring for why: a literal exact match
    # against Overture's population-less tiny-village namesakes must not
    # get to shadow a genuinely prominent place found only through a
    # spelling variant ("St. Louis" also literally names several small,
    # unpopulated villages worldwide; the famous one is "Saint Louis" in
    # Overture's own naming and needs the variant retry to enter the
    # candidate pool at all — verified live, see the #53 module docstring).
    #
    # "Good enough" literal match: at least one exact/prefix candidate that
    # actually carries a population figure — Overture populates that field
    # for most well-known places (#47), so its presence is itself a signal
    # the literal search already found something real, not just a
    # same-spelling coincidence.
    #
    # Read through _effective_tier, not _match_tier, so a #214 alternate-name
    # hit counts for what it achieved: "Munich" is an exact match for
    # München's alternate, and re-deriving a tier from the canonical
    # "München" here would grade it 1 and send a query that has already found
    # its answer through the variant retries for nothing. Before #214 no row
    # in this pool carried a `_tier`, so this reads identically.
    best_literal_tier = max((_effective_tier(c, search_query) for c in divisions), default=0)
    literal_match_has_prominence = any(
        _effective_tier(c, search_query) == best_literal_tier
        and c.get("population") is not None
        for c in divisions
    )
    literal_answer_is_good_enough = (
        best_literal_tier >= _STRONG_TIER and literal_match_has_prominence
    )
    seen_ids = {c["id"] for c in divisions}
    variant_rows: list[dict] = []
    if not literal_answer_is_good_enough:
        for variant_query in _abbreviation_variant_queries(search_query):
            for row in _query_divisions(variant_query, region_code, local_table):
                if row["id"] not in seen_ids:
                    row["_variant"] = True
                    # Tier against the variant text it actually matched
                    # (#53) — see _effective_tier's docstring for why this
                    # can't be recomputed against the original query later.
                    row["_tier"] = _match_tier(row["name"], variant_query)
                    variant_rows.append(row)
                    seen_ids.add(row["id"])

    # #221: with a local table (#43) the diacritic-folded pass runs
    # unconditionally, outside the "literal match lacks prominence" gate
    # above. Under the gate it never ran for "Zurich": the literal ILIKE
    # finds a Dutch village spelled exactly that, and it carries a
    # population (190), so the gate read the literal search as having
    # already found something real — while Zürich, 443k, was not merely
    # ranked below but absent from the candidate pool entirely, since ILIKE
    # '%Zurich%' does not match "Zürich". A prominence gate cannot work
    # here: whether the folded spelling is worth looking for has nothing to
    # do with how prominent the *unfolded* one turned out to be. So the pass
    # runs and _rank_key decides, which is what it is for. Cheap enough to
    # do every time — one more predicate over the same local divisions table
    # the literal pass just read (0.2s measured, #214).
    #
    # Without a local table it stays gated, exactly as before #221. That
    # 0.2s is a local-parquet number; with no local table _query_divisions
    # falls through to _query_divisions_from_upstream, and an unanchored
    # ILIKE over the divisions theme has nothing to prune by, so running it
    # unconditionally would roughly double the upstream work for every query
    # that used to stop at a good literal answer. That is the cost class
    # #105/#216 exist to avoid and that _query_divisions_fuzzy declines to
    # pay upstream for the same reason. The consequence is deliberate and
    # narrow: cache-off callers keep the pre-#221 "Zurich" answer (the fold
    # still runs for them whenever the literal search came back weak, which
    # is what "Sao Paulo" -> "São Paulo" needs), and warming the cache is
    # what buys the fix.
    #
    # The abbreviation retries above stay gated on both paths: they fire one
    # extra query per expandable token ("St." -> "Saint", "N." -> "North"),
    # and unlike folding they rewrite the query into a genuinely different
    # string, so running them against an already-good literal answer buys
    # noise rather than reach.
    if local_table is not None or not literal_answer_is_good_enough:
        stripped_query = _strip_diacritics(search_query)
        # Not when stripping left nothing: a query of only combining marks
        # folds to "", and searching for it is an ILIKE '%%' that matches
        # every division in the dataset — a nonsense query answered with
        # whichever places are most populous. The literal pass, matching raw
        # names, correctly returns nothing for those.
        rows = (
            _query_divisions(stripped_query, region_code, local_table, fold_diacritics=True)
            if stripped_query
            else []
        )
        for row in rows:
            if row["id"] not in seen_ids:
                row["_variant"] = True
                row["_tier"] = _match_tier(row["name"], stripped_query)
                variant_rows.append(row)
                seen_ids.add(row["id"])
    divisions = divisions + variant_rows

    # #215: the literal search (and its #53 variant retries) found nothing
    # at all — the shape a typo makes. Retry by edit distance against the
    # local table only; see the module docstring for why emptiness is the
    # whole trigger and why this never runs upstream.
    #
    # Matched against `base_query`, not `search_query`: when a region
    # suffix was recognized and its constrained search came up empty,
    # search_query has been reset to the *whole* original string, suffix
    # included, and no division name is within edit distance of
    # "Berekley, CA" — the correction would be lost on the single most
    # common way a caller writes a place. base_query is the name half
    # either way (_parse_region_suffix returns the query unchanged when it
    # recognizes no suffix), and the region it was parsed off of is passed
    # as a filter, dropped on a miss the same way the literal search drops
    # it.
    fuzzy_rows: list[dict] = []
    fuzzy_query = base_query
    if not divisions and local_table is not None and not _has_like_metacharacter(fuzzy_query):
        fuzzy_rows = _query_divisions_fuzzy(local_table, fuzzy_query, suffix_region_code)
        if not fuzzy_rows and suffix_region_code:
            fuzzy_rows = _query_divisions_fuzzy(local_table, fuzzy_query)
        divisions = fuzzy_rows

    region_population = _region_population_lookup(local_table)
    divisions.sort(key=lambda r: _rank_key(r, search_query, region_population))

    # Skip the places fallback once an exact-name division match is already
    # in hand: places is Overture's largest, least-indexed theme, and an
    # unbounded ILIKE scan over it live can cost tens of seconds (measured)
    # — worth paying to fill out a weak result set (a prefix/substring-only
    # match, or none at all), not worth paying just to pad an already-exact
    # answer out to `limit`.
    has_exact_division = any(_effective_tier(c, search_query) == 3 for c in divisions)
    candidates = divisions
    # #215: a fuzzy hit means the query is a misspelling we have a
    # correction for, which also settles what the places half would be
    # searching for — the typo, as a substring, against Overture's largest
    # theme. That is the scan that answered "Sna Francisco" with "Snags N
    # Burgs Cafe"; with San Francisco already in hand it can only add noise,
    # so a fuzzy answer stands the fallback down the same way an exact
    # division match does.
    if len(candidates) < limit and not has_exact_division and not fuzzy_rows:
        # #83: bound the places scan to an anchor's vicinity whenever one
        # can be derived (a division match already in hand, or a trailing
        # location word in the query) instead of an unconstrained
        # worldwide scan — see _fallback_anchor/_query_places_fallback.
        anchor_hit = _fallback_anchor(search_query, divisions, region_code, local_table)
        anchor, name_query = (
            ((anchor_hit[0], anchor_hit[1]), anchor_hit[2]) if anchor_hit else (None, search_query)
        )
        if name_query is None:
            # #216: an anchor was derivable, but the query has nothing left
            # in it that a place could be named — see _fallback_anchor.
            # Searching places for a bag of stopwords costs a full-theme
            # substring scan (50.2s measured on "the Met") and returns
            # whatever unrelated name happens to contain "the". Say what
            # went wrong instead.
            note = _STOPWORD_RESIDUAL_NOTE
        elif anchor is None and _skip_unanchored_places_scan():
            # #105: with no anchor there is no bbox to prune by, so this is
            # a substring scan of every place on Earth. Measured live
            # against the 2026-07-22.0 release: 216s end-to-end for a query
            # that matched nothing, and dropping the ORDER BY doesn't help
            # (219s) -- with few or no matches the LIMIT can never
            # short-circuit, so the whole theme is read either way. That is
            # far past any MCP client's timeout: not a slow answer, a hang.
            #
            # Return what divisions found (often nothing) immediately and
            # tell the caller how to make the query answerable, instead.
            # Local datasets keep #83's behavior -- see
            # _skip_unanchored_places_scan.
            note = _UNANCHORED_NAME_SEARCH_NOTE
        else:
            seen_names = {(c["name"].lower()) for c in candidates}
            places = _query_places_fallback(name_query, anchor=anchor)
            places = [p for p in places if p["name"].lower() not in seen_names]
            places.sort(
                key=lambda r: (-_match_tier(r["name"], name_query), -r["_confidence"], r["id"])
            )
            candidates = candidates + places

    out = []
    for row in candidates[:limit]:
        entry = {
            "name": row["name"],
            "type": row["subtype"],
            "lat": row["lat"],
            "lon": row["lon"],
            "id": row["id"],
            "admin_context": row["admin_context"],
            "rank_score": _rank_score(row, search_query) if "_confidence" not in row else round(
                0.4 + row["_confidence"] * 0.3, 3
            ),
        }
        if row.get("_matched_name"):
            # #214: only present when the row was found through one of
            # Overture's alternate (names.common) spellings rather than its
            # canonical one. `name` stays canonical either way, so this is
            # what tells a caller "Munich" found "München" on purpose.
            entry["matched_name"] = row["_matched_name"]
        if row.get("_fuzzy"):
            # #215: the note says "these answer a corrected spelling" in
            # prose, which is what a human reader needs; this says it per
            # row, in a field, which is what code needs. resolve_place
            # returns no note at all and has to label each candidate's
            # match on its own — without this it can only re-derive a tier
            # from a name that doesn't contain the query, and call a
            # correction a "substring" match.
            entry["matched_by"] = "fuzzy"
        out.append(entry)
    result = {"results": out}
    fuzzy_out = [row for row in candidates[:limit] if row.get("_fuzzy")]
    if fuzzy_out:
        # #215: unlike the skip notes below, this one accompanies real
        # results — it says which spelling they actually answer. Named
        # against the string actually fuzzed, which is the query minus any
        # region suffix.
        result["note"] = _fuzzy_correction_note(fuzzy_out, fuzzy_query)
    if note and not out:
        # Only worth saying when the answer is empty: if divisions already
        # produced candidates, the skipped places half isn't what the caller
        # is missing.
        result["note"] = note
    return result


# --- #22: GERS id resolution -----------------------------------------------

# resolve_place overfetches both sources before merging/ranking/trimming to
# `limit`, the same reasoning as DIVISION_OVERFETCH: a shallow per-source
# limit can drop the right candidate before the merged ranking ever sees it.
_RESOLVE_OVERFETCH = 10

# #105: a places-name search with no anchor has no bbox to prune by, making
# it a substring scan of Overture's largest theme. Against the live remote
# dataset that measured 216s end-to-end (and 219s with the ORDER BY removed
# -- a LIMIT can't short-circuit a scan matching few or no rows), which is
# past any MCP client's timeout.
#
# The cost is the *remote* full-theme read, not the query shape: against a
# local dataset (a test fixture, or a mirror via PLACEROOT_DATA_PATH /
# PLACEROOT_UPSTREAM_BASE) the same scan is cheap and genuinely useful, so
# #83's name-only search is kept exactly as-is there. Set this env var to
# force the unbounded scan even against a remote dataset.
_UNBOUNDED_NAME_SEARCH_ENV = "PLACEROOT_UNBOUNDED_NAME_SEARCH"

# Schemes DuckDB reads over the network; anything else is a local path.
_REMOTE_GLOB_SCHEMES = ("s3://", "http://", "https://", "gcs://", "gs://", "az://", "azure://")

_STOPWORD_RESIDUAL_NOTE = (
    "no division matched this query as a whole, and once its trailing location "
    "word is set aside as an anchor nothing distinctive is left to search place "
    "names for (only common words like \"the\" or \"of\"), so the places half of "
    "the search was skipped -- matching those against every place name is minutes "
    "of scanning for results that would be unrelated anyway. Spell the name out "
    "(\"the Metropolitan Museum of Art\" rather than \"the Met\"), or use "
    "find_places with lat/lon to search a known area."
)

_UNANCHORED_NAME_SEARCH_NOTE = (
    "no division matched, and this query carries no location context to bound a "
    "place-name search by, so the places half of the search was skipped (it would "
    "scan the entire global places dataset -- minutes, not seconds). Add a location "
    "to the query (\"Blue Bottle Roastery, Oakland\"), or use find_places with "
    "lat/lon (or resolve_place with near_lat/near_lon) to search a known area."
)


def _unbounded_name_search_enabled() -> bool:
    """True iff the operator opted back into the unbounded places-name scan."""
    value = os.environ.get(_UNBOUNDED_NAME_SEARCH_ENV, "").strip().lower()
    return value not in ("", "0", "false", "off")


def _is_remote(glob: str) -> bool:
    """Whether reading `glob` means going over the network."""
    return glob.lower().startswith(_REMOTE_GLOB_SCHEMES)


def _skip_unanchored_places_scan() -> bool:
    """Whether to skip the anchorless places-name scan for this dataset.

    Only skipped when the scan would be a remote read of the whole places
    theme (#105) and the operator hasn't opted back in.
    """
    if _unbounded_name_search_enabled():
        return False
    return _is_remote(overture.upstream_glob(theme="places", type_="place"))

# Bbox radius (#22) for the name-filtered find_places call when no
# near_lat/near_lon hint is given but a division match is in hand — "same
# metro area" as the top division match, not a general-purpose area search.
_RESOLVE_PLACE_RADIUS_M = 20_000

_MATCH_TIER_LABELS = {3: "exact", 2: "prefix", 1: "substring"}

# resolve_place's `match` labels, best first. "fuzzy" (#215) is not a tier
# the literal search can produce — it means the name doesn't contain the
# query at all and was reached by edit distance instead — so it ranks
# below every literal label, matching how _rank_key already orders the
# rows themselves.
_MATCH_LABEL_RANK = {"exact": 3, "prefix": 2, "substring": 1, "fuzzy": 0}

# Small enough to filter, generic enough that requiring them in a name
# match would be actively wrong ("the Whole Foods on Lamar" — "the"/"on"
# aren't part of any real place name). Dropped before a query is split into
# per-token find_places searches and before word-overlap scoring.
_STOPWORDS = {"the", "a", "an", "on", "in", "at", "near", "of", "and", "by"}


def _match_label(row: dict, query: str) -> str:
    """A geocode() result row -> how it matched `query`.

    A #215 fuzzy row is labeled from its own provenance, not re-derived
    from its name: `_match_tier` reports "substring" for any name it is
    handed, including one that doesn't contain the query at all, so
    deriving a label from "Berkeley" against "Berekley" would claim a
    containment that isn't there — the one thing a caller reads this field
    to rule out.
    """
    if row.get("matched_by") == "fuzzy":
        return "fuzzy"
    return _MATCH_TIER_LABELS[_match_tier(row["name"], query)]


# resolve_place runs one find_places per significant token, each taking the
# shared DuckDB connection lock — so an unbounded token count from a huge
# query string is a lock-contention DoS against the whole server, not just a
# slow response. A real place reference ("the Whole Foods on South Lamar,
# Austin") is a handful of words; cap the fan-out well above that.
_MAX_RESOLVE_TOKENS = 12


def _nothing_but_stopwords(text: str) -> bool:
    """Whether `text` holds no word a place could be named after — every word
    in it is a _STOPWORD, or there are no words at all.

    _fallback_anchor's residual gate (#216). Deliberately *not*
    _significant_tokens' rule: that one also drops words under 3 characters,
    which is safe there only because it layers a never-search-nothing
    fallback on top. Here the answer is load-bearing — "reject" means
    returning no results at all — and plenty of real names are nothing but
    short words ("H&M", and most two-character Chinese/Japanese/Korean place
    names). Those are distinctive enough to search on; "the" is not.
    """
    return not any(w.lower() not in _STOPWORDS for w in re.findall(r"[\w'-]+", text))


def _significant_tokens(query: str) -> list[str]:
    """query -> its meaningful words: >=3 chars, not a stopword, in order of
    appearance, capped at _MAX_RESOLVE_TOKENS. Falls back to the whole query
    if nothing survives (e.g. a query that's all short/stopword tokens)
    rather than searching nothing.
    """
    tokens = [t for t in re.findall(r"[\w'-]+", query) if len(t) >= 3]
    significant = [t for t in tokens if t.lower() not in _STOPWORDS]
    return (significant or tokens or [query])[:_MAX_RESOLVE_TOKENS]


def _place_match_label(name: str, query: str) -> str | None:
    """Match label for a place candidate found via per-token search (#22),
    or None if it isn't actually related to `query` — the per-token
    find_places calls below are deliberately loose (OR of significant
    words) for recall, so this is what keeps an unrelated nearby place from
    polluting results just because it happens to share one common word.

    Unlike _match_tier (divisions, where the query and the canonical name
    are usually close to the same shape), a free-text place query commonly
    carries extra context the place's own name doesn't ("Mañana coffee
    Austin" vs. a place literally named "Mañana Coffee") — so containment
    is checked in both directions, and failing that, a shared significant
    word still counts as a (weaker) match.
    """
    n, q = _normalize_for_match(name), _normalize_for_match(query)
    if n == q:
        return "exact"
    if n.startswith(q) or q.startswith(n):
        return "prefix"
    if n in q or q in n:
        return "substring"
    n_tokens = set(_significant_tokens(n))
    q_tokens = set(_significant_tokens(q))
    if n_tokens & q_tokens:
        return "substring"
    return None


def resolve_place(
    query: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
    limit: int = 3,
) -> list[dict]:
    """Free-text place reference -> ranked, typed GERS ids an agent can hold onto.

    The point of this tool: "the Whole Foods on Lamar" or "Travis County"
    are free text, not stable references — resolve_place turns either shape
    into a GERS id a caller can pass to place_details/other tools later.
    Merges two sources: geocode()'s division matches (a name/region/county/
    country), and find_places searches over the places theme (a business or
    POI), one per significant word in the query (so a query that names a
    place plus extra context — "Mañana coffee Austin" — still finds a place
    literally named just "Mañana Coffee") — bbox-limited to near_lat/near_lon
    if given, else to a 20km vicinity around the top division match (so a
    location hint isn't required when the query itself names a place, e.g.
    "Travis County, TX"). Place candidates unrelated to the query beyond
    incidentally sharing one word with something nearby are dropped, not
    just down-ranked — see _place_match_label.

    Each candidate: {"id" (GERS), "kind": "division" | "place", "name",
    "lat", "lon", "match": "exact" | "prefix" | "substring" | "fuzzy", plus
    "admin_context" (division) or "category" (place)}. "fuzzy" (#215,
    divisions only) means the name doesn't contain the query at all and was
    reached by close spelling instead — the caller asked for one string and
    is being handed the answer to another, so it ranks below every literal
    label. Ranked by match tier first — kind-agnostic, an exact place beats
    a prefix-matched division — then by prominence (division rank_score /
    place confidence, both roughly 0-1 scales), then id for determinism.
    Never more than `limit` results.

    No match is a valid answer, not an error: an unresolvable query returns
    an empty list. Raises overture.UpstreamUnavailable if a remote scan
    fails after retries, or overture.SchemaDegraded if the places dataset
    is missing bbox — the caller (server.py) turns either into a structured
    error like every other tool.
    """
    query = query.strip()
    limit = max(1, min(limit, MAX_LIMIT))
    if not query:
        return []

    geocode_hits = geocode(query, limit=_RESOLVE_OVERFETCH)
    division_hits = [r for r in geocode_hits if r["type"] != "place"]

    if near_lat is not None and near_lon is not None:
        reference = (near_lat, near_lon)
    elif division_hits:
        reference = (division_hits[0]["lat"], division_hits[0]["lon"])
    else:
        reference = None

    place_rows: list[dict] = []
    if reference is not None:
        ref_lat, ref_lon = reference
        seen_place_ids: set[str] = set()
        for token in _significant_tokens(query):
            for row in overture.find_places(
                ref_lat, ref_lon, radius_m=_RESOLVE_PLACE_RADIUS_M,
                name=token, limit=_RESOLVE_OVERFETCH,
            ):
                if row["id"] and row["id"] not in seen_place_ids:
                    seen_place_ids.add(row["id"])
                    place_rows.append(row)

    candidates = []
    seen_ids: set[str] = set()
    for r in division_hits:
        if not r["id"] or r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])
        candidates.append({
            "id": r["id"], "kind": "division", "name": r["name"],
            "lat": r["lat"], "lon": r["lon"],
            "admin_context": r["admin_context"],
            "match": _match_label(r, query),
            "_prominence": r["rank_score"],
        })
    for r in place_rows:
        if not r["id"] or r["id"] in seen_ids or not r["name"]:
            continue
        label = _place_match_label(r["name"], query)
        if label is None:
            continue
        seen_ids.add(r["id"])
        candidates.append({
            "id": r["id"], "kind": "place", "name": r["name"],
            "lat": r["lat"], "lon": r["lon"],
            "category": r["category"],
            "match": label,
            "_prominence": r.get("confidence") or 0.0,
        })

    candidates.sort(key=lambda c: (
        -_MATCH_LABEL_RANK[c["match"]],
        -c["_prominence"],
        c["id"],
    ))
    for c in candidates:
        del c["_prominence"]
    return candidates[:limit]


def _nearest_address(lat: float, lon: float) -> dict | None:
    """Nearest address point within an expanding search radius, or None if none found nearby
    or the addresses theme is unreachable/missing (degrade, don't raise).

    Reads through addresses._from_source, so this hop shares the tile cache
    (and the tiles themselves) with address_at rather than re-scanning S3 on
    every call — issue #189. Cache resolution can itself raise
    UpstreamUnavailable, which is caught alongside the query's own DB errors:
    this function's contract is to degrade to a divisions-only answer on any
    addresses-side failure, and "the cache could not reach upstream to
    materialize a tile" is one of those.
    """
    glob = addresses._upstream_glob()
    cols = overture.probe_schema(glob)
    if cols is not None and "street" not in cols:
        return None
    for radius_m in (200, 1000, 5000):
        bbox_filter, distance_filter, params, bbox, _radius_m = overture.area_geometry(
            lat, lon, radius_m
        )
        try:
            sql = f"""
                SELECT street, number, postcode, bbox.ymin AS lat, bbox.xmin AS lon,
                       round({overture.DISTANCE_EXPR}, 1) AS distance_m
                FROM {addresses._from_source(bbox)}
                WHERE {bbox_filter} AND {distance_filter}
                ORDER BY distance_m
                LIMIT 1
            """
            with overture._conn_lock:
                row = overture.conn().execute(sql, params).fetchone()
        except (duckdb.Error, overture.UpstreamUnavailable) as e:
            logger.warning("addresses theme query failed, degrading to divisions-only: %s", e)
            return None
        if row:
            return {
                "street": row[0], "number": row[1], "postcode": row[2],
                "lat": round(row[3], 6), "lon": round(row[4], 6), "distance_m": row[5],
            }
    return None


def _nearest_division(lat: float, lon: float) -> dict | None:
    glob = overture.upstream_glob(theme="divisions", type_="division")
    cols = overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return None
    for radius_m in (2000, 20000, 100000):
        bbox_filter, distance_filter, params, _bbox, _radius_m = overture.area_geometry(
            lat, lon, radius_m
        )
        sql = f"""
            SELECT names.primary AS name, subtype, hierarchies,
                   round({overture.DISTANCE_EXPR}, 1) AS distance_m
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND {distance_filter}
              AND subtype IN ('locality', 'localadmin', 'neighborhood')
            ORDER BY distance_m
            LIMIT 1
        """
        try:
            with overture._conn_lock:
                row = overture.conn().execute(sql, params).fetchone()
        except duckdb.Error as e:
            logger.warning("divisions theme query failed: %s", e)
            return None
        if row:
            chain = _admin_context(row[2], self_name=row[0])
            return {"name": row[0], "subtype": row[1], "admin_context": [*chain, row[0]]}
    return None


def reverse_geocode(lat: float, lon: float) -> dict:
    """Nearest address (street/number/postcode) plus its containing division chain.

    Degrades to a divisions-only result — noting it via "source" and
    "note" — if the addresses theme is unreachable or missing, rather than
    failing the call: addresses is Overture's newest, least complete theme,
    so this is the expected degraded path, not a rare edge case.
    """
    address = _nearest_address(lat, lon)
    division = _nearest_division(lat, lon)
    admin_context = division["admin_context"] if division else []

    if address is not None:
        return {
            "address": {
                "street": address["street"],
                "number": address["number"],
                "postcode": address["postcode"],
            },
            "lat": address["lat"],
            "lon": address["lon"],
            "distance_m": address["distance_m"],
            "admin_context": admin_context,
            "source": "address",
        }
    return {
        "address": None,
        "lat": lat,
        "lon": lon,
        "distance_m": None,
        "admin_context": admin_context,
        "source": "divisions_only",
        "note": "no nearby address found (addresses theme unavailable, missing, or sparse here)",
    }


# --- #123: free-text area name -> a division to constrain a search to -------

# Two candidates count as "equally ranked" when their rank_scores differ by
# less than this. rank_score is a computed float, so exact equality is the
# wrong test — but the tolerance stays tiny on purpose: geocode already
# breaks same-name ties by population and match tier (#47/#53), so a
# genuinely more prominent namesake outranks the rest by a wide margin and
# resolves cleanly. What survives at this tolerance is the real ambiguity
# the issue is about: same-tier, same-name divisions the dataset gives us
# no signal to choose between (e.g. two population-less "Springfield"s).
_AREA_RANK_EPSILON = 1e-6

# Cap on candidates reported back for an ambiguous area — enough to choose
# from, not a data dump.
_AREA_MAX_CANDIDATES = 5


def resolve_area(area: str) -> dict | None:
    """Free-text area name -> the single division to constrain a search to.

    Thin resolution layer over geocode()'s division ranking — deliberately
    NOT a second ranking implementation. geocode() already handles the
    "City, ST" suffix, diacritic/abbreviation variants, and the
    population-weighted tie-breaks; this just takes its division results
    (type != "place"; a business named "Palo Alto Cafe" is not an area) and
    decides whether the top one is a safe pick.

    Returns {"division_id", "name", "admin_context"} for a confident match,
    or None if nothing matched at all. Raises AmbiguousArea when several
    equally-ranked divisions share the top name, so the caller can surface
    the candidates instead of silently searching one arbitrary "Springfield"
    and reporting its places as though the question had one answer.

    Raises overture.UpstreamUnavailable if the underlying scan fails.
    """
    area = area.strip()
    if not area:
        return None

    # Rows without an id can't be handed to the polygon search at all, so
    # they're dropped here rather than surfacing as a confusing downstream
    # error (id is only ever absent from a degraded dataset).
    divisions = [
        r for r in geocode(area, limit=_RESOLVE_OVERFETCH)
        if r["type"] != "place" and r["id"]
    ]
    if not divisions:
        return None

    top = divisions[0]
    # Ambiguity is specifically "same name, no way to rank them" — a
    # differently-named division that merely scored close (a neighborhood
    # inside the city you asked for) is not ambiguity, so compare names too.
    top_name = _normalize_for_match(top["name"])
    tied = [
        d for d in divisions
        if _normalize_for_match(d["name"]) == top_name
        and abs(d["rank_score"] - top["rank_score"]) < _AREA_RANK_EPSILON
    ]
    if len(tied) > 1:
        raise AmbiguousArea(area, [_area_candidate(d) for d in tied[:_AREA_MAX_CANDIDATES]])

    return _area_candidate(top)


def _area_candidate(row: dict) -> dict:
    """A division row, projected to just what an area choice needs."""
    return {
        "division_id": row["id"],
        "name": row["name"],
        "admin_context": row["admin_context"],
    }
