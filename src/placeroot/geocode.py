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

Two properties this tier is built around:

- It never scans upstream. jaro_winkler over the whole local table is
  0.26s (measured, 4.65M names); the same predicate against S3 would be a
  full-theme read with nothing to prune by, which is exactly the cost
  #105/#216 exist to avoid. No local table (cache off, or materialization
  failed) means no fuzzy tier — an unavailable nicety, not a fallback to
  something expensive.
- A fuzzy hit answers a *different* string than the one typed, so it sorts
  below every literal tier (_rank_key's leading term, ahead of even tier
  3), scores below the substring tier, and carries a note naming the
  spelling it corrected to, so an agent can see the correction happened
  rather than silently trusting it. A fuzzy hit also stands down the
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
only a prefix of 東京都's (13.6M). _rank_key now lets a known population
outrank the tier *within* the exact/prefix group and only against a row
carrying no population at all — the same prominence-over-namesake judgement
#214 already made between literal and alternate-name hits, applied to the
tier ladder. Two populated rows still order by tier first, and a substring
hit still cannot leapfrog either. See _rank_key.

And the #53 second pass was gated on "no exact/prefix literal match carries
a population", which the diacritic half of it cannot live with: "Zurich"
literally matches a Dutch village of 190 people, so the gate declared the
literal search good enough and the folded pass never ran — Zürich (443k)
was absent from the candidate pool entirely, since ILIKE '%Zurich%' never
matches "Zürich". The folded pass now always runs; the abbreviation half
stays gated. Live after: "Zurich" -> Zürich, CH; "東京" -> 東京都.

tests/test_geocode_ranking.py pins the answers all of the above already got
right, as a corpus, precisely because a _rank_key change can move them.

--- #223: postcodes ---

"94110", "1011AB" and "SW1A 1AA" used to come back empty: division names are
names, and no postal_code division subtype exists in release 2026-07-22.0
(verified against all 9 subtypes). The postcode data that does exist lives on
the addresses theme -- 474M points carrying a `postcode` column -- so a query
that is *entirely* a postcode is answered from there instead, by one
aggregate: WHERE postcode IN (spellings) GROUP BY country, giving a point
count and a centroid per country, joined to the covering locality from the
already-materialized #43 divisions table (60ms). R27-measured live: 11.4-13.4s
cold for the aggregate, and 94110 comes back genuinely ambiguous -- 29,956 US
points in the Mission in San Francisco, 3,491 SK, 3,310 FR -- which is the
honest answer, so all three are returned, ordered and scored by the evidence
behind them.

The aggregate is the one addresses read in this codebase that does NOT go
through cache.py's tile machinery: tiles are bboxes, and "which countries
carry this code" has no bbox to be sliced by -- a tile-shaped answer here
would be wrong, not merely partial. See _query_postcode_countries.

The detector (_postcode_variants) is deliberately conservative: whole-query
match against a fixed list of country postcode shapes, so a query that could
be a name stays a name query. A postcode-shaped query that matches nothing
still falls through to the ordinary name search, and if that is also empty
the answer carries the coverage note -- because an empty postcode answer
usually means "outside the addresses theme" (GB) or "covered but carrying no
postcode values" (the 9 countries in _POSTCODE_ZERO_COUNTRIES), not "no such
code".

--- #224: bbox columns on the local divisions table (and why they are empty) ---

R27 wanted a city extent to bound a street-level address scan with (#225): a
hand-guessed Mountain View bbox 0.002 degrees too small returned empty for
"1600 Amphitheatre Parkway". The divisions rows carry a `bbox` struct
natively, so the four corners (xmin/ymin/xmax/ymax) now ride along in
_materialize_divisions_table's COPY and _division_bbox reads them back by id.
The columns are INTERNAL: no tool response mentions them, and geocode /
resolve_place answers are unchanged.

**The extent is not there.** Measured live against release 2026-07-22.0, every
type=division row's bbox is degenerate -- the rows are points (the same
bbox.ymin/bbox.xmin this module has always read as lat/lon), and their bbox is
just that point's float32 rounding envelope: 7.6e-6 to 1.5e-5 degrees wide,
3.8e-6 to 7.6e-6 tall, i.e. under two metres, for Mountain View CA exactly as
for San Francisco. So _division_bbox applies _DEGENERATE_BBOX_SPAN_DEG and
returns None for them rather than handing a caller a one-metre "city" it would
silently scan nothing inside of. A caller that gets None must fall back;
today, in practice, that is every locality.

The real extents live one type over, on divisions/type=division_area, which
carries a genuine polygon bbox plus a `division_id` column that joins straight
back to this table's `id` (verified live: division 15f1bd57-... "Mountain
View" -> area bbox -122.1176,37.3542 .. -122.0449,37.4711, about 6.4 x 13 km).
Materializing that theme wholesale is a second full-table COPY this module has
no other use for, so #224 deliberately stops here: the plumbing and the
honest None. #225, which resolves one anchor locality per query, should fetch
the area bbox for that single id at query time instead -- one bounded lookup,
not another release-sized table.

If a future Overture release starts populating real extents on the division
rows themselves, nothing here needs changing: _division_bbox already returns
whatever it finds once the span clears the degeneracy floor.

--- #225: geocode_address, street-level forward search ---

geocode answers at city/neighborhood granularity, address_at answers "what is
at this coordinate"; nothing answered "where is 1600 Amphitheatre Parkway".
The data does: R27 measured `number='1600' AND street ILIKE 'AMPHITHEATRE%'`
inside a Mountain View bbox returning Google HQ exactly, 4.1s cold and 10ms
from the addresses tile cache. So geocode_address is a *forward* search over
the addresses theme, bounded by a city extent.

It lives here rather than in addresses.py for one reason: it needs geocode()
to resolve its anchor, and addresses.py is imported *by* this module. The row
shape is addresses.py's (number/street/unit/postcode) and the coverage
contract is addresses.COVERED_COUNTRIES, both reused rather than restated.

Four steps, each able to end the call honestly:

1. Parse (_parse_address_query). The first comma splits the street half from
   the place half; a bare integer at either end of the street half is the
   house number, which covers US "1600 Amphitheatre Pkwy" and German
   "Hauptstraße 5" with one rule. Unit numbers are out of scope on purpose --
   they live in a separate `unit` column, and deciding which trailing integer
   is a unit rather than a house number would silently search a different
   doorway. A caller who already has the parts passes number/street/city.
2. Anchor (_anchor_bbox). #224's _division_bbox first, then the
   division_id-filtered division_area lookup that actually answers today
   (10.7s cold, measured, then memoized per process). No extent means no
   scan: an address search over a guessed box returns confidently wrong
   doorways, so the answer is an empty list plus a note naming the step that
   ended it.
