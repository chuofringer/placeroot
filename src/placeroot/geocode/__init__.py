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
            _pkg.logger.warning("addresses theme query failed, degrading to divisions-only: %s", e)
            return None
        if row:
            return {
                "street": row[0], "number": row[1], "postcode": row[2],
                "lat": round(row[3], 6), "lon": round(row[4], 6), "distance_m": row[5],
            }
    return None



def _nearest_division(lat: float, lon: float, country: str | None = None) -> dict | None:
    """Nearest locality-ish division to a point, scanning the divisions theme.

    `country` (#223) restricts the search to one country, for callers that
    already know which one the point is in and would contradict themselves by
    naming a place across the border. Left None by reverse_geocode, which
    knows only the coordinate and so wants the nearest division full stop.

    Also returns the row's own "country" (ISO 3166-1 alpha-2) and "region"
    (ISO 3166-2), when the active dataset carries those columns, so
    reverse_geocode can surface them without a second query (#446) — the
    same columns this query already reads for the `country` filter above.
    """
    glob = overture.upstream_glob(theme="divisions", type_="division")
    cols = overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return None
    has_country = cols is None or "country" in cols
    has_region = cols is None or "region" in cols
    country_filter = ""
    if country and has_country:
        country_filter = "AND country = $country"
    country_expr = "country" if has_country else "NULL"
    region_expr = "region" if has_region else "NULL"
    for radius_m in (2000, 20000, 100000):
        bbox_filter, distance_filter, params, _bbox, _radius_m = overture.area_geometry(
            lat, lon, radius_m
        )
        if country_filter:
            params = {**params, "country": country}
        sql = f"""
            SELECT names.primary AS name, subtype, hierarchies,
                   {country_expr} AS country, {region_expr} AS region,
                   round({overture.DISTANCE_EXPR}, 1) AS distance_m
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND {distance_filter}
              AND subtype IN ('locality', 'localadmin', 'neighborhood')
              {country_filter}
            ORDER BY distance_m
            LIMIT 1
        """
        try:
            with overture._conn_lock:
                row = overture.conn().execute(sql, params).fetchone()
        except duckdb.Error as e:
            _pkg.logger.warning("divisions theme query failed: %s", e)
            return None
        if row:
            chain = _pkg._admin_context(row[2], self_name=row[0])
            result = {"name": row[0], "subtype": row[1], "admin_context": [*chain, row[0]]}
            if row[3] is not None:
                result["country"] = row[3]
            if row[4] is not None:
                result["region"] = row[4]
            return result
    return None



def reverse_geocode(lat: float, lon: float) -> dict:
    """Nearest address (street/number/postcode) plus its containing division chain.

    Degrades to a divisions-only result — noting it via "source" and
    "note" — if the addresses theme is unreachable or missing, rather than
    failing the call: addresses is Overture's newest, least complete theme,
    so this is the expected degraded path, not a rare edge case.

    Also carries top-level "country" (ISO 3166-1 alpha-2) and "region"
    (ISO 3166-2), off the same nearest-division row admin_context is built
    from, when the active dataset has those columns — omitted, not
    null-filled, otherwise (#446).
    """
    address = _nearest_address(lat, lon)
    division = _pkg._nearest_division(lat, lon)
    admin_context = division["admin_context"] if division else []

    if address is not None:
        result = {
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
    else:
        result = {
            "address": None,
            "lat": lat,
            "lon": lon,
            "distance_m": None,
            "admin_context": admin_context,
            "source": "divisions_only",
            "note": (
                "no nearby address found (addresses theme unavailable, missing, or sparse here)"
            ),
        }
    if division:
        if "country" in division:
            result["country"] = division["country"]
        if "region" in division:
            result["region"] = division["region"]
    return result



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
        r for r in _pkg.geocode(area, limit=_pkg._RESOLVE_OVERFETCH)
        if r["type"] != "place" and r["id"]
    ]
    if not divisions:
        return None

    top = divisions[0]
    # Ambiguity is specifically "same name, no way to rank them" — a
    # differently-named division that merely scored close (a neighborhood
    # inside the city you asked for) is not ambiguity, so compare names too.
    top_name = _pkg._normalize_for_match(top["name"])
    tied = [
        d for d in divisions
        if _pkg._normalize_for_match(d["name"]) == top_name
        and abs(d["rank_score"] - top["rank_score"]) < _AREA_RANK_EPSILON
    ]
    if len(tied) > 1:
        raise AmbiguousArea(area, [_area_candidate(d) for d in tied[:_AREA_MAX_CANDIDATES]])

    _pkg._kick_autowarm(top)
    return _area_candidate(top)



def _area_candidate(row: dict) -> dict:
    """A division row, projected to just what an area choice needs."""
    return {
        "division_id": row["id"],
        "name": row["name"],
        "admin_context": row["admin_context"],
    }



def resolve_named_place(query: str) -> dict | None:
    """Free-text place name -> one place or division, or AmbiguousPlace.

    Same ranking as geocode() / resolve_area(): a prominence winner is a
    safe pick. Several same-name, same-score hits raise AmbiguousPlace
    so a compose tool cannot silently route the wrong city. Includes
    places as well as divisions — "Ferry Building" is not an area.

    When geocode matches no division at all, the places half of the answer
    comes from resolve_place rather than from geocode's own supplementary
    places scan — see the #429 block below for why that leg exists and why
    it is scoped to the no-division case.

    A comma-qualified name ("Le Marais, Paris") is read as name-plus-
    qualifier rather than as one opaque string — see the #427 block below
    for the split, the anchor bound, and how the tiers interact.

    Returns {name, lat, lon, id, type, admin_context} or None if nothing
    matched, plus a non-fatal "note" when a qualifier was present but did
    not resolve. Raises AmbiguousPlace, AnchoredNotFound (nothing inside a
    qualifier that did resolve), or overture.UpstreamUnavailable.
    """
    query = query.strip()
    if not query:
        return None

    head, qualifier = _split_qualifier(query)
    if head is not None:
        anchor = _pkg._resolve_qualifier_anchor(qualifier)
        if anchor is not None:
            return _resolve_inside_anchor(query, head, anchor)

    rows = [
        r for r in _pkg.geocode(query, limit=_pkg._RESOLVE_OVERFETCH)
        if r.get("lat") is not None and r.get("lon") is not None
        # #431: a fuzzy row that only ever proved itself against part of
        # what the caller typed is not an answer. See the block below.
        and not _pkg._fuzzy_row_is_too_weak(query, r)
    ]
    hit = None
    if not any(r["type"] != "place" for r in rows) and _pkg._has_extra_place_context(query):
        # #429: no division matched, so this is a places question — and the
        # places resolver is resolve_place, not geocode. See the block below.
        hit = _resolve_place_leg(query)
    if hit is None:
        if not rows:
            return None
        hit = _pick_named_place(query, rows)
    if head is not None:
        # The qualifier named nothing this release knows, so the whole
        # string was searched — today's behavior, and still the best
        # available answer. But the caller stated a location that was not
        # honored, and has to hear that rather than be handed a homonym as
        # if it had been.
        hit["note"] = f"{qualifier!r} did not resolve as a place or region; searched the full text"
    return hit



