"""geocode: forward and reverse geocoding on Overture data (#10), no Nominatim.

This package was split from the former single-module geocode.py. The public
surface is unchanged: every name is still importable as
``placeroot.geocode.<name>``. The design notes (why the divisions table is
materialised, ranking rules, the #-numbered decisions) live in
docs/GEOCODE-INTERNALS.md.

Submodules (leading underscore; import the package, not these):

- _ranking: name normalisation, match tiers, result ranking, country/region tables
- _index: materialised Overture name tables and their background builds
- _qualifiers: "City, ST" / "City, Country" query parsing
- _session: resolve LRU, last-good city memory, POI aliases, city hints
- _variants: name-variant generators and match SQL helpers
- _divisions: division name search (literal, variant, fuzzy)
- _anchors: anchor resolution and the places fallback
- _postcode: postcode-shaped queries
- _detailed: geocode(), geocode_batch(), geocode_detailed()
- _places: named-place scan helpers
- _resolve: resolve_place()
- _reverse: reverse_geocode() and its lookups
- _named_places: resolve_area(), resolve_named_place(), typo tier, comma-qualified names
- _addresses: geocode_address() and street-level search
- _intersections: geocode_intersection() and "A & B, City" parsing

Cross-submodule calls go through the package object (``_pkg.name``) at call
time, so ``monkeypatch.setattr(placeroot.geocode, name, fake)`` takes effect
for every caller, exactly as it did when this was one module."""

import contextlib  # noqa: F401
import contextvars  # noqa: F401
import inspect  # noqa: F401
import json  # noqa: F401
import logging  # noqa: F401
import math  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys
import tempfile  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import unicodedata  # noqa: F401
from collections import OrderedDict  # noqa: F401
from collections.abc import Callable  # noqa: F401
from concurrent.futures import ThreadPoolExecutor  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from functools import lru_cache  # noqa: F401
from importlib import resources  # noqa: F401
from pathlib import Path  # noqa: F401

import duckdb  # noqa: F401