3. Scan (_scan_addresses_in_bbox), through addresses._from_source and so
   through the same #202 tile cache address_at and reverse_geocode read.
   Street matching runs every USPS abbreviation/expansion of the query
   (_STREET_SUFFIX_VARIANTS, fed through the same _token_variants machinery
   as #53's St./Saint pairs) because Overture's US rows are normalized to the
   abbreviated uppercase form -- "AMPHITHEATRE PKWY", "MARKET ST", both
   verified live. DE/NL names need no transformation at all, which is why the
   map is US-only.
4. Deduplicate, in SQL. MARKET ST in San Francisco is 2,980 address points
   collapsing to 900 distinct number|street pairs (R27; 3,006 -> 915 on the
   live 2026-07-22.0 run of this tool): without the GROUP BY an
   undeduplicated top-5 is five spellings of one doorway. The distinct count
   rides back as `distinct_in_range` with the usual truncated note.

Ordering is by distance from the anchor division's *own* point, not from its
bbox centre. San Francisco's boundary reaches the Farallon Islands 45 km
offshore, so its bbox centre is in open water and a centre-ordered answer led
with the far west end of Market St; the division point is the city's label
point, which is what "in San Francisco" means.
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

# #224: the bbox columns carried by the materialized divisions table. Named
# with a bbox_ prefix rather than reusing the struct so the stale-cache check
# is a plain column-name test (see _divisions_table_has_bbox), and so `lat`/
# `lon` -- which are bbox.ymin/bbox.xmin, unchanged since #43 -- keep meaning
# exactly what they meant before.
_DIVISIONS_BBOX_COLUMNS = ("bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax")

# Below this span (degrees, in either axis) a bbox is a point, not an extent.
# Overture's division rows store a point's float32 rounding envelope: measured
# live at 7.6e-6 to 1.5e-5 degrees wide, so this floor sits ~6x above the
# widest observed noise and far below any real division polygon -- even a
# single city block spans more than 1e-4 degrees (~11m). See the module
# docstring's #224 section.
_DEGENERATE_BBOX_SPAN_DEG = 1e-4

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

# #222/R28: how much population a *lower-tier* row needs before its
# prominence is allowed to outrank a higher-tier match that has none.
#
# The rescue below exists for 東京都 (13.9M) over a population-less Nagano
# neighborhood — a real city beating a spelling coincidence. But it was
# gated on `population is not None`, and Overture's population column is
# full of placeholder 1s and 0s: any row carrying one of those counted as
# "prominent" and leapfrogged an exact match on the strength of a single
# fictional inhabitant. Live, that answered "Rafah" with Rafha in Saudi
# Arabia, "Johor" (the Malaysian state) with Johor Bahru, "Enga" (the PNG
# province) with Engativá in Bogotá, and "Plateau" in St Lucia with
# Plateau-Central in Burkina Faso — four wrong answers, three of them
# across a border.
#
# 50k is the scale at which the rescue's own argument holds: a place that
# size is plausibly the one a bare name means, and a place below it is not
# prominent enough to overrule the caller's literal spelling. It is a
# floor on the *rescue*, not on ranking generally — two rows at the same
# tier still order by population all the way down to 1, where a smaller
# real number is still better evidence than no number at all.
_PROMINENCE_RESCUE_FLOOR = 50_000


def _rank_key(row: dict, query: str, region_population: dict[str, int]):
    """Sort key: (#215) literal-over-fuzzy, then (#221) strong-vs-substring
    tier group, then whether the row's prominence can outrank the tier at
    all, then the match tier, then (#47) population, else a documented proxy
    chain of subtype
    rank / hierarchy depth / the row's own region's population, then (#53)
    literal-over-variant, then id for full determinism. All ascending
    (smaller sorts first).

    Tier vs prominence (#221). Tier used to dominate outright, so any
    exact-tier row beat every prefix-tier row no matter what stood behind
    them: live, "東京" put a population-less Nagano neighborhood above 東京都
    and its 13.9M people, because 東京 is exactly the neighborhood's name and
    only a prefix of the prefecture's. The rule now is that a population of
    at least _PROMINENCE_RESCUE_FLOOR outranks the tier, but only within the
    strong (exact/prefix) group and only against a row with no population at
    all — the exact-tier namesakes this rescues past are Overture rows with
    population NULL, and that emptiness is itself the signal (#47) that the
    match is a spelling coincidence rather than the place anyone means. The
    floor is what stops the same rule reading a placeholder population of 1
    as prominence (#222/R28); see the constant. Everything else is
    unchanged and deliberately so: two populated rows still order by tier
    first, so an exact match with 10k people still beats a prefix match with
    10M ("Portland" is not a worse answer than "Portland Heights" because
    the latter is bigger); two population-less rows still order by tier;
    and a substring match still cannot leapfrog either, however populous,
    because the group term sits ahead of the population one. This is the
    same judgement #214 already made one level down — an alternate-name hit
    (`_variant`) wins on its own prominence against a population-less
    literal namesake, which is why "Munich" resolves to München — applied to
    the tier ladder instead of the literal/variant one.

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
    # The prominence-rescue term (see _PROMINENCE_RESCUE_FLOOR). An exact
    # match keeps its population whatever the figure is — nothing outranks
    # it on tier, so the floor would only demote it against a *bigger*
    # prefix match, which is precisely the ordering #221 refuses ("Portland,
    # 10k, is not a worse answer than Portland Heights, 10M"). A prefix
    # match has to clear the floor, because leapfrogging is the only thing
    # this term ever does for it.
    rescued = population is not None and (tier >= 3 or population >= _PROMINENCE_RESCUE_FLOOR)
    depth = len(row.get("admin_context") or [])
    region_pop = region_population.get(row.get("region")) or 0
    return (
        1 if row.get("_fuzzy") else 0,
        -(row.get("_similarity") or 0.0),
        0 if tier >= _STRONG_TIER else 1,
        0 if rescued else 1,
        -tier,
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

    #224 adds the four bbox corners as their own columns. They are internal
    (nothing in a tool response reads them) and, on today's data, degenerate —
    see the module docstring's #224 section and _division_bbox. `bbox` is
    required by the theme, so unlike `population`/`hierarchies` they need no
    probe_schema guard: a dataset without it fails the existing `names` check
    or the COPY itself, both of which the caller already handles.
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
                   {hierarchies_expr},
                   bbox.xmin AS bbox_xmin, bbox.ymin AS bbox_ymin,
                   bbox.xmax AS bbox_xmax, bbox.ymax AS bbox_ymax
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
    _materialize_divisions_table on why this half is best-effort."""
    t0 = time.time()
    try:
        _materialize_alt_names_table(alt_path, glob)
    except (duckdb.Error, overture.UpstreamUnavailable) as e:
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


# #224: divisions tables this process has already checked for the bbox columns
# (and, if they were missing, tried once to rebuild). Same shape and same
# reasoning as _ALT_BUILD_ATTEMPTED above — an existing cache directory has a
# divisions table without the bbox columns, and rather than leaving it that way
# until the next Overture release rolls the cache over, it is rebuilt in place
# the first time the columns are found missing. Bounded to one attempt per
# release per process so a persistent failure costs one try, not one per
# geocode call.
_DIVISIONS_BBOX_CHECKED: set[str] = set()


def _divisions_table_has_bbox(path: Path) -> bool:
    """Whether the materialized table at `path` carries the #224 bbox columns.

    False for any table written before #224, and also for an unreadable one —
    both mean the same thing to the caller (rebuild if you haven't yet, and
    treat bbox lookups as unavailable either way).
    """
    try:
        with overture._conn_lock:
            cols = overture.conn().execute(
                f"SELECT * FROM read_parquet('{path}') LIMIT 0"
            ).description
    except duckdb.Error:
        return False
    names = {c[0] for c in cols or []}
    return all(c in names for c in _DIVISIONS_BBOX_COLUMNS)


def _division_bbox(
    local_table: str | None, division_id: str
) -> tuple[float, float, float, float] | None:
    """(xmin, ymin, xmax, ymax) for `division_id`, or None.

    INTERNAL — #225 (street-level address search) is the intended consumer; no
    tool response exposes this. None means "no usable extent", which covers
    four cases the caller must treat identically: no local table (cache off),
    a table predating #224, an unknown id, and — on release 2026-07-22.0, for
    *every* division row measured — a bbox whose span is below
    _DEGENERATE_BBOX_SPAN_DEG, i.e. a point rather than an extent.

    That last case is the normal one today, not an edge case: division rows
    are points and their bbox is the point's float32 rounding envelope. A
    caller needing a real city extent must join divisions/type=division_area
    on its `division_id` column instead. See the module docstring's #224
    section for the measurements and for why that join is not done here.
    """
    if not local_table:
        return None
    sql = f"""
        SELECT bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
        FROM read_parquet('{local_table}') WHERE id = $id LIMIT 1
    """
    try:
        with overture._conn_lock:
            row = overture.conn().execute(sql, {"id": division_id}).fetchone()
    except duckdb.Error:
        # Pre-#224 table (no such columns), or an unreadable one.
        return None
    if row is None or any(v is None for v in row):
        return None
    xmin, ymin, xmax, ymax = (float(v) for v in row)
    if (xmax - xmin) < _DEGENERATE_BBOX_SPAN_DEG and (ymax - ymin) < _DEGENERATE_BBOX_SPAN_DEG:
        return None
    return xmin, ymin, xmax, ymax


def _rebuild_once_for_bbox_columns(path: Path, glob: str) -> None:
    """Rebuild a pre-#224 divisions table in place so it carries the bbox
    columns, at most once per release per process.

    Failure is logged and swallowed: the existing table is still a perfectly
    good name table, and the only thing missing without the rebuild is a bbox
    lookup that returns None today anyway. Note this also refreshes the #214
    alt table, since _materialize_divisions_table writes both from the one
    upstream read.
    """
    key = str(path)
    if key in _DIVISIONS_BBOX_CHECKED:
        return
    # Recorded before the check, not after, so the schema probe is also once
    # per process: the common case is a table that already has the columns,
    # and re-probing it on every geocode call would be pure overhead.
    _DIVISIONS_BBOX_CHECKED.add(key)
    if _divisions_table_has_bbox(path):
        return
    logger.info("divisions table at %s predates #224 (no bbox columns); rebuilding it", path)
    t0 = time.time()
    try:
        _materialize_divisions_table(path, glob)
    except (duckdb.Error, overture.UpstreamUnavailable) as e:
        logger.warning(
            "divisions table rebuild for bbox columns failed, keeping the "
            "existing table (bbox lookups stay unavailable): %s", e,
        )
        return
    logger.info("divisions table rebuilt with bbox columns in %.1fs", time.time() - t0)


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
    glob = overture.upstream_glob(theme="divisions", type_="division")
    if path.exists():
        _rebuild_once_for_bbox_columns(path, glob)
        return str(path)
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


# #225: USPS street-suffix abbreviations, the same bidirectional shape as
# _ABBR_VARIANTS above and fed through the same _token_variants machinery —
# but only in street mode (see `street=True`), because these words are
# ordinary parts of a *division* name ("Place", "Court", "Drive" all name
# real localities) and swapping them there would search for places nobody
# asked about. Overture's US address rows are upstream-normalized to the
# abbreviated, uppercased form (live on 2026-07-22.0: "AMPHITHEATRE PKWY",
# "MARKET ST"), so the expansion->abbreviation direction is the one that
# does the work; the reverse is here so a caller who types the abbreviation
# still matches a dataset that spells it out. DE/NL street names need no
# transformation at all — "Hauptstraße" is one token in both the query and
# the data (R27-verified), which is why this map is US-only.
_STREET_SUFFIX_VARIANTS: dict[str, list[str]] = {
    "street": ["St"], "st": ["Street"],
    "avenue": ["Ave"], "ave": ["Avenue"],
    "parkway": ["Pkwy"], "pkwy": ["Parkway"],
    "boulevard": ["Blvd"], "blvd": ["Boulevard"],
    "road": ["Rd"], "rd": ["Road"],
    "drive": ["Dr"], "dr": ["Drive"],
    "lane": ["Ln"], "ln": ["Lane"],
    "court": ["Ct"], "ct": ["Court"],
    "place": ["Pl"], "pl": ["Place"],
}


def _token_variants(token: str, leading: bool, street: bool = False) -> list[str]:
    """Alternate spellings for one query token.

    `street` (#225) turns on the USPS suffix map and lifts the leading-token
    restriction on the cardinal directions: "N" is too ambiguous to expand in
    the middle of a division name, but a street name is exactly where "W 42nd
    St" vs "West 42nd Street" happens, and the token is bounded by a street
    field rather than by free text.
    """
    key = token.strip(".").lower()
    variants = list(_ABBR_VARIANTS.get(key, []))
    if street:
        variants += _STREET_SUFFIX_VARIANTS.get(key, [])
    if leading or street:
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
    """
    folded = _fold_alt_name(query)
    q = overture._like_escape(folded)
    params: dict = {"pattern": f"%{q}%", "exact": q, "prefix": f"{q}%"}
    region_filter = ""
    if region_code:
        region_filter = "AND d.region = $region_code"
        params["region_code"] = region_code
    sql = f"""
        SELECT d.id, d.name, d.subtype, d.country, d.region, d.lat, d.lon,
               d.admin_chain, d.population, a.alt_name, a.alt_display
        FROM read_parquet('{alt_table}') a
        JOIN read_parquet('{table_path}') d ON d.id = a.id
        WHERE a.alt_name ILIKE $pattern ESCAPE '\\'
        {region_filter}
        ORDER BY {_match_tier_order_sql("a.alt_name")}
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
        rows = _query_divisions_from_local(local_table, query, region_code, name_expr)
        if alt_table is not None:
            seen = {r["id"] for r in rows}
            rows = rows + [
                r
                for r in _query_alt_names(alt_table, local_table, query, region_code)
                if r["id"] not in seen
            ]
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


def _query_divisions_fuzzy(table_path: str, query: str) -> list[dict]:
    """Divisions whose folded name is within _FUZZY_SIMILARITY_THRESHOLD
    jaro-winkler of the folded query — the #215 typo tier.

    No region filter, unlike the literal queries: this only runs when the
    literal search came back empty, and a region-constrained literal miss
    has already dropped its region code and retried unconstrained by then
    (see geocode_detailed) — so there is never a live region constraint
    left to honor here.

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
    """
    params: dict = {"folded": _normalize_for_match(query)}
    sql = f"""
        SELECT id, name, subtype, country, region, lat, lon, admin_chain, population,
               jaro_winkler_similarity({_FOLDED_NAME_SQL}, $folded) AS similarity
        FROM read_parquet('{table_path}')
        WHERE jaro_winkler_similarity({_FOLDED_NAME_SQL}, $folded)
              >= {_FUZZY_SIMILARITY_THRESHOLD}
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
    which is strictly better than that. The significance rule is
    _significant_words' — >=3 chars and not a _STOPWORD — so this stays in
    step with how resolve_place decides which words are worth searching on.

    Note this gate is about *emptiness*, not correctness: a misspelling
    like "Sna Francisco" (anchor "Francisco", residual "Sna") clears it
    just fine — "Sna" is a significant word by this rule. Typos are
    handled a step earlier instead: #215's fuzzy tier retries the whole
    query by edit distance and, when it finds a correction, stands the
    places fallback down before this function is ever reached. Without a
    local divisions table for that tier to read (cache off), such a query
    still lands here and still reaches the substring scan of its own typo.
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
        name_query = base if _significant_words(base) else None
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


# --- #223: postcode-shaped queries -----------------------------------------

# Whole-query shapes that are postcodes and nothing else. Matched against the
# query uppercased with internal whitespace collapsed, anchored at both ends:
# a postcode is the *entire* query or this path does not run. Deliberately
# conservative -- anything that could also be a name stays a name query, so
# "10 Downing Street" (a number and words) and a bare outward code like "SW1A"
# (which is also how plenty of things are abbreviated) never enter here, while
# "94110", "1011AB" and "SW1A 1AA" do.
#
# The cost of a false positive is one wasted upstream aggregate plus a
# fallthrough to the normal name search; the cost of a false negative is
# today's empty answer. That asymmetry still doesn't buy loose patterns: the
# aggregate is a ~12s cold scan of a 474M-row theme (R27, measured), which is
# not a thing to spend on "1984".
_POSTCODE_PATTERNS = (
    re.compile(r"^\d{4}$"),           # AT BE AU CH DK HU LU NO NZ SI ...
    re.compile(r"^\d{5}$"),           # US ZIP, DE, FR, ES, IT, FI, MX
    re.compile(r"^\d{6}$"),           # SG
    re.compile(r"^\d{5}-\d{4}$"),     # US ZIP+4
    re.compile(r"^\d{5}-\d{3}$"),     # BR CEP
    re.compile(r"^\d{4}-\d{3}$"),     # PT
    re.compile(r"^\d{4} ?[A-Z]{2}$"),                 # NL 1011AB / 1011 AB
    re.compile(r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$"),         # CA M5V 3L9
    re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$"),  # GB SW1A 1AA (full only)
)

# NL codes specifically split 4|2; every other spaced shape here splits before
# its last three characters (GB "SW1A 1AA", CA "M5V 3L9").
_NL_POSTCODE = re.compile(r"^\d{4}[A-Z]{2}$")

# One row per country carrying the code -- ten is already more ambiguity than
# an answer can usefully carry, and the real ones run to three.
_POSTCODE_MAX_COUNTRIES = 10

# Covered countries whose address rows carry no postcode value at all
# (R27, measured against release 2026-07-22.0). Membership in
# addresses.COVERED_COUNTRIES therefore does NOT imply a postcode can be
# looked up here, which is exactly what the empty-result note has to say.
_POSTCODE_ZERO_COUNTRIES = ("CL", "CO", "EE", "HK", "IT", "JP", "NZ", "RS", "TW")

# How far from a postcode centroid a division may sit and still be reported as
# the place that code is in. A postcode centroid with nothing named within
# 25km is better answered by coordinates alone than by naming a town half a
# region away.
_POSTCODE_LOCALITY_MAX_M = 25_000

# Lat/lon window (degrees) prefiltering the local divisions table before the
# distance sort -- generous next to _POSTCODE_LOCALITY_MAX_M, and only there
# so the nearest-division scan reads a slice rather than the whole table.
_POSTCODE_LOCALITY_WINDOW_DEG = 0.5

# Haversine against the local divisions table's flat lat/lon columns, which is
# the one thing that table does not share with the raw theme (it stores the
# bbox corner overture.DISTANCE_EXPR reads as plain columns -- see
# _materialize_divisions_table).
_LOCAL_DISTANCE_EXPR = """2 * 6371000 * asin(sqrt(
                pow(sin(radians(lat - $lat) / 2), 2)
                + cos(radians($lat)) * cos(radians(lat))
                * pow(sin(radians(lon - $lon) / 2), 2)
            ))"""

_POSTCODE_LOCALITY_SUBTYPES = "('locality', 'localadmin', 'neighborhood')"


def _postcode_variants(query: str) -> list[str] | None:
    """The postcode spellings to search for `query`, or None if it isn't
    postcode-shaped.

    Returns both the spaced and unspaced spelling of the mixed letter/digit
    shapes, because Overture stores whichever the source data used ("1011AB"
    in NL, "SW1A 1AA" in GB) and a caller types whichever they know. Both go
    into one IN-list, so this stays one scan.
    """
    q = " ".join(query.strip().upper().split())
    if not any(p.match(q) for p in _POSTCODE_PATTERNS):
        return None
    variants = {q}
    if " " in q:
        variants.add(q.replace(" ", ""))
    elif any(c.isalpha() for c in q):
        if _NL_POSTCODE.match(q):
            variants.add(f"{q[:4]} {q[4:]}")
        elif len(q) >= 5:
            variants.add(f"{q[:-3]} {q[-3:]}")
    return sorted(variants)


def _postcode_display(query: str) -> str:
    """The spelling a postcode result is reported under: the caller's own,
    uppercased and whitespace-collapsed. Not normalized further -- we don't
    know which spelling the country actually uses, only which one matched."""
    return " ".join(query.strip().upper().split())


def _query_postcode_countries(variants: list[str]) -> list[tuple]:
    """One upstream aggregate over the addresses theme: (country, count,
    lat, lon) per country carrying this postcode, most points first.

    Deliberately NOT routed through cache.py's tile machinery, unlike every
    other addresses read (addresses._from_source). A tile is a bbox, and this
    query has no bbox: "which countries carry 94110" is a global question, and
    the tile cache would either have to be complete (the whole theme
    materialized) or answer from a slice, which for this query is not a
    slower answer but a wrong one. So it is a direct upstream scan with the
    usual duckdb.Error -> UpstreamUnavailable conversion, ~12s cold
    (R27-measured); the note says so when the read is remote.
    """
    glob = addresses._upstream_glob()
    cols = overture.probe_schema(glob)
    if cols is not None and ("postcode" not in cols or "country" not in cols):
        return []
    params = {f"v{i}": v for i, v in enumerate(variants)}
    in_list = ", ".join(f"${k}" for k in params)
    sql = f"""
        SELECT country, count(*) AS n,
               avg(bbox.ymin) AS lat, avg(bbox.xmin) AS lon
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE postcode IN ({in_list}) AND country IS NOT NULL
        GROUP BY country
        ORDER BY n DESC, country
        LIMIT {_POSTCODE_MAX_COUNTRIES}
    """
    try:
        with overture._conn_lock:
            return overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e


def _covering_division_from_local(lat: float, lon: float, local_table: str) -> dict | None:
    """Nearest locality-ish division to a point, from the #43 local table.

    60ms measured (R27) against the already-materialized table, which is why
    the postcode answer can afford to name a place per country rather than
    handing back bare coordinates.
    """
    sql = f"""
        SELECT name, subtype, admin_chain,
               {_LOCAL_DISTANCE_EXPR} AS distance_m
        FROM read_parquet('{local_table}')
        WHERE subtype IN {_POSTCODE_LOCALITY_SUBTYPES}
          AND lat BETWEEN $lat - {_POSTCODE_LOCALITY_WINDOW_DEG}
                      AND $lat + {_POSTCODE_LOCALITY_WINDOW_DEG}
          AND lon BETWEEN $lon - {_POSTCODE_LOCALITY_WINDOW_DEG}
                      AND $lon + {_POSTCODE_LOCALITY_WINDOW_DEG}
        ORDER BY distance_m
        LIMIT 1
    """
    try:
        with overture._conn_lock:
            row = overture.conn().execute(sql, {"lat": lat, "lon": lon}).fetchone()
    except duckdb.Error as e:
        logger.warning("local divisions lookup for postcode locality failed: %s", e)
        return None
    if row is None or row[3] > _POSTCODE_LOCALITY_MAX_M:
        return None
    return {"name": row[0], "admin_context": [*_admin_chain_context(row[2], self_name=row[0]),
                                              row[0]]}


def _covering_division(lat: float, lon: float, local_table: str | None) -> dict | None:
    """The place a postcode centroid sits in, local table first.

    Falls back to _nearest_division's upstream scan when there is no local
    table (PLACEROOT_CACHE=off, or materialization failed) -- the same
    degrade every other #43 caller makes, and it keeps the postcode answer
    naming a place rather than dropping to coordinates just because caching
    is off.
    """
    if local_table is not None:
        return _covering_division_from_local(lat, lon, local_table)
    return _nearest_division(lat, lon)


def _postcode_results(
    display: str, rows: list[tuple], local_table: str | None
) -> list[dict]:
    """Aggregate rows -> geocode result rows, type "postcode".

    Same shape every other geocode row has (name/type/lat/lon/id/
    admin_context/rank_score) so a caller needs no new parsing, plus the two
    facts that only exist for this type: which country the row is in, and how
    many address points carry the code there. `id` is None -- a postcode is
    not a GERS entity (no postal_code division subtype exists in
    2026-07-22.0, verified across all 9 subtypes), and inventing an id for
    one would be the one dishonest field in the row.

    rank_score is the row's share of the largest country's point count, so
    it says what it is measured on: 94110's US row scores 1.0 and its SK row
    0.12 because that is the ratio of the evidence behind them.
    """
    top = max((r[1] for r in rows), default=0) or 1
    results = []
    for country, count, lat, lon in rows:
        lat, lon = round(lat, 6), round(lon, 6)
        covering = _covering_division(lat, lon, local_table)
        results.append({
            "name": display,
            "type": "postcode",
            "lat": lat,
            "lon": lon,
            "id": None,
            "admin_context": covering["admin_context"] if covering else [],
            "rank_score": round(count / top, 3),
            "country": country,
            "address_count": count,
        })
    return results


def _postcode_coverage_sentence() -> str:
    covered = len(addresses.COVERED_COUNTRIES)
    zero = ", ".join(_POSTCODE_ZERO_COUNTRIES)
    return (
        f"postcodes here come from Overture's addresses theme, which carries "
        f"{covered} countries (no UK, Ireland, India or China at all), and "
        f"{len(_POSTCODE_ZERO_COUNTRIES)} of those ({zero}) carry no postcode "
        f"values whatsoever"
    )


def _postcode_cold_scan_sentence() -> str:
    """Said only when the aggregate actually went over the network -- against
    a local dataset or mirror it would be a lie."""
    if not _is_remote(addresses._upstream_glob()):
        return ""
    return (
        " This is an unindexed scan of the whole addresses theme, so the first "
        "such query in a session costs ~12s."
    )


def _postcode_note(display: str) -> str:
    """Note accompanying a postcode answer that found something."""
    return (
        f"\"{display}\" was read as a postcode, not a name: one aggregate over "
        f"Overture's addresses theme, one row per country whose address points "
        f"carry that code, each point being the mean of those points and "
        f"address_count the evidence behind it. Several countries can share a "
        f"code and often do, so the alternates below the top row are real "
        f"ambiguity rather than mis-ranking. Granularity varies by country -- a "
        f"Dutch code is about one street block, a US ZIP about a district -- so "
        f"the centroid is a neighborhood-scale answer at best, never a doorway. "
        f"Coverage: {_postcode_coverage_sentence()}."
        f"{_postcode_cold_scan_sentence()}"
    )


def _postcode_empty_note(display: str) -> str:
    """Note for a query that is postcode-shaped but matched no address point.

    The whole point of this note: an empty answer here is not evidence the
    code does not exist. It is much more often evidence the country is
    outside the theme (GB) or inside it without postcode values (IT, JP).
    """
    return (
        f"\"{display}\" is postcode-shaped, but no address point in Overture "
        f"carries it -- which is not the same as it not existing: "
        f"{_postcode_coverage_sentence()}. So a postcode in the UK, Italy or "
        f"Japan comes back empty here whether or not it is real. The name "
        f"search was run too and also found nothing."
        f"{_postcode_cold_scan_sentence()}"
    )


def geocode(query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Free-text place name -> ranked candidates. See geocode_detailed."""
    return geocode_detailed(query, limit)["results"]


def geocode_detailed(
    query: str, limit: int = DEFAULT_LIMIT, include_country: bool = False
) -> dict:
    """Free-text place name -> ranked candidates, from Overture divisions (and places fallback).

    Returns {"results": [...]} and, when the places-name half of the search
    is skipped as not worth its cost — no derivable location context to
    bound it by (#105), or nothing but stopwords left to search names for
    (#216) — a "note" saying so and how to make the query answerable.

    A "note" also comes back *with* results when nothing matched literally
    and the answer came from the #215 fuzzy tier instead: it names the
    spelling the results were corrected to ("Berekley" -> "Berkeley"), so
    a caller can tell a correction from a match.

    A query that is entirely a postcode ("94110", "1011AB", "SW1A 1AA") is
    answered from the addresses theme instead of by name (#223): one row per
    country carrying that code, `type` "postcode", `id` None, plus `country`
    and `address_count`, with a "note" on both the granularity of a postcode
    centroid and the theme's coverage. See the module docstring's #223
    section.

    Never more than `limit` results. Each result: {name, type, lat, lon, id
    (GERS), admin_context, rank_score}, plus (#214) `matched_name` on the
    rows found through one of Overture's alternate names rather than the
    canonical one — "Munich" answers München, with matched_name "Munich".
    Raises overture.UpstreamUnavailable
    if the remote scan fails after retries; the caller (server.py) turns
    that into a structured error like the other tools.

    Handles "City, ST" / "City, Region" suffixes (#46) and ranks same-tier
    ties by population/prominence (#47) — see the module docstring.

    include_country adds each row's ISO country code (None for a places-
    fallback row, which has no admin chain to read one off) to the result
    dicts. Off by default, and deliberately not part of the MCP tool's
    payload — it exists for geocode_address, which has to reject a
    runner-up anchor sitting in a different country than the top candidate
    ("London" -> London, Ontario for a UK query) and cannot do that from
    admin_context alone once a row's chain is empty.
    """
    query = query.strip()
    limit = max(1, min(limit, MAX_LIMIT))
    if not query:
        return {"results": []}

    note = None
    local_table = _local_divisions_table()

    # #223: a query that is *entirely* a postcode is not a name lookup, and
    # searching division names for "94110" finds nothing by construction. One
    # upstream aggregate over the addresses theme answers it instead, per
    # country carrying the code. A postcode-shaped query that matches nothing
    # still falls through to the name search below -- the shape detector is
    # conservative but not infallible, and a real name that happens to be
    # shaped like a postcode must still be findable. Its note is kept aside
    # (postcode_note) rather than assigned to `note`: when both halves come
    # back empty, "this is what an empty postcode answer means" is the more
    # useful of the two explanations, so it wins at the return below.
    postcode_note = None
    variants = _postcode_variants(query)
    if variants:
        display = _postcode_display(query)
        postcode_rows = _query_postcode_countries(variants)
        if postcode_rows:
            return {
                "results": _postcode_results(display, postcode_rows, local_table)[:limit],
                "note": _postcode_note(display),
            }
        postcode_note = _postcode_empty_note(display)

    # #214: None whenever there is no alternate-name table to search — cache
    # off, a cache directory predating the feature, a dataset without
    # names.common — in which case every _query_divisions call below is
    # exactly the primary-name-only search it was before.
    alt_table = _local_alt_names_table(local_table)
    base_query, region_code, _region_name = _parse_region_suffix(query, local_table)
    search_query = base_query if region_code else query

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
    seen_ids = {c["id"] for c in divisions}
    variant_rows: list[dict] = []
    if best_literal_tier < _STRONG_TIER or not literal_match_has_prominence:
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

    # #221: the diacritic-folded pass runs unconditionally, outside the
    # "literal match lacks prominence" gate above. Under the gate it never
    # ran for "Zurich": the literal ILIKE finds a Dutch village spelled
    # exactly that, and it carries a population (190), so the gate read the
    # literal search as having already found something real — while Zürich,
    # 443k, was not merely ranked below but absent from the candidate pool
    # entirely, since ILIKE '%Zurich%' does not match "Zürich". A prominence
    # gate cannot work here: whether the folded spelling is worth looking
    # for has nothing to do with how prominent the *unfolded* one turned out
    # to be. So the pass always runs and _rank_key decides, which is what it
    # is for. Cheap enough to do every time — one more predicate over the
    # same local divisions table the literal pass just read (0.2s measured,
    # #214).
    #
    # The abbreviation retries above stay gated: they fire one extra query
    # per expandable token ("St." -> "Saint", "N." -> "North"), and unlike
    # folding they rewrite the query into a genuinely different string, so
    # running them against an already-good literal answer buys noise rather
    # than reach.
    stripped_query = _strip_diacritics(search_query)
    for row in _query_divisions(stripped_query, region_code, local_table, fold_diacritics=True):
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
    fuzzy_rows: list[dict] = []
    if not divisions and local_table is not None and not _has_like_metacharacter(search_query):
        fuzzy_rows = _query_divisions_fuzzy(local_table, search_query)
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
        if include_country:
            entry["country"] = row.get("country")
        if row.get("_matched_name"):
            # #214: only present when the row was found through one of
            # Overture's alternate (names.common) spellings rather than its
            # canonical one. `name` stays canonical either way, so this is
            # what tells a caller "Munich" found "München" on purpose.
            entry["matched_name"] = row["_matched_name"]
        out.append(entry)
    result = {"results": out}
    fuzzy_out = [row for row in candidates[:limit] if row.get("_fuzzy")]
    if fuzzy_out:
        # #215: unlike the skip notes below, this one accompanies real
        # results — it says which spelling they actually answer.
        result["note"] = _fuzzy_correction_note(fuzzy_out, search_query)
    if postcode_note and not out:
        # #223: the query was postcode-shaped, the postcode aggregate found
        # nothing, and neither did the name search. What that emptiness means
        # is a coverage fact, not a "the places half was skipped" fact.
        result["note"] = postcode_note
    elif note and not out:
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

# Small enough to filter, generic enough that requiring them in a name
# match would be actively wrong ("the Whole Foods on Lamar" — "the"/"on"
# aren't part of any real place name). Dropped before a query is split into
# per-token find_places searches and before word-overlap scoring.
_STOPWORDS = {"the", "a", "an", "on", "in", "at", "near", "of", "and", "by"}


def _match_label(name: str, query: str) -> str:
    return _MATCH_TIER_LABELS[_match_tier(name, query)]


# resolve_place runs one find_places per significant token, each taking the
# shared DuckDB connection lock — so an unbounded token count from a huge
# query string is a lock-contention DoS against the whole server, not just a
# slow response. A real place reference ("the Whole Foods on South Lamar,
# Austin") is a handful of words; cap the fan-out well above that.
_MAX_RESOLVE_TOKENS = 12


def _significant_words(text: str) -> list[str]:
    """text -> the words in it worth searching a name index on: >=3 chars and
    not a _STOPWORD, in order of appearance.

    The shared rule behind _significant_tokens (which layers resolve_place's
    fan-out cap and its own never-search-nothing fallback on top) and
    _fallback_anchor's residual gate (#216, which needs the raw answer —
    "nothing here is worth searching for" is exactly the case it acts on).
    """
    return [t for t in re.findall(r"[\w'-]+", text) if len(t) >= 3 and t.lower() not in _STOPWORDS]


def _significant_tokens(query: str) -> list[str]:
    """query -> its meaningful words: >=3 chars, not a stopword, in order of
    appearance, capped at _MAX_RESOLVE_TOKENS. Falls back to the whole query
    if nothing survives (e.g. a query that's all short/stopword tokens)
    rather than searching nothing.
    """
    tokens = [t for t in re.findall(r"[\w'-]+", query) if len(t) >= 3]
    return (_significant_words(query) or tokens or [query])[:_MAX_RESOLVE_TOKENS]


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
    "lat", "lon", "match": "exact" | "prefix" | "substring", plus
    "admin_context" (division) or "category" (place)}. Ranked by match tier
    first — kind-agnostic, an exact place beats a prefix-matched division —
    then by prominence (division rank_score / place confidence, both
    roughly 0-1 scales), then id for determinism. Never more than `limit`
    results.

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
            "match": _match_label(r["name"], query),
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
        -{"exact": 3, "prefix": 2, "substring": 1}[c["match"]],
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