# --- #431: the typo tier refuses a match it only half earned ----------------
#
# The defect: resolve_named_place("Gare du Nord") answered "Garen Du", a
# hamlet in Côtes-d'Armor, and from_to("Louvre Museum" -> "Gare du Nord")
# then routed 430 km to it and said too_far. Not a threshold that was set
# too low — the row scored 0.975, comfortably over _FUZZY_SIMILARITY_
# THRESHOLD. It scored that against "Gare du". _parse_region_suffix reads
# the trailing "Nord" as Cameroon's CM-NO, the region-filtered fuzzy pass
# finds nothing there, and the retry that drops the filter (see the #215
# block in geocode()) then scores the shortened base_query against every
# name in the table. The suffix was misread, so a third of the query was
# never matched by anything, and the caller is handed the result as a fact.
#
# So the test is not "how close is this row to the string the pass happened
# to score" but "how close is it to what the caller actually typed". Two
# measures, both needed — neither separates the sets alone:
#
#   coverage: is every query token in the row's name, or typo-close to one
#             of its words (the #374 measure, same 0.92 bar)? A recognized
#             region suffix is excluded, but only when the row genuinely
#             lies in that region ("Berekley, CA" -> Berkeley, whose
#             admin_context names California; "Gare du Nord" -> Garen Du,
#             whose context names Bretagne, does not get the exemption).
#   whole:    jaro-winkler of the row's folded name against the folded
#             query, region suffix dropped only under that same exemption.
#
# Measured on release 2026-08-19.0 (fuzzy top-1 per query, "whole" column):
#
#   KEEP  Sao Paluo -> São Paulo          0.978  covered
#         New Yrok -> New York            0.975  covered
#         Rio de Janiero -> Rio de Janeiro 0.986 covered
#         Berekley, CA -> Berkeley        0.971  covered (CA exempt)
#         Cinncinati, OH -> Cincinnati    0.965  covered (OH exempt)
#         Sna Francisco -> San Francisco  0.977  UNCOVERED ("Sna"/"san" is
#                                                0.556 as a lone token)
#   REFUSE Gare du Nord -> Garen Du       0.883  UNCOVERED ("Nord")
#         Le Marais, Paris -> Le Mauvais Pas 0.921 UNCOVERED
#         Union Station Denver -> Union Station 0.930 UNCOVERED
#         Marina Bay Sands -> Marina Bay Estates 0.931 UNCOVERED
#         Copacabana Beach -> Copacabana Bajo 0.936 UNCOVERED
#         Brandenburg Gate -> Brandenburg  0.938 UNCOVERED
#         Central Park West -> Central Park Estates 0.948 UNCOVERED
#
# Coverage alone would refuse "Sna Francisco", a correction the #215 tests
# pin. Whole-string similarity alone cannot be set anywhere: the corrections
# run down to 0.965 and the homonyms up to 0.948, but "Kuala Lampur" ->
# Kuala Lumpur sits at 0.939, below two of the refusals. Requiring BOTH to
# fail separates every measured pair: refuse only an uncovered row whose
# whole-query similarity is under 0.96. The bar sits 0.012 above the
# strongest measured homonym and 0.017 below the weakest correction that
# needs the escape hatch. The cost is a real correction whose typo is in a
# token the name does not otherwise account for and that scores under 0.96
# overall ("Kuala Lampur"); geocode() still returns and labels that one, and
# resolve_named_place's caller gets not_found with its try hint pointing at
# resolve_place with city/near context. A wrong station is worse.
#
# Single-token queries are untouched: "Berekley" has nothing to be partly
# matched against, and the whole failure mode is a query whose remainder
# went unaccounted for.
#
# The refusal runs before the #429 places leg rather than after the winner
# is picked, because that leg is gated on geocode having matched no
# division — and the hamlet is a division. Dropping it does not only stop a
# wrong answer, it uncovers the search that can give a right one: with a
# Paris session in hand, "Gare du Nord" now resolves to the station itself
# (measured 216 m from the platforms), where before the hamlet stood in
# front of it. Live-pinned in test_live.py.
_FUZZY_WHOLE_QUERY_FLOOR = 0.96



def _fuzzy_row_is_too_weak(query: str, row: dict) -> bool:
    """Whether `row` is a fuzzy match that never accounted for all of `query`."""
    if row.get("matched_by") != "fuzzy":
        return False
    tokens = [t for t in query.replace(",", " ").split() if t]
    if len(tokens) < 2:
        return False
    scored_query = query
    base, _code, region_name = _pkg._parse_region_suffix(query, _pkg._local_divisions_table())
    if region_name and base != query and _row_lies_in_region(row, region_name):
        scored_query = base
        tokens = [t for t in base.replace(",", " ").split() if t]
    if _pkg._fuzzy_name_covers_tokens(row.get("name") or "", tokens):
        return False
    whole = _pkg._jaro_winkler(
        _pkg._normalize_for_match(row.get("name") or ""), _pkg._normalize_for_match(scored_query)
    )
    return whole < _pkg._FUZZY_WHOLE_QUERY_FLOOR



def _row_lies_in_region(row: dict, region_name: str) -> bool:
    """Whether `row`'s admin chain names the region a suffix was parsed as."""
    folded = _pkg._normalize_for_match(region_name)
    return any(_pkg._normalize_for_match(ctx) == folded for ctx in (row.get("admin_context") or []))



def _fuzzy_name_covers_tokens(name: str, tokens: list[str]) -> bool:
    """Whether every token of the query is accounted for by `name`.

    The #374 measure (_fuzzy_place_covers_query) applied to division names:
    a token counts as covered when the folded name contains it outright or
    one of the name's words is within _FUZZY_SIMILARITY_THRESHOLD of it.
    Separate from that function because it folds with _normalize_for_match,
    what the division tiers and _query_divisions_fuzzy use, rather than
    overture._fold_poi_name.
    """
    folded_name = _pkg._normalize_for_match(name)
    words = folded_name.split()
    for tok in tokens:
        folded_tok = _pkg._normalize_for_match(tok)
        if not folded_tok or folded_tok in folded_name:
            continue
        if any(_pkg._jaro_winkler(word, folded_tok) >= _pkg._FUZZY_SIMILARITY_THRESHOLD for word in words):  # noqa: E501
            continue
        return False
    return True



# --- #429: the places leg of an unqualified name ----------------------------
#
# The defect: resolve_named_place had no places search of its own. It read
# geocode(), whose places half is a *supplement* to the divisions search and
# is gated accordingly — stood down once an exact or fuzzy division match is
# in hand, and skipped outright against a remote dataset when no anchor can
# be derived from the query, because unanchored it is a substring scan of
# every place on Earth (#105, ~216s measured). "Louvre Museum" derives no
# anchor: "Museum" is a feature noun, and "Louvre" only prefix-matches the
# commune of Louvres, which the leading-token rule requires to be exact. So
# geocode returned nothing and every compose built on this resolver —
# from_to's ends, meeting_point's origins, optimize_route's stops,
# find_near's near — answered "no place matched" for a name the places
# theme carries six times over.
#
# resolve_place is the resolver that *does* have a places leg: its own
# reference derivation, the bundled landmark pins, the #329 last-good city,
# and a per-token bounded find_places sweep. Everything geocode's places
# half can reach, resolve_place's merge already includes (it folds
# geocode()'s own place rows in), so handing the places question to it is
# strictly more recall, never less.
#
# Scoped to "geocode found no division at all": when a division did match,
# the ranking that chose it stays untouched and the answer is byte-identical
# to before. This is a fallback, not a second opinion — a query geocode
# already answers is never re-litigated.
#
# Scoped again by _has_extra_place_context, and that one is a cost rule
# rather than a correctness rule. resolve_place re-runs geocode and then
# fans out one bounded find_places per significant token. When it would
# derive the very anchor geocode just used, that fan-out is the same search
# again at several times the price: "Nowhere At All Xyzzy" (a miss, cold
# cache) measured 12s before this leg and had not returned after five
# minutes with it. So the leg runs when resolve_place can bound the search
# somewhere geocode could not — a bundled landmark pin, or the session's
# last good city — which is the whole class this issue is about.


def _has_extra_place_context(query: str) -> bool:
    """Whether resolve_place knows a location for `query` that geocode did not.

    #469: a trailing well-known city counts. "Shibuya Station Tokyo" gives
    geocode one opaque string, and its places half returned a row named
    "Shibuya Station Tokyo. Japan" pinned 7 km east of the station — the
    top hit, so from_to routed 8.6 km for a 1.5 km walk. resolve_place
    splits the city off, anchors on its pin, searches the residual by
    category as well as by name, and grades against the residual. That is
    a bounded search (one local city lookup plus a few 20 km scans, ~2 s
    measured), never the unbounded miss the cost rule above guards
    against — that case has no recognizable city to split off.
    """
    _place_query, city, coords = _pkg._extract_city_hint(query)
    if coords is not None or city:
        return True
    last_city, _last_coords = _pkg._last_good()
    return bool(last_city) and _pkg._query_is_poi_shaped(query)



def _resolve_place_leg(query: str) -> dict | None:
    """resolve_place's top place candidate for `query`, or None.

    No ambiguity check: same reading as the anchored path in
    _resolve_inside_anchor. Several businesses sharing a name in one metro
    is the ordinary shape of the places theme rather than the "which city
    did you mean" ambiguity AmbiguousPlace exists to surface, and
    resolve_place has already ranked them by tier, distance and confidence.

    A degraded places dataset degrades this leg rather than the whole
    resolve: resolve_place raises SchemaDegraded where geocode does not, and
    a caller who used to get geocode's own answer (or an honest None) must
    not start getting an exception because a *supplementary* search failed.
    """
    try:
        hits = _pkg.resolve_place(query, limit=_pkg._RESOLVE_OVERFETCH)
    except overture.SchemaDegraded as e:
        _pkg.logger.info("resolve_named_place: places leg unavailable (%s); using geocode's rows", e)  # noqa: E501
        return None
    for hit in hits:
        if hit.get("kind") == "place":
            return {
                "name": hit["name"],
                "lat": hit["lat"],
                "lon": hit["lon"],
                "id": hit["id"],
                "type": "place",
            }
    return None



