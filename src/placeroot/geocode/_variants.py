"""Name-variant generators (abbreviations, ordinals, street suffixes) and match SQL helpers."""

import re
import sys

_pkg = sys.modules["placeroot.geocode"]


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
# the data (verified against the live release), which is why this map is US-only.
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


# #229: the quadrant suffix, which is part of the street name in every
# city that has one -- Washington DC's "PENNSYLVANIA AVE NW" is a different
# street from "PENNSYLVANIA AVE SE", and Overture writes the abbreviated
# form. Kept out of _CARDINAL_VARIANTS because those are single letters
# whose expansion is only safe on a leading token, while a quadrant is
# unambiguous wherever it appears in a street field.
_STREET_QUADRANT_VARIANTS: dict[str, list[str]] = {
    "nw": ["Northwest"], "northwest": ["NW"],
    "ne": ["Northeast"], "northeast": ["NE"],
    "sw": ["Southwest"], "southwest": ["SW"],
    "se": ["Southeast"], "southeast": ["SE"],
}



def _token_variants(token: str, leading: bool, street: bool = False) -> list[str]:
    """Alternate spellings for one query token.

    `street` (#225) turns on the USPS suffix map and lifts the leading-token
    restriction on the cardinal directions: "N" is too ambiguous to expand in
    the middle of a division name, but a street name is exactly where "W 42nd
    St" vs "West 42nd Street" happens, and the token is bounded by a street
    field rather than by free text. Street tokens also fold ordinals both
    ways ("5th" <-> "5"): city address datasets disagree on the form —
    NYC's Overture rows spell Fifth Avenue "5 AVENUE" — and a query in
    either spelling has to reach data in the other (task #23: "350 5th
    Ave, New York" matched nothing while the doorway existed as
    "350 5 AVENUE").
    """
    key = token.strip(".").lower()
    variants = list(_ABBR_VARIANTS.get(key, []))
    if street:
        variants += _STREET_SUFFIX_VARIANTS.get(key, [])
        variants += _STREET_QUADRANT_VARIANTS.get(key, [])
        variants += _pkg._ordinal_variants(key)
    if leading or street:
        variants += _CARDINAL_VARIANTS.get(key, [])
    return variants



# "5th" / "22ND" — a number wearing an English ordinal suffix.
_ORDINAL_RE = re.compile(r"^(\d+)(st|nd|rd|th)$")


# Spelled-out ordinals through twelfth: "350 Fifth Ave" has to reach NYC's
# "5 AVENUE" too, and the famous streets are all low-numbered. Each maps to
# both the digit-ordinal and bare-digit forms directly (variants of one
# token are not themselves re-expanded, so "fifth" must produce "5" here
# rather than relying on a second fold of "5th").
_WORD_ORDINALS: dict[str, list[str]] = {
    "first": ["1st", "1"], "second": ["2nd", "2"], "third": ["3rd", "3"],
    "fourth": ["4th", "4"], "fifth": ["5th", "5"], "sixth": ["6th", "6"],
    "seventh": ["7th", "7"], "eighth": ["8th", "8"], "ninth": ["9th", "9"],
    "tenth": ["10th", "10"], "eleventh": ["11th", "11"],
    "twelfth": ["12th", "12"],
}



def _ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")



def _ordinal_variants(key: str) -> list[str]:
    """"5th" -> ["5"], "5" -> ["5th"], "fifth" -> ["5th", "5"], else [].
    key is lowercased."""
    m = _ORDINAL_RE.match(key)
    if m:
        return [m.group(1)]
    if key.isdigit():
        return [key + _ordinal_suffix(int(key))]
    return list(_WORD_ORDINALS.get(key, []))



def _abbreviation_variant_queries(query: str) -> list[str]:
    """query -> whole-query variants with one token swapped for a common
    abbreviation/expansion (#53) — "St. Louis" -> ["Saint Louis"], "North
    Hollywood" -> ["N. Hollywood", "N Hollywood"]. Deduplicated, excludes
    the original query itself (case-insensitively)."""
    tokens = query.split(" ")
    variants = []
    for i, tok in enumerate(tokens):
        for alt in _pkg._token_variants(tok, leading=(i == 0)):
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