# --- #225: street-level forward search --------------------------------------

ADDRESS_DEFAULT_LIMIT = 5
# Capped low for the same reason address_at is: past a handful of doorways a
# street answer stops being an answer and becomes a dump of the street. The
# distinct-in-range count tells the caller how much was left behind.
ADDRESS_MAX_LIMIT = 10

# Cap on the whole-street spelling variants one query is expanded into. The
# expansion is a cartesian product over per-token alternates, so a street with
# a directional *and* a suffix ("W 42nd St") legitimately needs four; the cap
# only stops a pathological query from turning into an unbounded OR list.
_STREET_VARIANT_CAP = 16

# A house-number token: digits only. Overture's `number` is a string and real
# data carries "74B" and "12 bis", but this is the *query* side — a bare
# integer at either end of the street half is the shape that is unambiguously
# a house number ("1600 Amphitheatre Pkwy", "Hauptstraße 5"). Unit numbers
# ("Apt 3", "#204") are deliberately out of scope: they sit in a separate
# `unit` column, and guessing which trailing integer is which would silently
# search for the wrong doorway.
_HOUSE_NUMBER_RE = re.compile(r"^\d+$")

# Columns the address scan reads before grouping. postal_city is read but
# never returned: it is what "prefer the anchor's own municipality" sorts on
# (see _scan_addresses_in_bbox).
_ADDRESS_SELECT_COLUMNS = ("number", "street", "unit", "postcode", "country", "postal_city")