# --- #427: comma-qualified names ("Le Marais, Paris") -----------------------
#
# The defect: the whole string went to geocode() as one opaque name, no
# literal tier matched it, and the #215 typo tier then fuzzed across the
# comma — "Le Marais, Paris" came back as three villages named "Le Mauvais
# Pas", the qualifier the caller supplied thrown away entirely.
#
# Which comma: the FIRST one, with everything after it kept together as the
# qualifier. The issue proposed the last comma; the first is what
# geocode_address already does (its city anchor is the whole tail), and it
# reads "Grand Army Plaza, Brooklyn, NY" the way a caller means it — head
# "Grand Army Plaza", qualifier "Brooklyn, NY", which the anchor lookup
# then resolves as a name plus a region suffix on its own. A last-comma
# split would instead go looking for something named "Grand Army Plaza,
# Brooklyn".
#
# Which commas are ours: only the ones geocode() does not already
# understand. "Austin, TX" / "London, Ontario" are a name plus a region
# suffix, which _parse_region_suffix has recognized since #46 — it searches
# the name half alone, constrained to that region, so the comma is never
# crossed and there is nothing here to improve. _split_qualifier hands those
# back as unqualified, which is what keeps that whole family (and every
# typo correction inside it, "Berekley, CA") byte-identical.
#
# Tier interaction: for the qualifiers that are ours, the full ladder
# (exact -> alt -> typo) runs on the HEAD alone, inside the anchor bound —
# so no candidate can be reached by fuzzing a string that spans the comma.
# That is the "Le Mauvais Pas" class, impossible by construction rather
# than filtered out afterwards. The one path that still searches the whole
# string is the fallback below, taken only when the qualifier resolves to
# nothing at all, and it says so in a note.
#
# Anchor bound: a candidate is inside the anchor when the anchor's resolved
# name appears in the candidate's admin_context chain, or when it sits
# within _CITY_HINT_RADIUS_M of the anchor's point. Containment alone is
# too narrow (a place row carries no chain); radius alone is wrong for a
# region or country anchor, whose centroid can be hundreds of km from every
# real answer inside it.


def _split_qualifier(query: str) -> tuple[str | None, str | None]:
    """(head, qualifier) for a comma-qualified name, else (None, None).

    (None, None) for a query with no comma, and for one whose tail is a
    region suffix geocode() already resolves against — see the block above.
    """
    head, sep, tail = query.partition(",")
    head, tail = head.strip(), tail.strip()
    if not sep or not head or not tail:
        return None, None
    if _pkg._parse_region_suffix(query, _pkg._local_divisions_table())[1] is not None:
        return None, None
    return head, tail



def _pick_named_place(query: str, rows: list[dict]) -> dict:
    """The winner among ranked candidates, or AmbiguousPlace on a same-name tie."""
    top = rows[0]
    top_name = _pkg._normalize_for_match(top["name"])
    tied = [
        r for r in rows
        if _pkg._normalize_for_match(r["name"]) == top_name
        and abs(r.get("rank_score", 0) - top.get("rank_score", 0)) < _AREA_RANK_EPSILON
    ]
    if len(tied) > 1:
        raise AmbiguousPlace(query, [_named_candidate(r) for r in tied[:_AREA_MAX_CANDIDATES]])
    return _named_candidate(top)



def _resolve_qualifier_anchor(text: str) -> dict | None:
    """The city/region a qualifier names, or None if it names nothing.

    The name has to match exactly (folded, or through one of #214's
    alternates): a qualifier is the caller telling us where, so answering
    it with a prefix or fuzzy neighbor is the same class of mistake this
    whole path exists to stop — "Paris, France" anchored on *Franceville*
    before this rule, because geocode ranks a city above a country and
    "France" is a prefix of the one in Gabon.

    Place-kind hits are not anchors either: a qualifier says *where*, and a
    business that happens to share the word would bound the search on a
    storefront. When the qualifier is itself comma-separated and resolves
    as a whole ("Brooklyn, NY"), that is the anchor; only if it does not
    is its last segment tried on its own.
    """
    for candidate in _qualifier_texts(text):
        folded = _pkg._normalize_for_match(candidate)
        hits = [
            h for h in _pkg.geocode(candidate, limit=_pkg._ANCHOR_LOOKUP_LIMIT)
            if h.get("type") != "place"
            and h.get("lat") is not None
            and h.get("lon") is not None
            and _names_qualifier(h, folded)
        ]
        if hits:
            pin = _pkg._pick_city_hint_row(hits)
            return {
                "name": pin["name"],
                "lat": pin["lat"],
                "lon": pin["lon"],
                "folded_name": _pkg._normalize_for_match(pin["name"]),
            }
    return None



def _names_qualifier(row: dict, folded: str) -> bool:
    """Whether `row` is named exactly `folded`, canonically or through a #214 alternate."""
    names = {_pkg._normalize_for_match(n) for n in (row.get("name"), row.get("matched_name")) if n}
    return folded in names



def _qualifier_texts(text: str):
    yield text
    last = text.rsplit(",", 1)[-1].strip()
    if last and last != text:
        yield last



def _inside_anchor(row: dict, anchor: dict) -> bool:
    """Whether `row` is inside the qualifier, by admin chain or by distance.

    A row that carries an admin chain is judged on it alone, never on
    distance: "Le Marais, Paris" has a locality named Le Marais 45 km
    outside Paris and administratively in Essonne, well inside any
    city-scale radius, and taking it is the same wrong answer in a nearer
    village. A row with no chain to judge — a places-theme row from
    geocode's own fallback, or a country whose chain is just itself —
    falls back to the radius.
    """
    chain = {_pkg._normalize_for_match(n) for n in (row.get("admin_context") or []) if n}
    if chain:
        return anchor["folded_name"] in chain
    distance_m = geo.haversine_m(anchor["lat"], anchor["lon"], row["lat"], row["lon"])
    return distance_m <= _pkg._CITY_HINT_RADIUS_M



def _resolve_inside_anchor(query: str, head: str, anchor: dict) -> dict:
    """The full tier ladder on `head` alone, bounded by a resolved anchor."""
    rows = [
        r for r in _pkg.geocode(head, limit=_pkg._ANCHORED_OVERFETCH)
        if r.get("lat") is not None and r.get("lon") is not None and _inside_anchor(r, anchor)
    ]
    if rows:
        return _pick_named_place(query, rows)

    # No division inside the anchor. The neighborhood spellings this fix
    # exists for ("Le Marais") live in the places theme, not the divisions
    # one, so the anchored places search is the answer rather than a
    # consolation prize — bounded by the same anchor, never worldwide.
    anchored = _pkg.resolve_place(
        head, near_lat=anchor["lat"], near_lon=anchor["lon"], limit=_pkg._RESOLVE_OVERFETCH
    )
    places = [p for p in anchored if p.get("kind") == "place"]
    if not places:
        raise AnchoredNotFound(query, head, anchor["name"])
    top = places[0]
    return {
        "name": top["name"],
        "lat": top["lat"],
        "lon": top["lon"],
        "id": top["id"],
        "type": "place",
    }



def _named_candidate(row: dict) -> dict:
    """A geocode row, projected to what a named-place compose needs."""
    out = {
        "name": row["name"],
        "lat": row["lat"],
        "lon": row["lon"],
        "id": row.get("id"),
        "type": row.get("type"),
    }
    if row.get("admin_context"):
        out["admin_context"] = row["admin_context"]
    return out



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
# 24, not 16: the ordinal fold adds one alternate to numeric tokens, and at
# 16 a cardinal+ordinal+suffix+quadrant street ("West 42nd Street
# Northwest", 2x2x3x2 = 24 combos) truncated away its entire abbreviated
# "W ..." branch — the very form Overture stores.
_STREET_VARIANT_CAP = 24


# A house-number token: digits, optionally with one trailing letter. Overture's
# `number` is a string and real data carries "74B" and "12 bis"; "221B Baker
# Street" is the query shape that needs the letter (#229), while "12 bis"
# stays out of scope because it is two tokens and the second is a word. Unit
# numbers ("Apt 3", "#204") are deliberately out of scope too: they sit in a
# separate `unit` column, and guessing which trailing integer is which would
# silently search for the wrong doorway.
_HOUSE_NUMBER_RE = re.compile(r"^\d+[A-Za-z]?$")


