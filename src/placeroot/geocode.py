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
"""

import logging
import math
import re
import time
import unicodedata
from pathlib import Path

import duckdb

from placeroot import cache, overture, release
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


def _rank_key(row: dict, query: str, region_population: dict[str, int]):
    """Sort key: match tier, then (#47) population if known, else a
    documented proxy chain of subtype rank / hierarchy depth / the row's
    own region's population, then (#53) literal-over-variant, then id for
    full determinism. All ascending (smaller sorts first).

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
    tier_score = {3: 1.0, 2: 0.7, 1: 0.4}[tier]
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


def _materialize_divisions_table(path: Path, glob: str) -> None:
    """COPY just the columns geocode.py needs, for every type=division row, into
    a local parquet file at `path`. Raises UpstreamUnavailable/duckdb.Error on
    failure — callers treat that as "materialization failed" and fall back to
    direct upstream scans, same as a missing/incompatible schema.
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
        WHERE subtype = 'region' AND region IS NOT NULL AND name ILIKE $name
        LIMIT 1
    """
    try:
        with overture._conn_lock:
            row = overture.conn().execute(sql, {"name": candidate}).fetchone()
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
        f"CASE WHEN {name_expr} ILIKE $exact THEN 0 "
        f"WHEN {name_expr} ILIKE $prefix THEN 1 ELSE 2 END"
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
    params: dict = {"pattern": f"%{query}%", "exact": query, "prefix": f"{query}%"}
    if region_code:
        params["region_code"] = region_code
    sql = f"""
        SELECT id, name, subtype, country, region, lat, lon, admin_chain, population
        FROM read_parquet('{table_path}')
        WHERE {name_match_expr} ILIKE $pattern
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
    params: dict = {"pattern": f"%{query}%", "exact": query, "prefix": f"{query}%"}
    if region_code and (cols is None or "region" in cols):
        region_filter = "AND region = $region_code"
        params["region_code"] = region_code
    sql = f"""
        SELECT id, names.primary AS name, subtype, country, region,
               bbox.ymin AS lat, bbox.xmin AS lon, hierarchies, {population_expr}
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE {name_match_expr} ILIKE $pattern
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


def _query_divisions(
    query: str,
    region_code: str | None,
    local_table: str | None,
    fold_diacritics: bool = False,
) -> list[dict]:
    """fold_diacritics (#53): match strip_accents(name) against `query`
    (which the caller must already have run through _strip_diacritics) —
    the diacritic-folded half of the second-pass variant retry."""
    if local_table is not None:
        name_expr = "strip_accents(name)" if fold_diacritics else "name"
        return _query_divisions_from_local(local_table, query, region_code, name_expr)
    name_expr = "strip_accents(names.primary)" if fold_diacritics else "names.primary"
    return _query_divisions_from_upstream(query, region_code, name_expr)


def _fallback_anchor(
    search_query: str,
    divisions: list[dict],
    region_code: str | None,
    local_table: str | None,
) -> tuple[float, float, str] | None:
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
        return best["lat"], best["lon"], (base or search_query)
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
    filters = ["names.primary ILIKE $pattern"]
    params: dict = {"pattern": f"%{query}%"}
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
    """Free-text place name -> ranked candidates, from Overture divisions (and places fallback).

    Never more than `limit` results. Each result: {name, type, lat, lon, id
    (GERS), admin_context, rank_score}. Raises overture.UpstreamUnavailable
    if the remote scan fails after retries; the caller (server.py) turns
    that into a structured error like the other tools.

    Handles "City, ST" / "City, Region" suffixes (#46) and ranks same-tier
    ties by population/prominence (#47) — see the module docstring.
    """
    query = query.strip()
    limit = max(1, min(limit, MAX_LIMIT))
    if not query:
        return []

    local_table = _local_divisions_table()
    base_query, region_code, _region_name = _parse_region_suffix(query, local_table)
    search_query = base_query if region_code else query

    divisions = _query_divisions(search_query, region_code, local_table)
    if region_code and not divisions:
        # Recognized a region suffix, but nothing in this dataset matches
        # inside it — degrade to an unconstrained search of the original
        # query rather than returning empty for a query that would
        # otherwise have matched something.
        region_code = None
        search_query = query
        divisions = _query_divisions(search_query, None, local_table)

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
    best_literal_tier = max((_match_tier(c["name"], search_query) for c in divisions), default=0)
    literal_match_has_prominence = any(
        _match_tier(c["name"], search_query) == best_literal_tier
        and c.get("population") is not None
        for c in divisions
    )
    if best_literal_tier < 2 or not literal_match_has_prominence:
        seen_ids = {c["id"] for c in divisions}
        variant_rows: list[dict] = []
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
        stripped_query = _strip_diacritics(search_query)
        for row in _query_divisions(stripped_query, region_code, local_table, fold_diacritics=True):
            if row["id"] not in seen_ids:
                row["_variant"] = True
                row["_tier"] = _match_tier(row["name"], stripped_query)
                variant_rows.append(row)
                seen_ids.add(row["id"])
        divisions = divisions + variant_rows

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
    if len(candidates) < limit and not has_exact_division:
        # #83: bound the places scan to an anchor's vicinity whenever one
        # can be derived (a division match already in hand, or a trailing
        # location word in the query) instead of an unconstrained
        # worldwide scan — see _fallback_anchor/_query_places_fallback.
        anchor_hit = _fallback_anchor(search_query, divisions, region_code, local_table)
        anchor, name_query = (
            ((anchor_hit[0], anchor_hit[1]), anchor_hit[2]) if anchor_hit else (None, search_query)
        )
        seen_names = {(c["name"].lower()) for c in candidates}
        places = _query_places_fallback(name_query, anchor=anchor)
        places = [p for p in places if p["name"].lower() not in seen_names]
        places.sort(
            key=lambda r: (-_match_tier(r["name"], name_query), -r["_confidence"], r["id"])
        )
        candidates = candidates + places

    out = []
    for row in candidates[:limit]:
        out.append({
            "name": row["name"],
            "type": row["subtype"],
            "lat": row["lat"],
            "lon": row["lon"],
            "id": row["id"],
            "admin_context": row["admin_context"],
            "rank_score": _rank_score(row, search_query) if "_confidence" not in row else round(
                0.4 + row["_confidence"] * 0.3, 3
            ),
        })
    return out


# --- #22: GERS id resolution -----------------------------------------------

# resolve_place overfetches both sources before merging/ranking/trimming to
# `limit`, the same reasoning as DIVISION_OVERFETCH: a shallow per-source
# limit can drop the right candidate before the merged ranking ever sees it.
_RESOLVE_OVERFETCH = 10

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
    or the addresses theme is unreachable/missing (degrade, don't raise)."""
    glob = overture.upstream_glob(theme="addresses", type_="address")
    cols = overture.probe_schema(glob)
    if cols is not None and "street" not in cols:
        return None
    for radius_m in (200, 1000, 5000):
        bbox_filter, distance_filter, params, _bbox, _radius_m = overture.area_geometry(
            lat, lon, radius_m
        )
        sql = f"""
            SELECT street, number, postcode, bbox.ymin AS lat, bbox.xmin AS lon,
                   round({overture.DISTANCE_EXPR}, 1) AS distance_m
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND {distance_filter}
            ORDER BY distance_m
            LIMIT 1
        """
        try:
            with overture._conn_lock:
                row = overture.conn().execute(sql, params).fetchone()
        except duckdb.Error as e:
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

    divisions = [r for r in geocode(area, limit=_RESOLVE_OVERFETCH) if r["type"] != "place"]
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