# One anchor bbox per (release, division_id) per process. The division_area
# lookup below is an id-filtered scan of a theme with no bbox to prune by --
# 10.7s cold, measured live on 2026-07-22.0 -- and a caller working through
# the addresses of one city pays it once instead of once per query.
_AREA_BBOX_CACHE: dict[tuple[str, str], tuple[float, float, float, float] | None] = {}


def _street_variants(street: str) -> list[str]:
    """Street name -> the spellings to match against Overture's `street`.

    The original first, then the cartesian product of every token's
    alternates (#225's USPS suffix map plus the cardinals and the existing
    St./Ft./Mt. pairs), deduplicated case-insensitively and capped at
    _STREET_VARIANT_CAP.

    A product rather than _abbreviation_variant_queries' one-swap-at-a-time
    list because a street name routinely needs two swaps at once: a query for
    "West 42nd Street" has to reach "W 42ND ST", which no single swap
    produces.
    """
    tokens = street.split()
    if not tokens:
        return []
    choices = [[tok, *_token_variants(tok, leading=(i == 0), street=True)]
               for i, tok in enumerate(tokens)]
    out: list[str] = []
    seen: set[str] = set()
    combos: list[list[str]] = [[]]
    for options in choices:
        combos = [c + [o] for c in combos for o in options]
        if len(combos) > _STREET_VARIANT_CAP:
            # Truncate the frontier rather than the finished list, so the cap
            # cannot drop the original spelling (always the first branch).
            combos = combos[:_STREET_VARIANT_CAP]
    for combo in combos:
        candidate = " ".join(combo)
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out[:_STREET_VARIANT_CAP]


