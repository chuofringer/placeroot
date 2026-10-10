"""Query qualifier parsing: "City, ST", "City, Country", country codes and notes."""

import sys as _sys
from functools import lru_cache

import duckdb

_pkg = _sys.modules["placeroot.geocode"]


# --- #46: "City, ST" / "City, Region" parsing ---------------------------


def _resolve_us_state(token: str) -> tuple[str, str] | None:
    """token (case-insensitive, abbreviation or full name) -> (full_name,
    "US-XX"), or None if it isn't a recognized US state/DC."""
    t = token.strip().rstrip(".")
    upper = t.upper()
    if upper in _pkg.US_STATES:
        return _pkg.US_STATES[upper], f"US-{upper}"
    abbr = _pkg._US_STATES_BY_NAME.get(t.lower())
    if abbr:
        return _pkg.US_STATES[abbr], f"US-{abbr}"
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
        with _pkg.overture._conn_lock:
            row = _pkg.overture.conn().execute(
                sql, {"name": _pkg.overture._like_escape(candidate)}
            ).fetchone()
    except duckdb.Error:
        return None
    if row is None:
        return None
    return row[0], row[1]



def _suffix_split_candidates(query: str) -> list[tuple[str, str, bool]]:
    """(base, candidate_suffix, bare) triples to try, comma-suffix preferred
    over a bare trailing token (a comma is a much stronger "this is a region
    qualifier" signal than the last word of a multi-word query). `bare` is
    True for the trailing-word reading, which the parsers gate through
    _bare_suffix_split_allowed."""
    candidates = []
    if "," in query:
        base, _, suffix = query.rpartition(",")
        if base.strip() and suffix.strip():
            candidates.append((base.strip(), suffix.strip(), False))
    parts = query.strip().rsplit(None, 1)
    if len(parts) == 2 and parts[0].strip():
        candidates.append((parts[0].strip(), parts[1].strip(), True))
    return candidates



def _split_region_suffix(query: str) -> list[tuple[str, str]]:
    """(base, candidate_suffix) pairs to try — _suffix_split_candidates
    without the bare flag, for callers that only want the shapes."""
    return [(base, suffix) for base, suffix, _bare in _suffix_split_candidates(query)]



# Words that only ever modify the word after them. A no-comma query whose
# head is nothing but these is one name, not a name plus a qualifier:
# "West Virginia" is a state, not West inside Virginia; "Hotel California"
# is not a hotel in California; "New Jersey" is not New on Jersey.
_BARE_QUALIFIER_HEAD_ADJECTIVES = frozenset({
    "new", "old", "west", "east", "north", "south", "western", "eastern",
    "northern", "southern", "upper", "lower", "great", "little", "port",
    "saint", "st", "san", "santa", "fort", "ft", "mount", "mt", "lake", "hotel",
})



def _division_named_exactly(name: str, local_table: str | None) -> bool:
    """Whether some division's primary name equals `name` (case-
    insensitive) in the local table — one indexed probe against the local
    parquet (the bundled stage-0 index when that is all there is), never
    an upstream scan. False without a local table, and on a failed read.
    """
    if not local_table or not name.strip():
        return False
    try:
        return _pkg._division_named_exactly_cached(name.strip().casefold(), local_table)
    except duckdb.Error:
        return False



@lru_cache(maxsize=512)
def _division_named_exactly_cached(folded_name: str, local_table: str) -> bool:
    sql = f"""
        SELECT 1 FROM read_parquet('{local_table}')
        WHERE name ILIKE $exact ESCAPE '\\'
        LIMIT 1
    """
    with _pkg.overture._conn_lock:
        row = _pkg.overture.conn().execute(
            sql, {"exact": _pkg.overture._like_escape(folded_name)}
        ).fetchone()
    return row is not None



def _bare_suffix_split_allowed(base: str, query: str, local_table: str | None) -> bool:
    """Whether a *bare* (no comma) trailing word that resolved as a region
    or country may actually be read as a qualifier of `base`.

    Two gates, both of which "Paris, Texas" skips by carrying a comma:

    1. `base` has to be a plausible name of its own — at least one
       significant token that is not a bare modifier
       (_BARE_QUALIFIER_HEAD_ADJECTIVES). "West" is not a place
       Virginia contains, so "West Virginia" stays whole; "Portland" is
       a place, so "Portland Oregon" still splits.
    2. The whole query must not itself name a division exactly: a caller
       who typed a real division's full name meant that division, not a
       search for its first word inside its last. One local probe, and
       only reached once the suffix has resolved and gate 1 has passed.
    """
    head_tokens = [t.casefold().strip(".,'") for t in _pkg._significant_tokens(base)]
    if not any(t and t not in _BARE_QUALIFIER_HEAD_ADJECTIVES for t in head_tokens):
        return False
    return not _pkg._division_named_exactly(query, local_table)



def _parse_region_suffix(query: str, local_table: str | None) -> tuple[str, str | None, str | None]:
    """query -> (base_query, region_code, region_name). region_code/name are
    both None if no trailing token looks like a region — the caller then
    searches `query` unmodified, today's behavior.

    A bare trailing word (no comma) only counts once
    _bare_suffix_split_allowed agrees: "West Virginia" is not ("West",
    "US-VA").
    """
    for base, suffix, bare in _suffix_split_candidates(query):
        resolved = _resolve_us_state(suffix)
        if resolved is None and local_table:
            resolved = _resolve_region_from_table(suffix, local_table)
        if resolved and (not bare or _bare_suffix_split_allowed(base, query, local_table)):
            name, code = resolved
            return base, code, name
    return query, None, None



# --- #457: "City, Country" parsing ---------------------------------------