from placeroot import (
    addresses,  # noqa: F401
    cache,  # noqa: F401
    categories,  # noqa: F401
    db,  # noqa: F401
    geo,  # noqa: F401
    home_region,  # noqa: F401
    manifest,  # noqa: F401
    overture,  # noqa: F401
    progress,  # noqa: F401
    release,  # noqa: F401
    routing,  # noqa: F401
    session,  # noqa: F401
    trace,  # noqa: F401
)
from placeroot.errors import (
    AmbiguousArea,  # noqa: F401
    AmbiguousPlace,  # noqa: F401
    AnchoredNotFound,  # noqa: F401
)
from placeroot.geocode._addresses import (
    _ADDRESS_NEIGHBOR_FETCH_LIMIT,  # noqa: F401
    _ADDRESS_NO_ANCHOR_NOTE,  # noqa: F401
    _ADDRESS_NO_STREET_NOTE,  # noqa: F401
    _ADDRESS_SELECT_COLUMNS,  # noqa: F401
    _AREA_BBOX_CACHE,  # noqa: F401
    _HOUSE_NUMBER_RE,  # noqa: F401
    _INTERSECTION_NO_ANCHOR_NOTE,  # noqa: F401
    _INTERSECTION_SYMBOL_SPLIT_RE,  # noqa: F401
    _INTERSECTION_WORD_SPLIT_RE,  # noqa: F401
    _LEADING_INT_RE,  # noqa: F401
    _LEADING_STREET_TYPE_PAIRS,  # noqa: F401
    _LEADING_STREET_TYPES,  # noqa: F401
    _MAX_ANCHOR_SPAN_DEG,  # noqa: F401
    _STREET_NAME_PARTICLES,  # noqa: F401
    _STREET_TYPE_WORDS,  # noqa: F401
    _STREET_VARIANT_CAP,  # noqa: F401
    ADDRESS_DEFAULT_LIMIT,  # noqa: F401
    ADDRESS_MAX_LIMIT,  # noqa: F401
    _address_empty_note,  # noqa: F401
    _address_nearest_number_note,  # noqa: F401
    _address_row,  # noqa: F401
    _address_unresolved_anchor_note,  # noqa: F401
    _anchor_bbox,  # noqa: F401
    _anchor_too_broad,  # noqa: F401
    _bbox_span_label,  # noqa: F401
    _bracket_numbers,  # noqa: F401
    _country_suffix,  # noqa: F401
    _division_area_bbox,  # noqa: F401
    _intersection_unresolved_anchor_note,  # noqa: F401
    _opens_with_street_type,  # noqa: F401
    _parse_address_query,  # noqa: F401
    _parse_leading_int,  # noqa: F401
    _resolve_city_anchor,  # noqa: F401
    _ResolvedAnchor,  # noqa: F401
    _same_country,  # noqa: F401
    _scan_addresses_in_bbox,  # noqa: F401
    _scan_street_neighbors_in_bbox,  # noqa: F401
    _split_house_number,  # noqa: F401
    _street_variants,  # noqa: F401
    _warm_division_area_bboxes,  # noqa: F401
    geocode_address,  # noqa: F401
)
from placeroot.geocode._anchors import (
    _ANCHOR_LONGER_MATCH_POP_RATIO,  # noqa: F401
    _ANCHOR_MEMO_MAX,  # noqa: F401
    _anchor_contender_better,  # noqa: F401
    _anchor_is_weak,  # noqa: F401
    _anchor_memo,  # noqa: F401
    _anchor_memo_key,  # noqa: F401
    _anchor_memo_lock,  # noqa: F401
    _derive_split_anchors,  # noqa: F401
    _fallback_anchor,  # noqa: F401
    _fallback_anchor_candidates,  # noqa: F401
    _fallback_anchor_details,  # noqa: F401
    _names_a_feature,  # noqa: F401
    _pick_anchor_row,  # noqa: F401
    _query_places_fallback,  # noqa: F401
    _query_places_multi_anchor,  # noqa: F401
    _rank_anchor_contenders,  # noqa: F401
    _schedule_places_tiles_near,  # noqa: F401
)
from placeroot.geocode._detailed import (
    _REMOTE_GLOB_SCHEMES,  # noqa: F401
    _RESOLVE_OVERFETCH,  # noqa: F401
    _STOPWORD_RESIDUAL_NOTE,  # noqa: F401
    _UNANCHORED_NAME_SEARCH_NOTE,  # noqa: F401
    _UNBOUNDED_NAME_SEARCH_ENV,  # noqa: F401
    geocode,  # noqa: F401
    geocode_batch,  # noqa: F401
    geocode_detailed,  # noqa: F401
)
from placeroot.geocode._divisions import (
    _FOLDED_NAME_SQL,  # noqa: F401
    _FUZZY_SIMILARITY_THRESHOLD,  # noqa: F401
    NearConstraint,  # noqa: F401
    _alt_rows_not_already_found,  # noqa: F401
    _fuzzy_correction_note,  # noqa: F401
    _has_like_metacharacter,  # noqa: F401
    _near_filter_sql,  # noqa: F401
    _query_alt_names,  # noqa: F401
    _query_divisions,  # noqa: F401
    _query_divisions_from_local,  # noqa: F401
    _query_divisions_from_upstream,  # noqa: F401
    _query_divisions_fuzzy,  # noqa: F401
)
from placeroot.geocode._index import (
    _ALT_BUILD_ATTEMPTED,  # noqa: F401
    _DIVISIONS_BBOX_CHECKED,  # noqa: F401
    _LANG_BUILD_ATTEMPTED,  # noqa: F401
    _UPGRADE_DELAY_S,  # noqa: F401
    _blocking_build_lock,  # noqa: F401
    _build_lock,  # noqa: F401
    _build_started,  # noqa: F401
    _bundled_index_path,  # noqa: F401
    _clear_table_derived_caches,  # noqa: F401
    _copy_and_publish,  # noqa: F401
    _division_bbox,  # noqa: F401
    _divisions_table_has_bbox,  # noqa: F401
    _is_bundled_table,  # noqa: F401
    _is_remote_glob,  # noqa: F401
    _lang_variants_for,  # noqa: F401
    _local_alt_names_table,  # noqa: F401
    _local_divisions_table,  # noqa: F401
    _local_divisions_table_path,  # noqa: F401
    _local_lang_names_table,  # noqa: F401
    _materialize_alt_names_table,  # noqa: F401
    _materialize_divisions_pass,  # noqa: F401
    _materialize_divisions_table,  # noqa: F401
    _materialize_lang_names_table,  # noqa: F401
    _publish_copied_parquet,  # noqa: F401
    _rebuild_once_for_bbox_columns,  # noqa: F401
    _region_population_lookup,  # noqa: F401
    _region_population_lookup_cached,  # noqa: F401
    _spawn_divisions_build,  # noqa: F401
    _spawn_divisions_upgrade,  # noqa: F401
    _stage1_sentinel,  # noqa: F401
    _try_materialize_alt_names_table,  # noqa: F401
    _try_materialize_lang_names_table,  # noqa: F401
    _unique_tmp_path,  # noqa: F401
    _upgrade_divisions_table,  # noqa: F401
    _upgrade_lock,  # noqa: F401
    _upgrade_started,  # noqa: F401
)
from placeroot.geocode._named_places import (
    _AREA_MAX_CANDIDATES,  # noqa: F401
    _AREA_RANK_EPSILON,  # noqa: F401
    _FUZZY_WHOLE_QUERY_FLOOR,  # noqa: F401
    _area_candidate,  # noqa: F401
    _fuzzy_name_covers_tokens,  # noqa: F401
    _fuzzy_row_is_too_weak,  # noqa: F401
    _has_extra_place_context,  # noqa: F401
    _inside_anchor,  # noqa: F401
    _named_candidate,  # noqa: F401
    _names_qualifier,  # noqa: F401
    _pick_named_place,  # noqa: F401
    _qualifier_texts,  # noqa: F401
    _resolve_inside_anchor,  # noqa: F401
    _resolve_place_leg,  # noqa: F401
    _resolve_qualifier_anchor,  # noqa: F401
    _row_lies_in_region,  # noqa: F401
    _split_qualifier,  # noqa: F401
    resolve_area,  # noqa: F401
    resolve_named_place,  # noqa: F401
)
from placeroot.geocode._places import (
    _ALIAS_PIN_RADIUS_M,  # noqa: F401
    _ALIAS_PIN_SCAN_LIMIT,  # noqa: F401
    _ANCHOR_LOOKUP_LIMIT,  # noqa: F401
    _ANCHORED_OVERFETCH,  # noqa: F401
    _CITY_HINT_SUBTYPES,  # noqa: F401
    _CONFIDENT_PLACE_LABEL,  # noqa: F401
    _MATCH_LABEL_RANK,  # noqa: F401
    _MATCH_TIER_LABELS,  # noqa: F401
    _MAX_RESOLVE_TOKENS,  # noqa: F401
    _PLACE_SCAN_WORKERS,  # noqa: F401
    _RESOLVE_PLACE_RADIUS_M,  # noqa: F401
    _STOPWORDS,  # noqa: F401
    _TYPE_SCAN_SUPPORT_RADIUS_M,  # noqa: F401
    _TYPE_WORD_CATEGORIES,  # noqa: F401
    _best_place_label,  # noqa: F401
    _division_match_label,  # noqa: F401
    _find_places_kwargs,  # noqa: F401
    _fuzzy_place_covers_query,  # noqa: F401
    _is_remote,  # noqa: F401
    _is_word_prefix,  # noqa: F401
    _jaro_winkler,  # noqa: F401
    _match_label,  # noqa: F401
    _nothing_but_generic,  # noqa: F401
    _nothing_but_stopwords,  # noqa: F401
    _pick_city_hint_row,  # noqa: F401
    _place_match_label,  # noqa: F401
    _run_place_scans,  # noqa: F401
    _significant_tokens,  # noqa: F401
    _skip_unanchored_places_scan,  # noqa: F401
    _type_scan_rows,  # noqa: F401
    _type_word_slugs,  # noqa: F401
    _unbounded_name_search_enabled,  # noqa: F401
)
from placeroot.geocode._postcode import (
    _LOCAL_DISTANCE_EXPR,  # noqa: F401
    _NL_POSTCODE,  # noqa: F401
    _POSTCODE_AGGREGATE_CACHE,  # noqa: F401
    _POSTCODE_AGGREGATE_CACHE_MAX,  # noqa: F401
    _POSTCODE_LOCALITY_COS_FLOOR,  # noqa: F401
    _POSTCODE_LOCALITY_MAX_M,  # noqa: F401
    _POSTCODE_LOCALITY_SUBTYPES,  # noqa: F401
    _POSTCODE_LOCALITY_WINDOW_DEG,  # noqa: F401
    _POSTCODE_MAX_COUNTRIES,  # noqa: F401
    _POSTCODE_PATTERNS,  # noqa: F401
    _POSTCODE_ZERO_COUNTRIES,  # noqa: F401
    _covering_division,  # noqa: F401
    _covering_division_from_local,  # noqa: F401
    _locality_lon_window,  # noqa: F401
    _postcode_cold_scan_sentence,  # noqa: F401
    _postcode_coverage_sentence,  # noqa: F401
    _postcode_display,  # noqa: F401
    _postcode_empty_note,  # noqa: F401
    _postcode_note,  # noqa: F401
    _postcode_results,  # noqa: F401
    _postcode_variants,  # noqa: F401
    _query_postcode_countries,  # noqa: F401
)
from placeroot.geocode._qualifiers import (
    _BARE_QUALIFIER_HEAD_ADJECTIVES,  # noqa: F401
    _bare_suffix_split_allowed,  # noqa: F401
    _country_degrade_note,  # noqa: F401
    _division_named_exactly,  # noqa: F401
    _division_named_exactly_cached,  # noqa: F401
    _parse_country_suffix,  # noqa: F401
    _parse_region_suffix,  # noqa: F401
    _resolve_country_code,  # noqa: F401
    _resolve_country_from_table,  # noqa: F401
    _resolve_region_from_table,  # noqa: F401
    _resolve_us_state,  # noqa: F401
    _split_region_suffix,  # noqa: F401
    _suffix_split_candidates,  # noqa: F401
    _unrecognized_comma_qualifier,  # noqa: F401
    _unrecognized_qualifier_note,  # noqa: F401
    normalize_country,  # noqa: F401
)
from placeroot.geocode._ranking import (
    _ALT_NAMES_TABLE_FILENAME,  # noqa: F401
    _ANCHOR_SPECIFIC_SHARE,  # noqa: F401
    _CONFIDENT_TIER,  # noqa: F401
    _COUNTRIES_BY_ALPHA3,  # noqa: F401
    _COUNTRIES_BY_NAME,  # noqa: F401
    _COUNTRY_ALIASES,  # noqa: F401
    _DEGENERATE_BBOX_SPAN_DEG,  # noqa: F401
    _DIVISIONS_BBOX_COLUMNS,  # noqa: F401
    _DIVISIONS_TABLE_FILENAME,  # noqa: F401
    _DIVISIONS_TABLE_SUBDIR,  # noqa: F401
    _GENERIC_PLACE_WORDS,  # noqa: F401
    _HOME_BIAS_SCORE_BONUS,  # noqa: F401
    _LANG_NAMES_TABLE_FILENAME,  # noqa: F401
    _MAX_ANCHOR_TOKENS,  # noqa: F401
    _NAME_PREFIX_WORDS,  # noqa: F401
    _NAMESAKE_LOCALITY_KEY,  # noqa: F401
    _NAMESAKE_LOCALITY_SHARE,  # noqa: F401
    _PLACES_FALLBACK_RADIUS_M,  # noqa: F401
    _STRONG_TIER,  # noqa: F401
    _SUBTYPE_WEIGHT,  # noqa: F401
    _TIER_PUNCT_RE,  # noqa: F401
    _UNFOLDED_LETTERS,  # noqa: F401
    _US_STATES_BY_NAME,  # noqa: F401
    COUNTRIES,  # noqa: F401
    DEFAULT_LIMIT,  # noqa: F401
    DIVISION_OVERFETCH,  # noqa: F401
    MAX_LIMIT,  # noqa: F401
    US_STATES,  # noqa: F401
    _admin_chain_context,  # noqa: F401
    _admin_context,  # noqa: F401
    _effective_tier,  # noqa: F401
    _flag_namesake_localities,  # noqa: F401
    _fold_alt_name,  # noqa: F401
    _fold_alt_name_sql,  # noqa: F401
    _fold_for_tier,  # noqa: F401
    _home_bias_flag,  # noqa: F401
    _kick_autowarm,  # noqa: F401
    _match_tier,  # noqa: F401
    _normalize_for_match,  # noqa: F401
    _rank_key,  # noqa: F401
    _rank_score,  # noqa: F401
    _strip_diacritics,  # noqa: F401
)
from placeroot.geocode._resolve import resolve_place  # noqa: F401
from placeroot.geocode._reverse import (
    _nearest_address,  # noqa: F401
    _nearest_division,  # noqa: F401
    reverse_geocode,  # noqa: F401
)
from placeroot.geocode._session import (
    _CITY_HINT_RADIUS_M,  # noqa: F401
    _LAST_GOOD_SESSIONS_MAX,  # noqa: F401
    _POI_ALIASES,  # noqa: F401
    _RESOLVE_LRU_MAX,  # noqa: F401
    _WELL_KNOWN_CITIES,  # noqa: F401
    _WELL_KNOWN_CITY_COORDS,  # noqa: F401
    _alias_anchor,  # noqa: F401
    _alias_names_for,  # noqa: F401
    _canonical_city,  # noqa: F401
    _extract_city_hint,  # noqa: F401
    _fold_query_key,  # noqa: F401
    _last_good,  # noqa: F401
    _last_good_by_session,  # noqa: F401
    _last_good_lock,  # noqa: F401
    _lookup_poi_alias,  # noqa: F401
    _poi_aliases,  # noqa: F401
    _query_is_poi_shaped,  # noqa: F401
    _remember_last_city,  # noqa: F401
    _resolve_cache_get,  # noqa: F401
    _resolve_cache_key,  # noqa: F401
    _resolve_cache_put,  # noqa: F401
    _resolve_lru,  # noqa: F401
    _resolve_lru_lock,  # noqa: F401
    _well_known_city_near,  # noqa: F401
    clear_resolve_session,  # noqa: F401
)
from placeroot.geocode._variants import (
    _ABBR_VARIANTS,  # noqa: F401
    _CARDINAL_VARIANTS,  # noqa: F401
    _ORDINAL_RE,  # noqa: F401
    _STREET_QUADRANT_VARIANTS,  # noqa: F401
    _STREET_SUFFIX_VARIANTS,  # noqa: F401
    _WORD_ORDINALS,  # noqa: F401
    _abbreviation_variant_queries,  # noqa: F401
    _match_tier_order_sql,  # noqa: F401
    _ordinal_suffix,  # noqa: F401
    _ordinal_variants,  # noqa: F401
    _token_variants,  # noqa: F401
)