def _split_house_number(text: str) -> tuple[str | None, str]:
    """"1600 Amphitheatre Pkwy" -> ("1600", "Amphitheatre Pkwy");
    "Hauptstraße 5" -> ("5", "Hauptstraße"); "Market Street" -> (None, ...).

    Leading or trailing only, and never when it is the *whole* string — a
    query of nothing but digits is a postcode-shaped thing for geocode() to
    read, not a house number with an empty street.
    """
    tokens = text.split()
    if len(tokens) < 2:
        return None, text.strip()
    if _HOUSE_NUMBER_RE.match(tokens[0]):
        return tokens[0], " ".join(tokens[1:])
    if _HOUSE_NUMBER_RE.match(tokens[-1]):
        return tokens[-1], " ".join(tokens[:-1])
    return None, text.strip()


def _parse_address_query(query: str) -> tuple[str | None, str, str | None]:
    """Free-text address -> (number, street, city).

    One rule: the first comma separates the street half from the place half,
    and everything after it is handed to geocode() whole — so "1600
    Amphitheatre Parkway, Mountain View, CA" anchors on "Mountain View, CA"
    and geocode's own "City, ST" parsing (#46) does the rest. Without a comma
    there is no place half at all, and `city` comes back None: geocode_address
    then declines to scan rather than guessing a city out of the street name.
    """
    parts = [p.strip() for p in query.split(",")]
    if not parts:
        return None, "", None
    # Positions are kept, not compacted: a leading comma means the street
    # half is genuinely empty ("no street to search for"), and compacting it
    # away would promote the city into the street slot and search for a
    # street named "San Francisco".
    city = ", ".join(p for p in parts[1:] if p) or None
    number, street = _split_house_number(parts[0])
    return number, street, city