# Street-type words that come *first* in the languages that number their
# streets rather than name them (#229). "Calle 8" is the name of a
# street in Miami, not house number 8 on a street called "Calle" — and the
# same holds for Avenida 9, Carrera 7, Via 20.
#
# English earns its entries here after all. The original list stopped
# at the Romance types on the grounds that an English street type leads only
# rarely ("Avenue 26" in Los Angeles) — true of "avenue", but numbered routes
# are the same grammar and are ordinary US address data: "ROUTE 66",
# "HIGHWAY 101", "US 1", "INTERSTATE 5" all name the street, and stripping
# the number searches for a street called "Route". Note this costs nothing
# for a real doorway on one of them, because the *leading*-number rule fires
# first: "1234 Highway 101" still splits to ("1234", "Highway 101").
_LEADING_STREET_TYPES = frozenset({
    "calle", "avenida", "avda", "av", "carrera", "cra", "calzada", "camino",
    "paseo", "diagonal", "transversal", "autopista", "rua", "rue", "via",
    "viale", "corso", "strada", "vicolo", "travessa",
    "route", "highway", "hwy", "interstate", "us",
})


# The same rule for the numbered-route names whose type word is two tokens
# ("County Road 12", "State Route 89", "Historic Route 66"), where the first
# token alone — "county", "state", "historic" — is far too ordinary to put in
# _LEADING_STREET_TYPES: it would swallow the house number of a real address
# on a street called "State St".
_LEADING_STREET_TYPE_PAIRS = frozenset({
    "county road", "county route", "county highway", "state route",
    "state road", "state highway", "historic route", "old highway",
    "farm road", "ranch road",
})


# Lowercase particles that make a leading integer part of the street's name
# rather than a house number: "8 de Octubre", "4 de Julio", "1º de Mayo".
_STREET_NAME_PARTICLES = frozenset({
    "de", "del", "di", "du", "des", "da", "do", "la", "le", "el", "of",
})


# Columns the address scan reads before grouping. postal_city is read but
# never returned: it is what "prefer the anchor's own municipality" sorts on
# (see _scan_addresses_in_bbox).
_ADDRESS_SELECT_COLUMNS = ("number", "street", "unit", "postcode", "country", "postal_city")


# One anchor bbox per (release, division_id) per process. The division_area
# lookup below is an id-filtered scan of a theme with no bbox to prune by --
# 10.7s cold, measured live on 2026-07-22.0 -- and a caller working through
# the addresses of one city pays it once instead of once per query.
_AREA_BBOX_CACHE: dict[tuple[str, str], tuple[float, float, float, float] | None] = {}


# The widest anchor extent, per axis, an address scan will run inside.
#
# _division_area_bbox rejects an extent for being too *small* (a point's
# rounding envelope, _DEGENERATE_BBOX_SPAN_DEG) but had no ceiling, and
# nothing in geocode_address restricts what a caller may name as the city.
# "Main Street, Texas" resolves a division_area 13 degrees across; that box
# blows past cache.MAX_TILES_PER_QUERY, so the tile cache declines it and the
# scan degrades to a direct, bbox-filtered read of the whole 474M-row
# addresses theme -- minutes of upstream work behind one tool call. This is
# the same class of guard as geo.MAX_QUERY_RADIUS_M, which clamps the radius
# every other theme's queries are bounded by.
#
# 3 degrees is chosen to sit above every real city and below every state.
# The widest genuine city extents are ~1-2 degrees once coastal islands are
# counted (live San Francisco reaches the Farallones, 0.9 degrees of
# longitude; Houston and Istanbul are of that order), while US states start
# around 4 and the ones anybody would name in this slot -- Texas, California
# -- are 10 to 13. Anything above the line is refused with a note naming a
# city as the fix, not scanned: an address search that takes minutes and
# returns the Main Streets of a whole state is not the answer that was asked
# for.
_MAX_ANCHOR_SPAN_DEG = 3.0



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
    choices = [[tok, *_pkg._token_variants(tok, leading=(i == 0), street=True)]
               for i, tok in enumerate(tokens)]
    if len(tokens) == 1:
        # A lone token becomes the whole prefix pattern (street ILIKE
        # '<variant>%'), and a bare-digit variant of "5th" would be
        # ILIKE '5%' — every street starting with a 5. Ordinal folds only
        # widen a single-token street toward more specific forms, never
        # toward a bare digit the original didn't have.
        choices[0] = [
            o for o in choices[0] if not (o.isdigit() and not tokens[0].isdigit())
        ]
    out: list[str] = []
    seen: set[str] = set()
    combos: list[list[str]] = [[]]
    for options in choices:
        combos = [c + [o] for c in combos for o in options]
        if len(combos) > _pkg._STREET_VARIANT_CAP:
            # Truncate the frontier rather than the finished list, so the cap
            # cannot drop the original spelling (always the first branch).
            combos = combos[:_pkg._STREET_VARIANT_CAP]
    for combo in combos:
        candidate = " ".join(combo)
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out[:_pkg._STREET_VARIANT_CAP]



def _split_house_number(text: str) -> tuple[str | None, str]:
    """"1600 Amphitheatre Pkwy" -> ("1600", "Amphitheatre Pkwy");
    "Hauptstraße 5" -> ("5", "Hauptstraße"); "Market Street" -> (None, ...).

    Leading or trailing only, and never when it is the *whole* string — a
    query of nothing but digits is a postcode-shaped thing for geocode() to
    read, not a house number with an empty street.

    Two locale rules keep a number that belongs to the *street name* out of
    the number slot (#229), because stripping it searches for a street
    that does not exist and returns an honest-looking empty:

    - a trailing number is not a house number when the street opens with one
      of the street-type words that lead in the numbered-street languages,
      whether that word is one token ("Calle 8" in Miami, "Route 66") or two
      ("County Road 12"). This is exactly the opposite convention from
      German, where the street type is a *suffix* glued to the name
      ("Hauptstraße 5"), which is why that case still splits;
    - a leading number is not a house number when the next token is a
      lowercase particle: "8 de Octubre" is a street, "8 Octubre" would be a
      doorway.

    The leading-number rule is checked first, so a real doorway on a numbered
    route is unaffected: "1234 Highway 101" splits to ("1234", "Highway 101")
    while a bare "Highway 101" does not split at all.

    A wrong split here is worse than no split: the number lands in an equality
    filter on `number`, so the scan silently searches a street nobody named.
    """
    tokens = text.split()
    if len(tokens) < 2:
        return None, text.strip()
    if _HOUSE_NUMBER_RE.match(tokens[0]) and tokens[1].lower() not in _STREET_NAME_PARTICLES:
        return tokens[0], " ".join(tokens[1:])
    if _HOUSE_NUMBER_RE.match(tokens[-1]) and not _opens_with_street_type(tokens):
        return tokens[-1], " ".join(tokens[:-1])
    return None, text.strip()