_pkg = sys.modules[__name__]


logger = logging.getLogger(__name__)



def _looks_like_street(half: str, *, strict: bool) -> bool:
    """Is this half of a split query a street name and not a fragment?

    Loose (symbol separators): non-empty, has a letter, is not a bare number.
    Strict (word separators): additionally carries a street-type word or an
    ordinal token, so "Rock" (of "Rock and Roll ...") fails and the query
    falls through to today's single-street scan.
    """
    tokens = half.split()
    if not tokens or not any(ch.isalpha() for ch in half):
        return False
    if not strict:
        return True
    for tok in tokens:
        key = tok.strip(".").lower()
        if key in _pkg._STREET_TYPE_WORDS or _pkg._ORDINAL_RE.match(key) or key in _pkg._WORD_ORDINALS:  # noqa: E501
            return True
    return False



def _split_intersection(street: str) -> tuple[str, str] | None:
    """"5th Ave & 42nd St" -> ("5th Ave", "42nd St"); a plain street -> None.

    Exactly two halves, each street-looking (see _looks_like_street); any
    other shape — three pieces, an empty side, a half that is only a number
    — is not an intersection this parser will vouch for, and the caller
    scans the text as one street as it always has.
    """
    for pattern, strict in (
        (_pkg._INTERSECTION_SYMBOL_SPLIT_RE, False),
        (_pkg._INTERSECTION_WORD_SPLIT_RE, True),
    ):
        parts = [p.strip() for p in pattern.split(street)]
        if len(parts) != 2:
            continue
        if all(_looks_like_street(p, strict=strict) for p in parts):
            return parts[0], parts[1]
        return None
    return None