def _division_area_bbox(division_id: str) -> tuple[float, float, float, float] | None:
    """The real extent of a division, from divisions/type=division_area.

    _division_bbox (#224) is tried first by the caller and returns None for
    every division row in release 2026-07-22.0 — those rows are points, and
    their bbox is the point's float32 rounding envelope. The genuine polygon
    extents live one type over, joined by `division_area.division_id ==
    division.id` (verified live: 15f1bd57-… "Mountain View" ->
    -122.1176,37.3542 .. -122.0449,37.4711, ~6.4 x 13 km).

    Aggregated rather than LIMIT 1 because a division may be filed as several
    area rows (multi-part boundaries); the union of their corners is the
    extent, and one aggregate is the same single scan either way. Returns
    None for an unknown id, a dataset without the join column, a failed scan,
    or an extent still under _DEGENERATE_BBOX_SPAN_DEG — all of which mean
    the same thing to the caller: no bbox, so no scan.
    """
    key = (release.resolve_release(), division_id)
    if key in _AREA_BBOX_CACHE:
        return _AREA_BBOX_CACHE[key]
    glob = overture.upstream_glob(theme="divisions", type_="division_area")
    missing = set(overture.missing_columns(glob, ["bbox", "division_id"]))
    result: tuple[float, float, float, float] | None = None
    if not missing:
        sql = f"""
            SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax)
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE division_id = $id
        """
        try:
            with overture._conn_lock:
                row = overture.conn().execute(sql, {"id": division_id}).fetchone()
        except duckdb.Error as e:
            logger.warning("division_area extent lookup failed for %s: %s", division_id, e)
            row = None
        if row and not any(v is None for v in row):
            xmin, ymin, xmax, ymax = (float(v) for v in row)
            if (xmax - xmin) >= _DEGENERATE_BBOX_SPAN_DEG or (
                ymax - ymin
            ) >= _DEGENERATE_BBOX_SPAN_DEG:
                result = (xmin, ymin, xmax, ymax)
    _AREA_BBOX_CACHE[key] = result
    return result


