geocode / reverse_geocode / resolve_place on Overture data (#10) — no Nominatim, no
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

--- #457: "City, Country" parsing ---

The country counterpart to #46, tried as a sibling parse (_parse_country_suffix)
right after the region parse has failed on the same trailing token — "Paris,
Texas" and "London, Ontario" are already region matches and never reach this
at all. Resolves the suffix against an embedded ISO 3166-1 table (COUNTRIES:
all 249 currently-assigned alpha-2 codes, paired with their alpha-3 and full
name, plus a small alias set for {UK, USA, U.S., U.S.A.}) first, then against
country-subtype rows of the local name table (and their #214 alternates) for
exonyms the embedded table doesn't carry ("Deutschland"). On a hit, divisions
candidates are constrained on each row's own `country` column, the same
hierarchy-membership filter #46 applies via `region`. An explicit `country=`
parameter on geocode()/geocode_batch()/resolve_place() is the same
constraint stated directly rather than parsed off the query, and composes
with (or, if it disagrees, raises ValueError naming) a suffix parsed off the
query itself. Two divergences from #46's degrade rules, both required by
the issue that added this: a recognized country that turns up zero
candidates degrades to an unconstrained search of the *base* name (not the
whole comma-joined string, which no bare Overture name could ever equal
anyway) with a note; and a comma-suffix recognized as neither a region nor
a country no longer falls through to searching the literal joined string at
all — it searches the base name with a note naming the unrecognized
qualifier, since "Cambridge, Narnia" searched as one string can never match
anything division names are bare.

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

--- #223: postcodes ---

"94110", "1011AB" and "SW1A 1AA" used to come back empty: division names are
names, and no postal_code division subtype exists in release 2026-07-22.0
(verified against all 9 subtypes). The postcode data that does exist lives on
the addresses theme -- 474M points carrying a `postcode` column -- so a query
that is *entirely* a postcode is answered from there instead, by one
aggregate: WHERE postcode IN (spellings) GROUP BY country, giving a point
count and a centroid per country, joined to the covering locality from the
already-materialized #43 divisions table (60ms). measured live: 11.4-13.4s
cold for the aggregate, and 94110 comes back genuinely ambiguous -- 29,956 US
points in the Mission in San Francisco, 3,491 SK, 3,310 FR -- which is the
honest answer, so all three are returned, ordered and scored by the evidence
behind them.

The aggregate is the one addresses read in this codebase that does NOT go
through cache.py's tile machinery: tiles are bboxes, and "which countries
carry this code" has no bbox to be sliced by -- a tile-shaped answer here
would be wrong, not merely partial. See _query_postcode_countries. It is
memoized per (dataset, code) for the life of the process instead
(_POSTCODE_AGGREGATE_CACHE), which is what keeps the scan a one-off rather
than a per-call cost -- including for the year-shaped queries the detector
cannot tell from four-digit postcodes.

Each row's locality is looked up in the row's own country. A postcode
centroid near a border is otherwise named by whatever sits nearest across it,
and "country: FR, admin_context: [..., Basel]" is a self-contradicting row.

The detector (_postcode_variants) is deliberately conservative: whole-query
match against a fixed list of country postcode shapes, so a query that could
be a name stays a name query. A postcode-shaped query that matches nothing
still falls through to the ordinary name search, and if that is also empty
the answer carries the coverage note -- because an empty postcode answer
usually means "outside the addresses theme" (GB) or "covered but carrying no
postcode values" (the 9 countries in _POSTCODE_ZERO_COUNTRIES), not "no such
code".

--- #224: bbox columns on the local divisions table (and why they are empty) ---

The street-level work (#225) wanted a city extent to bound an address scan: a
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

--- #329: city-hint ranking, last-resolve LRU, shared-table batch ---

Three name-path levers, no new remote APIs.

1. City-hint ranking. `resolve_place(city=)` (#271) already bounds a search
   to a city; agents often skip it and a one-word landmark then exact-matches
   a random division (Colosseum → Queensland, Ebisu → Shikoku). We now parse
   a trailing well-known city off the query ("Colosseo Roma", "notre dame
   paris") or look up a tiny alias list shipped next to the bundled stage-0
   index (`data/geocode-index/aliases.json`) and pass that as `city=` /
   `near_lat`/`near_lon`. When a hint is in play, division hits outside
   ~50 km of it are dropped — the hint bounds where the search looks; every
   returned row still comes from the data. A session's last successful city
   is reused as the implicit hint for the *next* POI-shaped resolve in the
   same process (not for a bare city-name query — "Paris" after a Palo Alto
   resolve must still be Paris).

2. Last-resolve LRU. Repeats of the same (normalized query, city hint) are
   answered from an in-process OrderedDict, not a second cache system. The
   tile cache in cache.py is untouched. `clear_resolve_session()` empties
   the LRU and the last-city memory (tests, a new conversation).

3. geocode_batch shares ONE name table. The server used to call geocode()
   N times, and a cold pair paid N table resolutions / N S3 scans. The
   library entry opens `_local_divisions_table()` once, looks every name
   up against it, and threads the same path through geocode_detailed.

--- #225: geocode_address, street-level forward search ---

geocode answers at city/neighborhood granularity, address_at answers "what is
at this coordinate"; nothing answered "where is 1600 Amphitheatre Parkway".
The data does: a live probe measured `number='1600' AND street ILIKE 'AMPHITHEATRE%'`
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
   (10.7s cold, measured, then memoized per process -- but only when it
   answers; a failed scan is not memoized, or one network blip would tell
   every later call in the process that a city has no boundary). No extent
   means no scan: an address search over a guessed box returns confidently
   wrong doorways, so the answer is an empty list plus a note naming the
   step that ended it. An extent too *wide* to be a city ends the call the
   same way (_MAX_ANCHOR_SPAN_DEG): "Main Street, Texas" would otherwise
   sweep 474M address points for the Main Street of every town in a state.
3. Scan (_scan_addresses_in_bbox), through addresses._from_source and so
   through the same #202 tile cache address_at and reverse_geocode read.
   Street matching runs every USPS abbreviation/expansion of the query
   (_STREET_SUFFIX_VARIANTS, fed through the same _token_variants machinery
   as #53's St./Saint pairs) because Overture's US rows are normalized to the
   abbreviated uppercase form -- "AMPHITHEATRE PKWY", "MARKET ST", both
   verified live. DE/NL names need no transformation at all, which is why the
   map is US-only.
4. Deduplicate, in SQL. MARKET ST in San Francisco is 2,980 address points
   collapsing to 900 distinct number|street pairs (measured live; 3,006 -> 915 on the
   live 2026-07-22.0 run of this tool): without the GROUP BY an
   undeduplicated top-5 is five spellings of one doorway. The distinct count
   rides back as `distinct_in_range` with the usual truncated note.

Ordering is by distance from the anchor division's *own* point, not from its
bbox centre. San Francisco's boundary reaches the Farallon Islands 45 km
offshore, so its bbox centre is in open water and a centre-ordered answer led
with the far west end of Market St; the division point is the city's label
point, which is what "in San Francisco" means.