# --- #448: geocode_intersection --------------------------------------------

INTERSECTION_MAX_LIMIT = 5

# Divided roads and signalled junctions are several Overture connectors per
# physical corner (15-40 m apart); matched nodes closer than this to one
# already kept are the same crossing, not another one.
INTERSECTION_CLUSTER_M = 50.0

# Floor on the walk-graph extraction radius: a village whose boundary is a
# few hundred metres across still gets a graph big enough to hold its
# streets, and the cache tile is never smaller than a walk isochrone's.
INTERSECTION_MIN_RADIUS_M = 1000.0


# The trailing tokens an Overture street name may carry beyond what the
# caller typed and still be *the same street*: the cardinal/quadrant suffix
# DC, Atlanta, Calgary and Portland put on every name ("Pennsylvania Avenue
# NW" for a query of "Pennsylvania Avenue"). Nothing else — "Broadway
# Terrace" is a different street from "Broadway", "Park Ave Ext" from "Park
# Ave", "Main St Bridge" from "Main St" — and a lone "Main" must not sweep
# up Main St, Main Ave and Main Street North the way geocode_address's
# ILIKE prefix deliberately does over address rows: there a wide match is
# a longer list to pick from, here it is a wrong coordinate.
_STREET_DIRECTIONAL_SUFFIXES: frozenset[str] = frozenset({
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "north", "south", "east", "west",
    "northeast", "northwest", "southeast", "southwest",
})