def _opens_with_street_type(tokens: list[str]) -> bool:
    """Does this street start with a street-type word that numbers what
    follows it — "Calle 8", "Route 66", "County Road 12"? See
    _LEADING_STREET_TYPES / _LEADING_STREET_TYPE_PAIRS."""
    if tokens[0].lower() in _LEADING_STREET_TYPES:
        return True
    return " ".join(t.lower() for t in tokens[:2]) in _LEADING_STREET_TYPE_PAIRS



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
    number, street = _pkg._split_house_number(parts[0])
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

    A *failed scan* is the one of those that is not memoized. The other
    three are facts about the dataset: they will answer the same way for as
    long as this release is pinned, so caching them is what makes the 10.7s
    lookup a once-per-city cost. A duckdb error is not a fact about the
    dataset — it is a network blip, a throttled read, a connection recycled
    mid-flight — and writing None for it would answer every later call for
    that city out of the cache, with no query and so no chance of recovery.
    The caller renders that None as "Overture carries no boundary extent for
    it", which for a city that plainly has one is a wrong answer the process
    would keep repeating until restart. Same reasoning as #230's vanished-
    table fallback in _query_divisions, which declines to report a missing
    local table as an upstream outage: a transient absence must not be
    recorded as a permanent one.
    """
    key = (release.resolve_release(), division_id)
    if key in _pkg._AREA_BBOX_CACHE:
        return _pkg._AREA_BBOX_CACHE[key]
    glob = overture.upstream_glob(theme="divisions", type_="division_area")
    missing = set(overture.missing_columns(glob, ["bbox", "division_id"]))
    if missing:
        _pkg._AREA_BBOX_CACHE[key] = None
        return None
    sql = f"""
        SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax)
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE division_id = $id
    """
    try:
        with overture._conn_lock:
            row = overture.conn().execute(sql, {"id": division_id}).fetchone()
    except duckdb.Error as e:
        _pkg.logger.warning(
            "division_area extent lookup failed for %s (not cached, so the next "
            "call retries): %s", division_id, e,
        )
        return None
    result: tuple[float, float, float, float] | None = None
    if row and not any(v is None for v in row):
        xmin, ymin, xmax, ymax = (float(v) for v in row)
        if (xmax - xmin) >= _pkg._DEGENERATE_BBOX_SPAN_DEG or (
            ymax - ymin
        ) >= _pkg._DEGENERATE_BBOX_SPAN_DEG:
            result = (xmin, ymin, xmax, ymax)
    _pkg._AREA_BBOX_CACHE[key] = result
    return result



def _warm_division_area_bboxes(division_ids: list[str]) -> None:
    """Fill _AREA_BBOX_CACHE for every id in one scan instead of one each.

    #268: the address path tries the top candidate's extent and then each
    same-country runner-up's, and every miss is its own ~10s division_area
    scan. "221B Baker Street, London" has a whole column of Londons in GB to
    walk, and measured 116.3s — nearly all of it the same scan run over and
    over for different ids. The predicate is the only thing that differed, so
    fold them into one IN-list and pay the scan once.

    Best-effort: a failure here leaves the cache empty and the per-id path
    runs exactly as before, including its deliberate no-memo-on-error rule.
    """
    release_id = release.resolve_release()
    wanted = [i for i in division_ids if i and (release_id, i) not in _pkg._AREA_BBOX_CACHE]
    if len(wanted) < 2:
        return
    glob = overture.upstream_glob(theme="divisions", type_="division_area")
    if set(overture.missing_columns(glob, ["bbox", "division_id"])):
        return
    # DuckDB positional parameters are 1-based; $0 is not a parameter.
    placeholders = ", ".join(f"${i}" for i in range(1, len(wanted) + 1))
    sql = f"""
        SELECT division_id,
               min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax)
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE division_id IN ({placeholders})
        GROUP BY division_id
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(
                sql, {str(i): v for i, v in enumerate(wanted, start=1)}
            ).fetchall()
    except duckdb.Error as e:
        _pkg.logger.warning("batched division_area extent lookup failed: %s", e)
        return
    found = {}
    for division_id, xmin, ymin, xmax, ymax in rows:
        if any(v is None for v in (xmin, ymin, xmax, ymax)):
            continue
        xmin, ymin, xmax, ymax = (float(v) for v in (xmin, ymin, xmax, ymax))
        if (xmax - xmin) >= _pkg._DEGENERATE_BBOX_SPAN_DEG or (
            ymax - ymin
        ) >= _pkg._DEGENERATE_BBOX_SPAN_DEG:
            found[division_id] = (xmin, ymin, xmax, ymax)
    for division_id in wanted:
        _pkg._AREA_BBOX_CACHE[(release_id, division_id)] = found.get(division_id)



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
    return _pkg._division_bbox(local_table, anchor_id) or _pkg._division_area_bbox(anchor_id)



def _anchor_too_broad(bbox: tuple[float, float, float, float]) -> bool:
    """Is this extent bigger than any city, i.e. too big to scan addresses
    inside? See _MAX_ANCHOR_SPAN_DEG."""
    xmin, ymin, xmax, ymax = bbox
    return (
        (xmax - xmin) > _MAX_ANCHOR_SPAN_DEG or (ymax - ymin) > _MAX_ANCHOR_SPAN_DEG
    )



def _bbox_span_label(bbox: tuple[float, float, float, float]) -> str:
    """"13.1° x 10.7°" — an extent's size, for a note that has to say why it
    was refused."""
    xmin, ymin, xmax, ymax = bbox
    return f"{xmax - xmin:.1f}° x {ymax - ymin:.1f}°"