def _anchor_bbox(anchor_id: str | None, local_table: str | None):
    """The city extent to bound an address scan by, or None.

    #224's _division_bbox first — free, already materialized, and the path
    that starts working on its own if a future Overture release populates
    real extents on the division rows. Then the division_area join, which is
    what actually answers today. None from both is a hard stop, not a
    fallback to a guessed radius: an address scan over the wrong box returns
    confidently wrong doorways, and #225's contract is an honest empty
    instead.
    """
    if not anchor_id:
        return None
    return _division_bbox(local_table, anchor_id) or _division_area_bbox(anchor_id)


def _scan_addresses_in_bbox(
    bbox: tuple[float, float, float, float],
    origin: tuple[float, float],
    street_patterns: list[str],
    number: str | None,
    limit: int,
    locality: str | None = None,
) -> tuple[list[tuple], int, int]:
    """Deduplicated address rows inside `bbox`, nearest `origin` first.

    `origin` is the anchor division's own reference point, not the bbox
    centre. The two diverge more than they look: San Francisco's boundary
    includes the Farallon Islands 45 km out to sea, so its bbox centre sits
    in open water and "nearest first" off it ranks the westernmost end of
    Market St ahead of downtown. The division point is the city's label
    point, which is what a caller means by "in San Francisco".

    Returns (rows, distinct_in_range, matched_rows). Dedup is not optional:
    Overture files one address point per source contribution, so MARKET ST in
    San Francisco is 2,980 rows collapsing to 900 distinct number|street
    pairs (R27, live) — an undeduplicated top-5 is five spellings of the same
    doorway. Grouping happens in SQL so the wire never carries the 2,980.

    The group key is (number, street, postcode), not (number, street)
    (#229, R28). A city bbox is not a municipality: Boston's box covers
    Hingham, Charlestown and Cambridge, all of which have a 1 Main St, and
    grouping without the postcode collapsed three real, different doorways
    into one arbitrarily-chosen row — an answer that is wrong rather than
    merely incomplete. The postcode is the cheapest field that separates
    them; a real point-in-polygon municipality test would be correct for
    the rest but costs a polygon join per row, which this scan is not the
    place for. `locality` (the anchor's own name) breaks the remaining tie
    softly: rows whose postal_city is the anchor's municipality sort ahead
    of the neighbours that share its bbox, before distance decides.

    `unit` is likewise no longer an arg_min pick off the group. A doorway
    with 458 units (live: 1 Franklin St, Boston) has no "the" unit, and
    naming whichever one happened to sit nearest is a guess dressed as a
    fact. It comes back only when the group carries exactly one distinct
    unit; otherwise the count does, as `unit_count`.

    Reads through addresses._from_source, so this shares the #202 tile cache
    with address_at and reverse_geocode's address hop: the second query in a
    city the cache already holds is a local parquet read.
    """
    xmin, ymin, xmax, ymax = bbox
    lat, lon = origin
    glob = addresses._upstream_glob()
    missing = set(addresses._check_schema(glob))
    columns = ", ".join(addresses._column_expr(c, missing) for c in _ADDRESS_SELECT_COLUMNS)
    params: dict = {"lat": lat, "lon": lon, "xmin": xmin, "ymin": ymin,
                    "xmax": xmax, "ymax": ymax}
    street_sql = []
    for i, pattern in enumerate(street_patterns):
        params[f"s{i}"] = overture._like_escape(pattern)
        street_sql.append(f"street ILIKE ${f's{i}'} ESCAPE '\\'")
    number_sql = ""
    if number is not None and "number" not in missing:
        params["number"] = number
        number_sql = " AND number = $number"
    # Rows in the anchor's own municipality first. A soft preference, not a
    # filter: postal_city is missing on plenty of real rows, and dropping
    # those would turn a partial field into an invisible coverage hole.
    locality_rank = "0"
    if locality and "postal_city" not in missing:
        params["locality"] = locality
        locality_rank = "CASE WHEN lower(postal_city) = lower($locality) THEN 0 ELSE 1 END"
    sql = f"""
        WITH matched AS (
            SELECT {columns},
                   bbox.ymin AS lat, bbox.xmin AS lon,
                   {overture.DISTANCE_EXPR} AS d
            FROM {addresses._from_source(bbox)}
            WHERE bbox.xmin BETWEEN $xmin AND $xmax
              AND bbox.ymin BETWEEN $ymin AND $ymax
              AND ({" OR ".join(street_sql)}){number_sql}
        ),
        grouped AS (
            SELECT number, street, postcode,
                   count(DISTINCT unit) AS unit_count,
                   CASE WHEN count(DISTINCT unit) = 1 THEN min(unit) END AS unit,
                   arg_min(country, d) AS country,
                   min({locality_rank}) AS locality_rank,
                   round(arg_min(lat, d), 6) AS lat,
                   round(arg_min(lon, d), 6) AS lon,
                   round(min(d), 1) AS distance_m,
                   count(*) AS n
            FROM matched GROUP BY number, street, postcode
        )
        SELECT number, street, unit, unit_count, postcode, country, lat, lon, distance_m,
               count(*) OVER () AS distinct_in_range,
               sum(n) OVER () AS matched_rows
        FROM grouped
        ORDER BY locality_rank, distance_m, street NULLS LAST, number NULLS LAST,
                 postcode NULLS LAST
        LIMIT {limit}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    if not rows:
        return [], 0, 0
    return rows, int(rows[0][-2]), int(rows[0][-1])


def _address_row(row: tuple) -> dict:
    """One grouped row -> the response shape. unit/postcode are dropped when
    null, the same padding-is-not-an-answer rule address_at applies.

    `unit_count` replaces `unit` when the doorway carries more than one
    (#229, R28): "which of the 458 units" is a question this tool cannot
    answer, and naming one of them would be a fabricated answer to it.
    """
    number, street, unit, unit_count, postcode, country, lat, lon, distance_m = row[:9]
    out = {
        "number": number,
        "street": street,
        "unit": unit,
        "postcode": postcode,
        "country": country,
        "distance_m": distance_m,
        "lat": lat,
        "lon": lon,
    }
    for field in ("unit", "postcode"):
        if not out[field]:
            del out[field]
    if unit_count and unit_count > 1:
        out["unit_count"] = int(unit_count)
    return out


def _address_empty_note(origin: tuple[float, float], street: str) -> str:
    """Why a scan inside a resolved city extent found no such street.

    Coverage first, because it is the answer far more often than "no such
    street": the addresses theme is alpha and carries
    addresses.COVERED_COUNTRIES only, so a Manchester street search comes
    back empty whether or not the street exists. Reuses address_at's
    containment lookup so both tools name the same country by the same rule.
    """
    country = addresses._country_at(*origin)
    covered = len(addresses.COVERED_COUNTRIES)
    if country.status == addresses.RESOLVED and not addresses._is_covered(country.code):
        return (
            f"no Overture address coverage for {country.label}, so this empty result "
            f"means no data rather than no such street: the addresses theme is alpha "
            f"and carries {covered} countries. Try geocode or find_places for a "
            f"named landmark on the street instead."
        )
    return (
        f"no address point in this city matches \"{street}\" (abbreviated and "
        f"spelled-out spellings were both tried). Coverage inside a covered country "
        f"is partial -- the addresses theme is alpha and carries {covered} countries "
        f"-- so this may be a gap in the data rather than a missing street. Check "
        f"the spelling, or drop the house number to see whether the street itself "
        f"is present."
    )


_ADDRESS_NO_ANCHOR_NOTE = (
    "no city to search in, so no scan was run. A street name alone has no extent to "
    "bound a search by, and scanning Overture's 474M address points unbounded is not "
    "an answer anyone gets back. Give the city after a comma -- "
    "\"Market Street, San Francisco\" -- or pass the `city` parameter."
)

_ADDRESS_NO_STREET_NOTE = (
    "no street to search for. Pass a street name, either as the part before the "
    "comma (\"1600 Amphitheatre Parkway, Mountain View\") or as the `street` "
    "parameter."
)


def _address_unresolved_anchor_note(
    city: str, anchor: dict | None, rejected: list[dict] | None = None
) -> str:
    if anchor is None:
        return (
            f"\"{city}\" did not resolve to any place, so there was no extent to "
            f"bound an address scan by and none was run. Check the spelling, or try "
            f"geocode(\"{city}\") to see what the name does match."
        )
    note = (
        f"\"{city}\" resolved to {anchor['name']}{_country_suffix(anchor)}, but Overture "
        f"carries no boundary extent for it -- only a point -- so there is no city-sized "
        f"box to scan addresses inside, and guessing one would return confidently wrong "
        f"doorways. Try a larger containing place (the city rather than the "
        f"neighborhood), or address_at({anchor['lat']}, {anchor['lon']}) for the "
        f"doorways around its centre."
    )
    if rejected:
        names = ", ".join(f"{r['name']}{_country_suffix(r)}" for r in rejected)
        note += (
            f" Same-named candidates with a boundary do exist ({names}), but in a "
            f"different country than the one this name resolved to, so scanning inside "
            f"one would answer about the wrong place entirely."
        )
    return note


def _country_suffix(row: dict) -> str:
    """" (United Kingdom, GB)" — whatever of the two a row actually carries."""
    label = (row.get("admin_context") or [None])[0]
    code = row.get("country")
    parts = [p for p in (label, code) if p]
    return f" ({', '.join(parts)})" if parts else ""


def _same_country(a: dict, b: dict) -> bool:
    """Are two geocode candidates in the same country?

    #229/R28: the runner-up anchor loop below used to take *any* candidate
    that had an extent, so "Baker Street, London" — where the UK London has
    no division_area row at all — walked past it onto London, Ontario and
    returned Canadian doorways under a UK anchor. A fallback anchor is only
    ever a fix for "geocode ranked the neighborhood above the city that
    contains it"; it is never a licence to cross a border.

    ISO code first (the authoritative field, present on every division row),
    falling back to the top of the admin chain for rows that carry a chain
    but no code. Missing both is *not* a match: a places-fallback row has
    neither, and "unknown country" must not be read as "same country".
    """
    ca, cb = a.get("country"), b.get("country")
    if ca and cb:
        return ca == cb
    ctx_a, ctx_b = a.get("admin_context") or [], b.get("admin_context") or []
    if ctx_a and ctx_b:
        return ctx_a[0] == ctx_b[0]
    return False


def geocode_address(
    query: str = "",
    limit: int = ADDRESS_DEFAULT_LIMIT,
    number: str | None = None,
    street: str | None = None,
    city: str | None = None,
) -> dict:
    """"Market Street, San Francisco" -> the address points on that street.

    The forward counterpart to address_at: a street-level *search*, where
    geocode answers at city/neighborhood granularity and never at a doorway.

    Four steps, in this order, and any of them can end the call honestly:

    1. Parse. The first comma splits a street half from a place half; a bare
       integer at either end of the street half is the house number ("1600
       Amphitheatre Parkway", "Hauptstraße 5"). `number`/`street`/`city`
       override the parse for a caller who already has the parts.
    2. Anchor. The place half goes through geocode(), and the winner's extent
       comes from #224's division bbox, then from a division_id-filtered
       division_area lookup. No extent -> empty plus a note, never a scan.
       A runner-up candidate may supply the extent when the winner has none
       (geocode ranks by name match, not by "which of these has a boundary"),
       but only one in the *same country* as the winner: same-named cities
       across a border are the normal case, not the exception.
    3. Scan the addresses theme inside that extent, through the same tile
       cache address_at reads, matching `street` against every USPS
       abbreviation/expansion of the query (Parkway<->Pkwy, W<->West, ...).
    4. Deduplicate to distinct number|street|postcode, nearest the anchor's
       own reference point first. The postcode is in the key because a city
       bbox is not a municipality (#229): without it, the 1 Main St of every
       town the box overlaps collapses into one row.

    Returns {"results": [{number, street, unit?, postcode?, country,
    distance_m, lat, lon}, ...], "anchor": {name, id, country,
    admin_context}} plus, when the answer is empty or clipped or the anchor
    is not the top-ranked candidate, a "note" saying which of the four steps
    ended it.
    Raises overture.UpstreamUnavailable / overture.SchemaDegraded, which
    server.py turns into structured errors.
    """
    limit = max(1, min(int(limit), ADDRESS_MAX_LIMIT))
    parsed_number, parsed_street, parsed_city = _parse_address_query(query or "")
    number = number if number is not None else parsed_number
    street = (street if street is not None else parsed_street).strip()
    city = (city if city is not None else parsed_city) or None
    if number is not None:
        number = str(number).strip() or None

    if not street:
        return {"results": [], "note": _ADDRESS_NO_STREET_NOTE}
    if not city:
        return {"results": [], "note": _ADDRESS_NO_ANCHOR_NOTE}

    local_table = _local_divisions_table()
    candidates = [r for r in geocode_detailed(city, limit=3, include_country=True)["results"]
                  if r["id"]]
    top = candidates[0] if candidates else None
    anchor = top
    bbox = _anchor_bbox(anchor["id"], local_table) if anchor else None
    notes: list[str] = []
    rejected: list[dict] = []
    if bbox is None and top is not None:
        # A neighborhood or a place row can lose to its own containing city
        # here: geocode ranks by name match, not by "which of these has a
        # boundary". Try the runners-up before declaring no extent -- but
        # only the ones in the *same country* as the top candidate (#229,
        # R28): every division name worth searching for is shared across
        # borders, and a fallback that crosses one turns "no extent for the
        # London you meant" into confidently wrong doorways in Ontario.
        for row in candidates[1:]:
            candidate_bbox = _anchor_bbox(row["id"], local_table)
            if candidate_bbox is None:
                continue
            if not _same_country(row, top):
                rejected.append(row)
                continue
            anchor, bbox = row, candidate_bbox
            notes.append(
                f"\"{city}\" resolved to {top['name']}{_country_suffix(top)}, which "
                f"Overture carries no boundary extent for, so the scan ran inside "
                f"{anchor['name']}{_country_suffix(anchor)} -- the next candidate of "
                f"that name in the same country."
            )
            break
    if bbox is None:
        return {"results": [], "note": _address_unresolved_anchor_note(city, top, rejected)}

    origin = (anchor["lat"], anchor["lon"])
    patterns = _street_variants(street)
    rows, distinct_in_range, matched_rows = _scan_addresses_in_bbox(
        bbox, origin, patterns, number, limit, locality=anchor["name"]
    )
    payload: dict = {
        "results": [_address_row(r) for r in rows],
        # country/admin_context always, never conditionally: the anchor is
        # the one thing that decides *which* Baker Street this answers
        # about, and a bare "London" is not enough for a caller to tell.
        "anchor": {
            "name": anchor["name"],
            "id": anchor["id"],
            "country": anchor.get("country"),
            "admin_context": anchor.get("admin_context") or [],
        },
    }
    if not rows:
        notes.append(_address_empty_note(origin, street))
    elif distinct_in_range > len(rows):
        payload["truncated"] = True
        payload["distinct_in_range"] = distinct_in_range
        notes.append(
            f"showing the {len(rows)} nearest of {distinct_in_range} distinct "
            f"addresses matching \"{street}\" in {anchor['name']} (deduplicated from "
            f"{matched_rows} raw address points). Add a house number to land on one "
            f"doorway."
        )
    if notes:
        payload["note"] = " ".join(notes)
    if not rows:
        return payload
    degraded = addresses.degraded_fields()
    if degraded:
        payload["degraded_fields"] = degraded
    return payload