def _query_has_directional(variants: set[str]) -> bool:
    """Did the caller's street carry a directional token ("East 42nd St",
    "Main St N")? _street_variants only respells the tokens the query has,
    so a directional in any variant means one in the query."""
    return any(
        tok in _STREET_DIRECTIONAL_SUFFIXES for v in variants for tok in v.split()
    )



def _matches_street(edge_name: str | None, variants: set[str]) -> bool:
    """Whether an edge's street name is one of the caller's street's spellings.

    `variants` is _street_variants' lowercase output for the query (USPS
    suffixes, cardinals, ordinals). Equality, or equality followed only by
    directional tokens (_STREET_DIRECTIONAL_SUFFIXES) — never an arbitrary
    longer name.

    #465: when the query carries no directional at all, a single *leading*
    directional on the map's name is dropped too, so "42nd St" reaches
    Manhattan's "East 42nd Street" and "West 42nd Street" (5th Avenue is
    the E/W divide; no segment is named plain "42nd Street" there). A query
    that does name a side keeps exact matching — "East 42nd St" never
    matches "West 42nd Street" — and only whole tokens strip, so "Main St"
    still does not match "Main Street North Extension".
    """
    if not edge_name:
        return False
    enl = " ".join(edge_name.lower().split())
    if enl in variants:
        return True
    for v in variants:
        if len(enl) > len(v) and enl.startswith(v) and enl[len(v)] == " ":
            rest = enl[len(v) + 1:].split()
            if all(tok in _STREET_DIRECTIONAL_SUFFIXES for tok in rest):
                return True
    tokens = enl.split()
    if (
        len(tokens) > 1
        and tokens[0] in _STREET_DIRECTIONAL_SUFFIXES
        and not _query_has_directional(variants)
    ):
        return " ".join(tokens[1:]) in variants
    return False