def _scan_addresses_in_bbox(
    bbox: tuple[float, float, float, float],
    origin: tuple[float, float],
    street_patterns: list[str],
    number: str | None,
    limit: int,
    locality: str | None = None,
) -> tuple[list[tuple], int, int, bool]:
    """Deduplicated address rows inside `bbox`, nearest `origin` first.

    `origin` is the anchor division's own reference point, not the bbox
    centre. The two diverge more than they look: San Francisco's boundary
    includes the Farallon Islands 45 km out to sea, so its bbox centre sits
    in open water and "nearest first" off it ranks the westernmost end of
    Market St ahead of downtown. The division point is the city's label
    point, which is what a caller means by "in San Francisco".

    Returns (rows, distinct_in_range, matched_rows, number_filtered), where
    number_filtered is False when a `number` was asked for but the dataset has
    no such column to match it against — the caller turns that into a note,
    because "every doorway on the street" is a different answer from "this
    one address" and must not be handed back as if it were the latter.

    Dedup is not optional:
    Overture files one address point per source contribution, so MARKET ST in
    San Francisco is 2,980 rows collapsing to 900 distinct number|street
    pairs (measured live) — an undeduplicated top-5 is five spellings of the same
    doorway. Grouping happens in SQL so the wire never carries the 2,980.

    The group key is (number, street, postcode), not (number, street)
    (#229). A city bbox is not a municipality: Boston's box covers
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
        # Prefix, not equality (#229): US street names carry a trailing
        # quadrant or directional that a caller routinely leaves off, and
        # "Pennsylvania Avenue" must still find "PENNSYLVANIA AVE NW". The
        # variant map handles the caller who *does* type it; this handles
        # the one who doesn't. Honest because the group key keeps the
        # variants apart -- NW and SE come back as separate rows, with
        # distinct_in_range saying how many there were -- rather than
        # collapsing into one answer that hides which street it means.
        params[f"s{i}"] = overture._like_escape(pattern) + "%"
        street_sql.append(f"street ILIKE ${f's{i}'} ESCAPE '\\'")
    number_sql = ""
    number_filtered = number is None or "number" not in missing
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
        return [], 0, 0, number_filtered
    return rows, int(rows[0][-2]), int(rows[0][-1]), number_filtered



# The leading integer run of a house number: "12" from "12-14", "5" from
# "5A", nothing from "Lot 4" or a bare letter. This is the *only* numeric
# comparison the nearest-number fallback makes -- Overture's `number` is a
# free-text string, and this is the cheapest rule that is still honest about
# what it compares. See _bracket_numbers.
_LEADING_INT_RE = re.compile(r"^(\d+)")


# How many of the street's own points the nearest-number fallback pulls back
# from the tile cache before bracketing in Python. Generous relative to the
# 1-2 rows the answer actually needs: duplicate contributions (#229's dedup)
# and multiple postcodes on one number can each cost a slot, and the fetch is
# a local read against data the exact-match scan already warmed, not a new
# remote one -- see _scan_street_neighbors_in_bbox. Fetched as two same-sized
# halves, below and above the target, so a street with many points on one
# side of the miss can never starve the other side out of the fetch and
# silently downgrade a real bracket to a one-sided answer.
_ADDRESS_NEIGHBOR_FETCH_LIMIT = 20



def _parse_leading_int(number: str | None) -> int | None:
    """"12" -> 12, "12-14" -> 12, "5A" -> 5, "Lot 4" -> None, None -> None."""
    if not number:
        return None
    m = _LEADING_INT_RE.match(number.strip())
    return int(m.group(1)) if m else None



def _scan_street_neighbors_in_bbox(
    bbox: tuple[float, float, float, float],
    origin: tuple[float, float],
    street_patterns: list[str],
    target: int,
    locality: str | None = None,
) -> list[tuple]:
    """The street's own address points, ordered by numeric closeness to
    `target`, for the nearest-number fallback (#414).

    Same WHERE clause and CTE shape as _scan_addresses_in_bbox -- same tables,
    same bbox, same street patterns -- with the `number = $number` filter
    dropped, so this is a second local DuckDB query against the tile-cache
    parquet the exact-number scan already pulled down, not a second remote
    scan. Ordered by |leading_int(number) - target| (see _parse_leading_int)
    rather than distance to the anchor: the exact-number scan already proved
    the anchor-nearest doorways don't include this number, so a distance-first
    order would keep missing it on a long street. Rows whose number has no
    leading digit run are dropped in SQL -- they cannot bracket anything --
    and the fetch pulls two bounded halves (nearest below the target,
    nearest above) so a busy avenue can't turn this into an unbounded fetch
    and a lopsided street can't starve one side of the bracket out of it.
    """
    xmin, ymin, xmax, ymax = bbox
    lat, lon = origin
    glob = addresses._upstream_glob()
    missing = set(addresses._check_schema(glob))
    columns = ", ".join(addresses._column_expr(c, missing) for c in _ADDRESS_SELECT_COLUMNS)
    params: dict = {
        "lat": lat, "lon": lon, "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
        "target": float(target),
    }
    street_sql = []
    for i, pattern in enumerate(street_patterns):
        params[f"s{i}"] = overture._like_escape(pattern) + "%"
        street_sql.append(f"street ILIKE ${f's{i}'} ESCAPE '\\'")
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
              AND ({" OR ".join(street_sql)})
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
                   TRY_CAST(regexp_extract(trim(number), '^(\\d+)', 1) AS DOUBLE)
                       AS leading_int
            FROM matched GROUP BY number, street, postcode
        ),
        below AS (
            SELECT * FROM grouped WHERE leading_int <= $target
            ORDER BY locality_rank, $target - leading_int, distance_m
            LIMIT {_ADDRESS_NEIGHBOR_FETCH_LIMIT // 2}
        ),
        above AS (
            SELECT * FROM grouped WHERE leading_int > $target
            ORDER BY locality_rank, leading_int - $target, distance_m
            LIMIT {_ADDRESS_NEIGHBOR_FETCH_LIMIT // 2}
        )
        SELECT number, street, unit, unit_count, postcode, country, lat, lon, distance_m
        FROM (SELECT * FROM below UNION ALL SELECT * FROM above) sides
        ORDER BY locality_rank,
                 abs(leading_int - $target),
                 distance_m
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    return rows



def _bracket_numbers(
    rows: list[tuple], target: int, limit: int
) -> tuple[list[tuple], bool]:
    """The neighbor rows to answer a number-miss with, and whether they
    bracket the target (one below, one above) or sit only on one side.

    Only rows whose number has a leading digit run (_parse_leading_int) are
    numeric candidates; the rest cannot honestly be called "nearer" or
    "farther" than the target and are dropped here. Candidates are further
    restricted to one exact `street` value -- the one carried by the
    numerically-closest row: street matching upstream is a deliberate prefix
    match (PENNSYLVANIA AVE NW and ...SE both match "Pennsylvania Ave"), and
    a "bracket" straddling two different streets would put the true doorway
    between two points kilometres apart on neither of them. When both sides
    have a candidate, the nearest below and nearest above are the answer --
    a real bracket. When only one side does (the target is off one end of
    the street's known range), the 1-2 nearest rows on that side stand in
    instead, and the caller's note says so differently. `limit` bounds the
    result the same as every other geocode_address path; a bracket truncated
    to one row is reported as unbracketed (`bracketed` describes what the
    caller can actually see, not what the street theoretically holds).
    """
    parsed = [(_pkg._parse_leading_int(r[0]), r) for r in rows]
    parsed = [(n, r) for n, r in parsed if n is not None]
    if parsed:
        # rows arrive ordered by numeric closeness to the target, so the
        # first candidate's street is the street the miss most plausibly
        # sits on; neighbors from other prefix-matched street variants are
        # not honest bracket material.
        anchor_street = parsed[0][1][1]
        parsed = [(n, r) for n, r in parsed if r[1] == anchor_street]
    below = sorted((p for p in parsed if p[0] <= target), key=lambda p: -p[0])
    above = sorted((p for p in parsed if p[0] > target), key=lambda p: p[0])
    if below and above:
        chosen = [below[0], above[0]]
        bracketed = True
    else:
        chosen = (below or above)[:2]
        bracketed = False
    # `limit` truncates by nearness to the target -- a limit of 1 on a real
    # bracket keeps whichever of the two immediate neighbors is closer, not
    # an arbitrary "below wins" pick -- and the survivors are then read out
    # low-to-high, the order a bracket naturally comes in and the order the
    # note lists them in.
    chosen.sort(key=lambda p: abs(p[0] - target))
    chosen = chosen[: max(0, limit)]
    chosen.sort(key=lambda p: p[0])
    # Re-derive after truncation: a limit-1 answer holds one neighbor, and a
    # note claiming the doorway "may lie between them" over a single named
    # number would be nonsense.
    bracketed = bracketed and len(chosen) > 1
    return [p[1] for p in chosen], bracketed



def _address_nearest_number_note(
    number: str, street: str, chosen: list[tuple], bracketed: bool
) -> str:
    """"no address point for 32 W 26th St; nearest known numbers on that
    street: 30, 36 -- the true doorway may lie between them." -- honest about
    what is and isn't known: no coordinate is offered for the missing number
    (there is nothing to interpolate from but a straight line between two
    other doorways, which is exactly the invented precision #414 rejects),
    and the neighbors are named by number only, not by a distance to a point
    that does not exist.
    """
    names = ", ".join(r[0] for r in chosen)
    if bracketed:
        tail = "the true doorway may lie between them"
    elif len(chosen) > 1:
        tail = "the true doorway may lie beyond them, or the number may not exist"
    else:
        # One surviving neighbor: either a genuinely one-sided street or a
        # real bracket truncated by limit=1 -- direction-neutral either way.
        tail = "the true doorway may lie near it, or the number may not exist"
    return (
        f"no address point for {number} {street}; nearest known numbers on that "
        f"street: {names} -- {tail}."
    )



def _address_row(row: tuple) -> dict:
    """One grouped row -> the response shape. unit/postcode are dropped when
    null, the same padding-is-not-an-answer rule address_at applies.

    `unit_count` replaces `unit` when the doorway carries more than one
    (#229): "which of the 458 units" is a question this tool cannot
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



def _address_empty_note(
    origin: tuple[float, float], street: str, anchor: dict | None = None
) -> str:
    """Why a scan inside a resolved city extent found no such street.

    Coverage first, because it is the answer far more often than "no such
    street": the addresses theme is alpha and carries
    addresses.COVERED_COUNTRIES only, so a Manchester street search comes
    back empty whether or not the street exists.

    The anchor already names its country (geocode resolved the city, and
    country rides on every division row), so when one is in hand the note
    reuses it instead of re-deriving the same fact with address_at's
    ST_Contains polygon lookup — measured 18.9s cold against
    division_area's geometry column, on a path whose whole output is one
    explanatory sentence. Anchorless callers (address_at itself) still do
    the containment lookup, so both tools keep naming the same country by
    the same rule.
    """
    if anchor and anchor.get("country"):
        context = anchor.get("admin_context") or []
        country = addresses.Country(
            addresses.RESOLVED, anchor["country"], context[0] if context else None
        )
    else:
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
        f"spelled-out spellings were both tried, and the match is a prefix one, so a "
        f"quadrant or directional suffix in the data -- \"AVE NW\" -- would have been "
        f"found too). Coverage inside a covered country "
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


_INTERSECTION_NO_ANCHOR_NOTE = (
    "no city to search in. Pass `city` to locate the intersection within a "
    "specific city or town (e.g. \"5th Avenue\", \"Main Street\", \"Portland\")."
)


_ADDRESS_NO_STREET_NOTE = (
    "no street to search for. Pass a street name, either as the part before the "
    "comma (\"1600 Amphitheatre Parkway, Mountain View\") or as the `street` "
    "parameter."
)



def _address_unresolved_anchor_note(
    city: str,
    anchor: dict | None,
    rejected: list[dict] | None = None,
    too_broad: tuple[float, float, float, float] | None = None,
) -> str:
    if anchor is None:
        return (
            f"\"{city}\" did not resolve to any place, so there was no extent to "
            f"bound an address scan by and none was run. Check the spelling, or try "
            f"geocode(\"{city}\") to see what the name does match."
        )
    if too_broad is not None:
        # A different failure from "no extent", and it must not borrow that
        # wording: this place has a boundary, it is simply a state-sized one
        # (see _MAX_ANCHOR_SPAN_DEG).
        note = (
            f"\"{city}\" resolved to {anchor['name']}{_pkg._country_suffix(anchor)}, whose "
            f"boundary spans {_bbox_span_label(too_broad)} -- far larger than a city, "
            f"so no scan was run. An address search inside a box that size is a sweep "
            f"of Overture's 474M address points that takes minutes and comes back with "
            f"the same street name from every town in it. Name the city or town you "
            f"mean, and pass the region after it if the name is ambiguous "
            f"(\"Springfield, IL\")."
        )
    else:
        note = (
            f"\"{city}\" resolved to {anchor['name']}{_pkg._country_suffix(anchor)}, but Overture "
            f"carries no boundary extent for it -- only a point -- so there is no city-sized "
            f"box to scan addresses inside, and guessing one would return confidently wrong "
            f"doorways. Try a larger containing place (the city rather than the "
            f"neighborhood), or address_at({anchor['lat']}, {anchor['lon']}) for the "
            f"doorways around its centre."
        )
    if rejected:
        names = ", ".join(f"{r['name']}{_pkg._country_suffix(r)}" for r in rejected)
        note += (
            f" Same-named candidates in a different country do exist ({names}), but "
            f"scanning inside one would answer about the wrong place entirely."
        )
    return note



def _intersection_unresolved_anchor_note(
    city: str,
    anchor: dict | None,
    rejected: list[dict] | None = None,
    too_broad: tuple[float, float, float, float] | None = None,
) -> str:
    if anchor is None:
        return (
            f"\"{city}\" did not resolve to any place, so there was no city to "
            f"search intersections within. Check the spelling, or try "
            f"geocode(\"{city}\") to see what the name does match."
        )
    if too_broad is not None:
        note = (
            f"\"{city}\" resolved to {anchor['name']}{_pkg._country_suffix(anchor)}, whose "
            f"boundary spans {_bbox_span_label(too_broad)} -- far larger than a city. "
            f"Name the city or town you mean, and pass the region after it if the "
            f"name is ambiguous (\"Springfield, IL\")."
        )
    else:
        note = (
            f"\"{city}\" resolved to {anchor['name']}{_pkg._country_suffix(anchor)}, but Overture "
            f"carries no boundary extent for it -- only a point. Try a larger "
            f"containing place (the city rather than the neighborhood)."
        )
    if rejected:
        names = ", ".join(f"{r['name']}{_pkg._country_suffix(r)}" for r in rejected)
        note += (
            f" Same-named candidates in a different country do exist ({names}), but "
            f"searching inside one would answer about the wrong place entirely."
        )
    return note



def _country_suffix(row: dict, *, with_type: bool = False) -> str:
    """" (United Kingdom, GB)" — whatever of the two a row actually carries.

    `with_type` (#465) is for the one place two rows of the *same name and
    country* are named side by side — the runner-up note below — and spells
    out what tells them apart: the division type and the whole admin chain,
    most specific first: " (region; United States, US)" against
    " (locality; New York, United States, US)", or Springfield
    " (locality; Massachusetts, United States, US)" against
    " (locality; Illinois, United States, US)". "New York, NY" resolves to
    the *region* New York, too broad, and falls back to the *locality* New
    York; with the plain suffix both halves of that note read "New York
    (United States, US)" and the sentence looks like it re-picked the state
    (which is how #465 was filed).
    """
    chain = [p for p in (row.get("admin_context") or []) if p]
    code = row.get("country")
    if with_type:
        # "; " between type and place so the type is not read as one more
        # element of the admin chain.
        parts = [*reversed(chain), *([code] if code else [])]
        head = f"{row['type']}; " if row.get("type") else ""
        body = head + ", ".join(parts)
        return f" ({body})" if body else ""
    parts = [p for p in (chain[:1] + [code]) if p]
    return f" ({', '.join(parts)})" if parts else ""



def _same_country(a: dict, b: dict) -> bool:
    """Are two geocode candidates in the same country?

    #229: the runner-up anchor loop below used to take *any* candidate
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



@dataclass
class _ResolvedAnchor:
    anchor: dict | None
    bbox: tuple[float, float, float, float] | None
    notes: list[str]
    top: dict | None
    rejected: list[dict]
    too_broad: tuple[float, float, float, float] | None



def _resolve_city_anchor(
    city: str,
    *,
    action_label: str = "scan",
) -> _ResolvedAnchor:
    """Resolve the city anchor extent for geocode_address / geocode_intersection.

    Returns the resolved anchor and bounding box (or None if unresolved),
    along with any runner-up fallback notes or metadata for generating
    unresolved-anchor notes.
    """
    local_table = _pkg._local_divisions_table()
    candidates = [
        r for r in _pkg.geocode_detailed(city, limit=3, include_country=True)["results"]
        if r.get("id")
    ]
    top = candidates[0] if candidates else None
    if top is not None:
        _pkg._warm_division_area_bboxes(
            [top["id"]] + [r["id"] for r in candidates[1:] if _pkg._same_country(r, top)]
        )
    anchor = top
    bbox = _pkg._anchor_bbox(anchor["id"], local_table) if anchor else None
    notes: list[str] = []
    rejected: list[dict] = []
    too_broad: tuple[float, float, float, float] | None = None
    top_reason = "which Overture carries no boundary extent for"
    if bbox is not None and _pkg._anchor_too_broad(bbox):
        too_broad, bbox = bbox, None
        top_reason = f"whose boundary ({_bbox_span_label(too_broad)}) is far larger than a city"
    if bbox is None and top is not None:
        for row in candidates[1:]:
            if not _pkg._same_country(row, top):
                rejected.append(row)
                continue
            candidate_bbox = _pkg._anchor_bbox(row["id"], local_table)
            if candidate_bbox is None or _pkg._anchor_too_broad(candidate_bbox):
                continue
            anchor, bbox = row, candidate_bbox
            # #465: a namesake pair ("New York" the region -> "New York"
            # the locality) is only tellable apart by type; otherwise the
            # note names the same label twice.
            same_name = top["name"] == anchor["name"]
            notes.append(
                f"\"{city}\" resolved to {top['name']}"
                f"{_pkg._country_suffix(top, with_type=same_name)}, "
                f"{top_reason}, so the {action_label} ran inside "
                f"{anchor['name']}{_pkg._country_suffix(anchor, with_type=same_name)} -- "
                f"the next candidate of that name in the same country."
            )
            break

    return _pkg._ResolvedAnchor(
        anchor=anchor if bbox is not None else None,
        bbox=bbox,
        notes=notes,
        top=top,
        rejected=rejected,
        too_broad=too_broad,
    )



# --- #465: "A & B, City" is an intersection, not a street ------------------

# The separators that write a street crossing in one field. The symbols are
# unambiguous; "and"/"at" are ordinary English that also appears *inside*
# street names ("Rock and Roll Hall of Fame Blvd"), so they only split when
# both halves independently look like streets (_looks_like_street, strict).
_INTERSECTION_SYMBOL_SPLIT_RE = re.compile(r"\s*&\s*|\s+@\s+|\s+/\s+")

_INTERSECTION_WORD_SPLIT_RE = re.compile(r"\s+(?:and|at)\s+", re.IGNORECASE)


# What makes a half of "A and B" a street rather than half of one name: a
# street-type word (the USPS table geocode_address already matches through,
# plus the common types it has no abbreviation pair for and the numbered-
# route types) or an ordinal ("5th", "42nd", "Fifth").
_STREET_TYPE_WORDS: frozenset[str] = (
    frozenset(_STREET_SUFFIX_VARIANTS)
    | _LEADING_STREET_TYPES
    | frozenset({
        "way", "terrace", "ter", "circle", "cir", "square", "sq", "trail", "trl",
        "alley", "aly", "expressway", "expy", "freeway", "fwy", "turnpike",
        "tpke", "plaza", "plz", "broadway", "crescent", "cres", "loop", "path",
        "row", "walk", "esplanade", "promenade", "quay", "embankment",
    })
)



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
        if key in _STREET_TYPE_WORDS or _pkg._ORDINAL_RE.match(key) or key in _pkg._WORD_ORDINALS:
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
        (_INTERSECTION_SYMBOL_SPLIT_RE, False),
        (_INTERSECTION_WORD_SPLIT_RE, True),
    ):
        parts = [p.strip() for p in pattern.split(street)]
        if len(parts) != 2:
            continue
        if all(_looks_like_street(p, strict=strict) for p in parts):
            return parts[0], parts[1]
        return None
    return None



def geocode_address(
    query: str = "",
    limit: int = ADDRESS_DEFAULT_LIMIT,
    number: str | None = None,
    street: str | None = None,
    city: str | None = None,
) -> dict:
    """"Market Street, San Francisco" -> the address points on that street.

    #465: a street half written as a crossing — "5th Ave & 42nd St", "5th
    Ave and 42nd St", "5th Ave at 42nd St", "5th Ave / 42nd St" — with no
    house number is not a street name to scan for (nothing is addressed
    "5th Ave & 42nd St"); it is routed to geocode_intersection with the same
    city, and the answer is that tool's, marked "delegated_to":
    "geocode_intersection". "and"/"at" only split when both halves look
    like streets (a street-type word or an ordinal each), so "Rock and Roll
    Hall of Fame Blvd, Cleveland" still scans as one street.

    The forward counterpart to address_at: a street-level *search*, where
    geocode answers at city/neighborhood granularity and never at a doorway.

    Four steps, in this order, and any of them can end the call honestly:

    1. Parse. The first comma splits a street half from a place half; a bare
       integer at either end of the street half is the house number ("1600
       Amphitheatre Parkway", "Hauptstraße 5"). `number`/`street`/`city`
       override the parse for a caller who already has the parts.
    2. Anchor. The place half goes through geocode(), and the winner's extent
       comes from #224's division bbox, then from a division_id-filtered
       division_area lookup. No extent -> empty plus a note, never a scan;
       an extent wider than _MAX_ANCHOR_SPAN_DEG (a state or a country, not
       a city) is refused the same way, for the same reason -- the scan it
       would license is not an answer. A runner-up candidate may supply the
       extent when the winner has none (geocode ranks by name match, not by
       "which of these has a boundary"), but only one in the *same country*
       as the winner: same-named cities across a border are the normal case,
       not the exception.
    3. Scan the addresses theme inside that extent, through the same tile
       cache address_at reads, matching `street` against every USPS
       abbreviation/expansion of the query (Parkway<->Pkwy, W<->West,
       NW<->Northwest, ...) as a *prefix*, so a street written with a
       quadrant suffix is found by a query without one.
    4. Deduplicate to distinct number|street|postcode, nearest the anchor's
       own reference point first. The postcode is in the key because a city
       bbox is not a municipality (#229): without it, the 1 Main St of every
       town the box overlaps collapses into one row.

    A house number that lands on no address point is never interpolated
    (#414): Overture's addresses theme is points only, so a synthesized
    doorway between two real ones would be invented precision, not data.
    Instead the answer carries `match`, one of:
      - "exact" -- the number was asked for and found.
      - "nearest_number" -- the number was asked for, missed, and the street
        has other numbered points; `results` holds the nearest known number
        below and above it (or the 1-2 nearest, if the miss is off one end
        of the street's known range), as real rows with their own
        coordinates -- never the missing number's coordinates, which do not
        exist. A note names the miss and its neighbors. House numbers
        compare by their leading integer run ("12-14" -> 12, "5A" -> 5); a
        number with no leading digits (an alley name, a lot number) cannot
        be bracketed and falls to "street" instead.
      - "street" -- everything else: no number asked for, the dataset has
        no `number` column to filter on, or the street has no numbered
        point to bracket the miss with. This is today's plain street answer
        or empty-plus-note, unchanged, now labeled.
    The nearest-number fallback costs one extra local DuckDB query, run only
    on a miss, against the same bbox/street tiles the exact-number scan
    already pulled from the tile cache -- not a second remote scan.

    Returns {"results": [{number, street, unit?, postcode?, country,
    distance_m, lat, lon}, ...], "anchor": {name, id, country,
    admin_context}, "match": "exact"|"nearest_number"|"street"} plus, when
    the answer is empty or clipped or the anchor is not the top-ranked
    candidate, a "note" saying which of the four steps ended it (or, for
    "nearest_number", naming the miss and its neighbors). `match` is absent
    only when no street was scanned at all -- no street name, no city to
    anchor in, or the city itself did not resolve.
    Raises overture.UpstreamUnavailable / overture.SchemaDegraded, which
    server.py turns into structured errors.
    """
    limit = max(1, min(int(limit), _pkg.ADDRESS_MAX_LIMIT))
    parsed_number, parsed_street, parsed_city = _pkg._parse_address_query(query or "")
    number = number if number is not None else parsed_number
    street = (street if street is not None else parsed_street).strip()
    city = (city if city is not None else parsed_city) or None
    if number is not None:
        number = str(number).strip() or None

    if not street:
        return {"results": [], "note": _ADDRESS_NO_STREET_NOTE}
    if not city:
        return {"results": [], "note": _pkg._ADDRESS_NO_ANCHOR_NOTE}

    # #465: no house number and a street half shaped like "A & B" is a
    # crossing, which the address scan can never find (no address point is
    # on a street named "5th Ave & 42nd St"). Hand it to the tool built for
    # it, with the same city, and say so in the payload.
    if number is None:
        crossing = _pkg._split_intersection(street)
        if crossing is not None:
            result = _pkg.geocode_intersection(crossing[0], crossing[1], city)
            result["delegated_to"] = "geocode_intersection"
            return result

    resolved = _pkg._resolve_city_anchor(city, action_label="scan")
    if resolved.bbox is None:
        return {
            "results": [],
            "note": _address_unresolved_anchor_note(
                city, resolved.top, resolved.rejected, resolved.too_broad
            ),
        }
    anchor = resolved.anchor
    bbox = resolved.bbox
    notes: list[str] = list(resolved.notes)

    origin = (anchor["lat"], anchor["lon"])
    patterns = _pkg._street_variants(street)
    rows, distinct_in_range, matched_rows, number_filtered = _pkg._scan_addresses_in_bbox(
        bbox, origin, patterns, number, limit, locality=anchor["name"]
    )
    # match is answer-level, not per-row: one geocode_address call answers one
    # query at one tier, and a per-row field would repeat the same value on
    # every result for no extra information while implying rows could
    # disagree (they can't -- exact rows are always all-exact, nearest_number
    # rows are always all-neighbors).
    match: str | None = None
    neighbor_note: str | None = None
    if number is not None and not rows and number_filtered:
        # The exact number has no address point, but the dataset does carry
        # one to filter on -- so before calling the street empty, ask
        # whether the street has any points at all to bracket the miss
        # with. _scan_street_neighbors_in_bbox re-runs the same WHERE
        # bbox/street clause with the number filter dropped: a second local
        # DuckDB query against the parquet the scan above already pulled
        # into the tile cache (#202/#414), not a second remote scan. Never
        # run for a street with no numeric target ("Lot 4") -- there is
        # nothing honest to bracket it with.
        target = _pkg._parse_leading_int(number)
        if target is not None:
            neighbor_rows = _pkg._scan_street_neighbors_in_bbox(
                bbox, origin, patterns, target, locality=anchor["name"]
            )
            chosen, bracketed = _pkg._bracket_numbers(neighbor_rows, target, limit)
            if chosen:
                rows = chosen
                match = "nearest_number"
                neighbor_note = _address_nearest_number_note(
                    number, street, chosen, bracketed
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
    if number is not None and not number_filtered:
        # The dataset has no `number` column, so the house number could not be
        # filtered on and these are every doorway on the street. This
        # goes in the note, not just degraded_fields: a caller who asked for
        # one address and silently got the street back has been answered a
        # different question than the one they asked.
        notes.append(
            f"this dataset carries no `number` column, so the house number "
            f"\"{number}\" could not be matched -- these are the doorways on "
            f"\"{street}\", not that one address."
        )
        match = "street"
    if not rows:
        notes.append(_address_empty_note(origin, street, anchor))
        match = "street"
    elif distinct_in_range > len(rows):
        payload["truncated"] = True
        payload["distinct_in_range"] = distinct_in_range
        notes.append(
            f"showing the {len(rows)} nearest of {distinct_in_range} distinct "
            f"addresses matching \"{street}\" in {anchor['name']} (deduplicated from "
            f"{matched_rows} raw address points). Add a house number to land on one "
            f"doorway."
        )
    if neighbor_note:
        notes.append(neighbor_note)
    if notes:
        payload["note"] = " ".join(notes)
    # Every path past the anchor resolves to exactly one tier: a number that
    # landed on its own address point is "exact"; anything else -- no number
    # asked for, a number the dataset couldn't filter on, a miss with no
    # street points to bracket it with -- is "street", the street-level
    # answer #225 always gave. Set last, once, rather than threaded through
    # every branch above, so no path can leave it unset or double-set it.
    payload["match"] = match if match is not None else ("exact" if number is not None else "street")
    if not rows:
        return payload
    degraded = addresses.degraded_fields()
    if degraded:
        payload["degraded_fields"] = degraded
    return payload



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
        return {"results": [], "note": _INTERSECTION_NO_ANCHOR_NOTE}
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
            "note": _intersection_unresolved_anchor_note(
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