def _resolve_country_code(token: str) -> tuple[str, str] | None:
    """token (case-insensitive: ISO 3166-1 alpha-2, alpha-3, one of the
    {UK, U.K., U.S., U.S.A.} aliases, or the full ISO short name) ->
    (name, alpha-2), or None if it isn't a recognized country.

    Deliberately generic rather than a hardcoded shortlist: any 2-letter
    token that is a key of COUNTRIES is a hit, any 3-letter token that is a
    key of _COUNTRIES_BY_ALPHA3 is a hit, so all ~249 currently-assigned
    ISO codes are covered without listing them twice. "Georgia" (also a US
    state) still resolves as a country here on purpose — the region path
    is tried first by every caller in this module, so a query like
    "Atlanta, Georgia" never reaches this function at all, and "Tbilisi,
    Georgia" needs it to.
    """
    t = token.strip().rstrip(".")
    upper = t.upper()
    if upper in _pkg.COUNTRIES:
        return _pkg.COUNTRIES[upper][0], upper
    alias = _pkg._COUNTRY_ALIASES.get(upper)
    if alias:
        return _pkg.COUNTRIES[alias][0], alias
    a2 = _pkg._COUNTRIES_BY_ALPHA3.get(upper)
    if a2:
        return _pkg.COUNTRIES[a2][0], a2
    a2 = _pkg._COUNTRIES_BY_NAME.get(t.lower())
    if a2:
        return _pkg.COUNTRIES[a2][0], a2
    return None



def _resolve_country_from_table(
    candidate: str, local_table: str, alt_table: str | None = None
) -> tuple[str, str] | None:
    """candidate (e.g. "Deutschland") -> (name, alpha-2) if it exactly
    matches (case-insensitive) a country-subtype row's name.primary in the
    local divisions table, else — if `alt_table` is given — one of that
    row's #214 alternate spellings. Covers country names/exonyms outside
    the embedded ISO table (#457), the same role _resolve_region_from_table
    plays for regions (#46).
    """
    sql = f"""
        SELECT name, country FROM read_parquet('{local_table}')
        WHERE subtype = 'country' AND country IS NOT NULL AND name ILIKE $name ESCAPE '\\'
        LIMIT 1
    """
    try:
        with _pkg.overture._conn_lock:
            row = _pkg.overture.conn().execute(
                sql, {"name": _pkg.overture._like_escape(candidate)}
            ).fetchone()
    except duckdb.Error:
        row = None
    if row is not None:
        return row[0], row[1]
    if not alt_table:
        return None
    folded = _pkg._fold_alt_name(candidate)
    if not folded:
        return None
    sql2 = f"""
        SELECT d.name, d.country
        FROM read_parquet('{alt_table}') a
        JOIN read_parquet('{local_table}') d ON d.id = a.id
        WHERE d.subtype = 'country' AND d.country IS NOT NULL AND a.alt_name = $folded
        LIMIT 1
    """
    try:
        with _pkg.overture._conn_lock:
            row = _pkg.overture.conn().execute(sql2, {"folded": folded}).fetchone()
    except duckdb.Error:
        return None
    return (row[0], row[1]) if row is not None else None



def _parse_country_suffix(
    query: str, local_table: str | None, alt_table: str | None = None
) -> tuple[str, str | None, str | None]:
    """query -> (base_query, country_code, country_name), the #457
    counterpart to _parse_region_suffix (#46) for a trailing country
    instead of a region. Callers try the region parse first and only fall
    back to this one when it found nothing — see geocode_detailed.
    """
    for base, suffix, bare in _suffix_split_candidates(query):
        resolved = _resolve_country_code(suffix)
        if resolved is None and local_table:
            resolved = _resolve_country_from_table(suffix, local_table, alt_table)
        if resolved and (not bare or _bare_suffix_split_allowed(base, query, local_table)):
            name, code = resolved
            return base, code, name
    return query, None, None



def _unrecognized_comma_qualifier(query: str) -> tuple[str, str] | None:
    """(base, qualifier) when `query` has a comma-separated trailing token
    that the region and country parses have already failed on — #457's
    "never search the joined string" rule.

    Comma only, not the bare-trailing-word candidate _split_region_suffix
    also tries for the region/country parses: flagging every unresolved
    last WORD of an untagged multi-word query ("New York City") as an
    "unrecognized qualifier" would be far too aggressive and would add a
    note to queries that have nothing wrong with them. A comma is
    deliberate punctuation a caller adds specifically to separate a
    qualifier from the name, so treating an unresolved one as worth a note
    (rather than silently searching a string no division name will ever
    equal) is safe.
    """
    if "," not in query:
        return None
    base, _, suffix = query.rpartition(",")
    base, suffix = base.strip(), suffix.strip()
    if not base or not suffix:
        return None
    return base, suffix



def _unrecognized_qualifier_note(qualifier: str, base: str) -> str:
    return (
        f"qualifier '{qualifier}' not recognized as a region or country; "
        f"showing matches for '{base}'"
    )



def _country_degrade_note(base: str, country_code: str) -> str:
    return (
        f"no match for '{base}' in {country_code}; showing unconstrained matches for '{base}'"
    )



def normalize_country(token: str) -> str:
    """Validate/normalize an explicit `country=` parameter (#457): ISO
    3166-1 alpha-2, alpha-3, or a recognized alias/full name,
    case-insensitive -> uppercase alpha-2.

    Raises ValueError naming the expected form on anything else — server.py
    turns that into a structured {"error": "bad_request", ...}, the same
    convention overture.py's other parameter validation uses.
    """
    resolved = _resolve_country_code(token)
    if resolved is None:
        raise ValueError(
            f"country={token!r} is not a recognized ISO 3166-1 country code "
            "(alpha-2 like 'GB', alpha-3 like 'GBR', or a common alias like 'UK'/'USA')"
        )
    return resolved[1]