def geocode_intersection(
    street_a: str = "",
    street_b: str = "",
    city: str = "",
) -> dict:
    """"5th Avenue", "Main Street", "Portland" -> coordinate where they cross.

    Locates where two named streets intersect within a resolved city anchor.

    Four steps:
    1. Parse and validate street_a, street_b, and city. Reject self-intersections
       where both street names normalize to the same street.
    2. Resolve the city anchor extent (_resolve_city_anchor, shared with
       geocode_address). No extent or too broad returns an empty list plus a note.
    3. Build or retrieve the walk graph around the anchor center, sized to the
       city bbox and clamped to routing.WALK_MAX_RADIUS_M. The search covers
       the part of the bbox within that radius; when the city is larger the
       note says so, so the caller can pass a neighborhood as `city` instead.
    4. Find junction nodes (three or more neighbors — a degree-2 node is one
       road changing name, not two roads meeting) inside that extent with an
       incident edge matching street_a and a different incident edge matching
       street_b, through the normalized USPS variants plus a directional
       suffix the caller left off ("Pennsylvania Avenue" -> "Pennsylvania
       Avenue NW"). Cluster the connectors of a divided-road junction within
       INTERSECTION_CLUSTER_M, keeping the one nearest the city center.

    Returns {"results": [{"lat", "lon", "streets": [name_a, name_b]}, ...],
    "anchor": {"name", "id", "country", "admin_context"}, "note": ...}.
    `streets` carries the map's own spelling of the two streets (Overture
    names.primary of the matched edges, in street_a/street_b order), which is
    how a caller learns the "NW" or "Avenue" they left off. `anchor` is the
    place the search ran in, once, the same shape geocode_address returns.
    When multiple crossings exist, results are ordered nearest to the city
    center first and capped at INTERSECTION_MAX_LIMIT. If one or both streets
    do not resolve in the city, returns empty results and a note naming the
    unresolved street. When the extracted graph hit routing.MAX_GRAPH_SEGMENTS
    the answer carries "truncated": true and says so, since a street missing
    from a partial graph is not a street missing from the city.
    """
    street_a = (street_a or "").strip()
    street_b = (street_b or "").strip()
    city = (city or "").strip()

    if not city:
        return {"results": [], "note": _pkg._INTERSECTION_NO_ANCHOR_NOTE}
    if not street_a and not street_b:
        return {
            "results": [],
            "note": (
                "no streets to search for. Pass two street names and a city to "
                "find their intersection."
            ),
        }
    if not street_a:
        return {
            "results": [],
            "note": (
                f"no first street provided to cross with \"{street_b}\". Pass two street "
                "names and a city to find their intersection."
            ),
        }
    if not street_b:
        return {
            "results": [],
            "note": (
                f"no second street provided to cross with \"{street_a}\". Pass two street "
                "names and a city to find their intersection."
            ),
        }

    variants_a = {v.lower() for v in _pkg._street_variants(street_a)}
    variants_b = {v.lower() for v in _pkg._street_variants(street_b)}

    if (
        variants_a & variants_b
        or _pkg._matches_street(street_a, variants_b)
        or _pkg._matches_street(street_b, variants_a)
    ):
        return {
            "results": [],
            "note": (
                f"\"{street_a}\" and \"{street_b}\" refer to the same street. "
                "Pass two different street names to find their intersection."
            ),
        }

    resolved = _pkg._resolve_city_anchor(city, action_label="search")
    if resolved.bbox is None:
        return {
            "results": [],
            "note": _pkg._intersection_unresolved_anchor_note(
                city, resolved.top, resolved.rejected, resolved.too_broad
            ),
        }
    anchor = resolved.anchor
    bbox = resolved.bbox
    notes: list[str] = list(resolved.notes)

    center_lat = anchor["lat"]
    center_lon = anchor["lon"]
    min_lon, min_lat, max_lon, max_lat = bbox

    max_corner_dist = max(
        geo.haversine_m(center_lat, center_lon, lat, lon)
        for lat in (min_lat, max_lat)
        for lon in (min_lon, max_lon)
    )
    radius_m = max(
        min(max_corner_dist, routing.WALK_MAX_RADIUS_M), INTERSECTION_MIN_RADIUS_M
    )

    graph = routing._get_or_build_graph(
        center_lat,
        center_lon,
        radius_m,
        mode="walk",
        speed_m_s=None,
        want_shapes=False,
    )

    found_a = False
    found_b = False
    crossings: list[dict] = []
    # A graph has hundreds of thousands of edge visits and a few thousand
    # distinct street names: match each name once, then it is a dict lookup.
    name_hits: dict[str, tuple[bool, bool]] = {}

    for node_id, (nlat, nlon) in graph.coords.items():
        # The served graph can be larger than the city — a cached 5 km walk
        # graph centered elsewhere, or the GRAPH_CACHE_MARGIN-padded build —
        # and a crossing in the next town over is not an answer about this
        # one. Bound by the anchor's own extent, as geocode_address's scan is.
        if not (min_lat <= nlat <= max_lat and min_lon <= nlon <= max_lon):
            continue
        nbrs = graph._undirected_neighbors.get(node_id, set())
        edges_a: list[tuple[str, str]] = []
        edges_b: list[tuple[str, str]] = []
        for nbr in nbrs:
            edge_name = graph.name_between(node_id, nbr) or graph.name_between(nbr, node_id)
            if not edge_name:
                continue
            hit = name_hits.get(edge_name)
            if hit is None:
                hit = (
                    _pkg._matches_street(edge_name, variants_a),
                    _pkg._matches_street(edge_name, variants_b),
                )
                name_hits[edge_name] = hit
            if hit[0]:
                found_a = True
                edges_a.append((nbr, edge_name))
            if hit[1]:
                found_b = True
                edges_b.append((nbr, edge_name))

        # Two streets meeting is a junction: at least three ways leave it.
        # A degree-2 node with one matching edge on each side is a single
        # road changing its name (Main St becoming Broadway at the city
        # line), which is not where they cross.
        if len(nbrs) < 3 or not edges_a or not edges_b:
            continue
        matched_pair: tuple[str, str] | None = None
        for nbr_a, name_a in edges_a:
            for nbr_b, name_b in edges_b:
                if nbr_a != nbr_b:
                    matched_pair = (name_a, name_b)
                    break
            if matched_pair:
                break
        if matched_pair is None:
            continue
        dist_m = geo.haversine_m(center_lat, center_lon, nlat, nlon)
        # #465: a junction sitting on the rim of the extracted graph is still
        # a junction in this city -- 5th Avenue & 42nd Street lies 5,003 m
        # from New York's anchor point against a 5,000 m walk radius and was
        # dropped by 3 m. Allow one cluster width of slack past the radius;
        # the graph itself already bounds how far out a node can be.
        if dist_m > radius_m + INTERSECTION_CLUSTER_M:
            continue
        crossings.append({
            "lat": nlat,
            "lon": nlon,
            "streets": [matched_pair[0], matched_pair[1]],
            "_dist": dist_m,
        })

    crossings.sort(key=lambda c: c["_dist"])
    clustered: list[dict] = []
    for c in crossings:
        if not any(
            geo.haversine_m(c["lat"], c["lon"], acc["lat"], acc["lon"]) < INTERSECTION_CLUSTER_M
            for acc in clustered
        ):
            clustered.append(c)
            if len(clustered) >= _pkg.INTERSECTION_MAX_LIMIT:
                break

    results = [
        {k: v for k, v in c.items() if k != "_dist"}
        for c in clustered
    ]

    payload: dict = {
        "results": results,
        # Once, at the top, same shape as geocode_address: the anchor is the
        # one thing that decides *which* Portland this answers about, and
        # repeating it on every row adds bytes, not information.
        "anchor": {
            "name": anchor["name"],
            "id": anchor["id"],
            "country": anchor.get("country"),
            "admin_context": anchor.get("admin_context") or [],
        },
    }
    if not results:
        # A street absent from a graph that stopped at its segment cap is a
        # fact about the extraction, not the city; say "the extracted part"
        # rather than asserting the negative about the map.
        where = (
            f"the extracted part of {anchor['name']}" if graph.truncated else anchor["name"]
        )
        if not found_a and not found_b:
            notes.append(f"neither \"{street_a}\" nor \"{street_b}\" resolved in {where}")
        elif not found_a:
            notes.append(f"\"{street_a}\" did not resolve in {where}")
        elif not found_b:
            notes.append(f"\"{street_b}\" did not resolve in {where}")
        else:
            notes.append(f"\"{street_a}\" and \"{street_b}\" do not intersect in {where}")
        if max_corner_dist > routing.WALK_MAX_RADIUS_M:
            notes.append(
                f"(the search covered only the {routing.WALK_MAX_RADIUS_M / 1000.0:.1f} km "
                f"around the center of {anchor['name']}; pass a neighborhood or district "
                f"as `city` to search elsewhere in it)"
            )
    if graph.truncated:
        payload["truncated"] = True
        notes.append(
            f"the street graph around {anchor['name']} hit its "
            f"{routing.MAX_GRAPH_SEGMENTS:,}-segment cap, so this is a partial view of "
            f"the network and a street or crossing may be missing from it"
        )
    if notes:
        payload["note"] = " ".join(notes)
    return payload
