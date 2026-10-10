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
# today's empty answer.
#
# Be clear about what that buys: `\d{4}` matches years, so geocode("1984")
# *does* pay the aggregate — a ~12s cold scan of a 474M-row theme (measured
# against release 2026-07-22.0). That is deliberate and not fixable by a
# heuristic, because
# four-digit postcodes are real and heavily used (DK/NO/AT/CH/BE/HU...), and
# "2100" is Copenhagen Ø as surely as "1984" is a novel; no rule separates
# them without breaking the countries this feature exists to serve. What
# bounds the damage instead is _POSTCODE_AGGREGATE_CACHE below: the scan is
# paid once per (dataset, code) per process, so a year-shaped query costs at
# most one scan for the life of the process rather than one per call.
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
# (measured against release 2026-07-22.0). Membership in
# addresses.COVERED_COUNTRIES therefore does NOT imply a postcode can be
# looked up here, which is exactly what the empty-result note has to say.
_POSTCODE_ZERO_COUNTRIES = ("CL", "CO", "EE", "HK", "IT", "JP", "NZ", "RS", "TW")


# How far from a postcode centroid a division may sit and still be reported as
# the place that code is in. A postcode centroid with nothing named within
# 25km is better answered by coordinates alone than by naming a town half a
# region away.
_POSTCODE_LOCALITY_MAX_M = 25_000


# Latitude window (degrees) prefiltering the local divisions table before the
# distance sort -- generous next to _POSTCODE_LOCALITY_MAX_M, and only there
# so the nearest-division scan reads a slice rather than the whole table.
# 0.5 degrees of latitude is ~55km, comfortably outside the 25km cap.
_POSTCODE_LOCALITY_WINDOW_DEG = 0.5


# A degree of longitude shrinks with cos(latitude), so the same 0.5 degrees
# that is ~55km at the equator is ~19km at 70N -- narrower than the 25km cap
# it is supposed to be generous next to, which would silently drop a locality
# that is genuinely in range from a northern-Norway or Alaskan postcode. The
# window is therefore widened by 1/cos(lat), floored so the tropics keep the
# plain 0.5 and clamped at the poles where the scale factor runs away.
_POSTCODE_LOCALITY_COS_FLOOR = 0.05



def _locality_lon_window(lat: float) -> float:
    """Longitude half-window (degrees) covering _POSTCODE_LOCALITY_MAX_M at
    `lat` -- see _POSTCODE_LOCALITY_COS_FLOOR."""
    scale = max(math.cos(math.radians(lat)), _POSTCODE_LOCALITY_COS_FLOOR)
    return min(_pkg._POSTCODE_LOCALITY_WINDOW_DEG / scale, 180.0)


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



# Aggregate results already computed this process, keyed by (dataset glob,
# variants). The scan behind one entry is the most expensive read this module
# makes, and it answers a question whose answer cannot change without the
# release changing -- which changes the glob, and so the key. Keyed on the
# glob rather than the code alone so a test (or an operator) pointing the
# addresses theme at a different dataset gets that dataset's answer, not the
# previous one's. Only successful reads land here; an UpstreamUnavailable is
# a transient fact about the network, not about the data.
_POSTCODE_AGGREGATE_CACHE: dict[tuple[str, tuple[str, ...]], list[tuple]] = {}


# Bound on the above: postcode queries are a long tail, and an unbounded dict
# in a long-lived server is a leak. Oldest-first eviction (dicts preserve
# insertion order) is enough -- the cost of a miss is one scan, not an error.
_POSTCODE_AGGREGATE_CACHE_MAX = 256



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
    (measured live); the note says so when the read is remote. Repeats within
    the process are served from _POSTCODE_AGGREGATE_CACHE.

    Two filters keep the answer internally consistent:

    `upper(trim(postcode))` on the column side, because the variants are
    uppercased and a source that wrote "1011 ab" or padded the value is
    otherwise invisible -- and an invisible row would come back as the
    coverage note, which asserts something quite different (that the theme
    does not carry the country) than "we compared case-sensitively".

    A NOT NULL guard on the bbox corners, because count(*) and avg() do not
    treat NULLs alike: avg() skips them, count(*) does not. A country whose
    rows are all bbox-less would hand back a NULL centroid -- which used to
    reach round() and raise TypeError -- and one whose rows are partly
    bbox-less would report an address_count measured over more rows than the
    centroid was. Filtering first makes both numbers describe one row set.
    """
    glob = addresses._upstream_glob()
    key = (glob, tuple(variants))
    cached = _pkg._POSTCODE_AGGREGATE_CACHE.get(key)
    if cached is not None:
        return cached
    cols = overture.probe_schema(glob)
    if cols is not None and ("postcode" not in cols or "country" not in cols):
        return []
    params = {f"v{i}": v for i, v in enumerate(variants)}
    in_list = ", ".join(f"${k}" for k in params)
    sql = f"""
        SELECT country, count(*) AS n,
               avg(bbox.ymin) AS lat, avg(bbox.xmin) AS lon
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE upper(trim(postcode)) IN ({in_list})
          AND country IS NOT NULL
          AND bbox.ymin IS NOT NULL AND bbox.xmin IS NOT NULL
        GROUP BY country
        ORDER BY n DESC, country
        LIMIT {_POSTCODE_MAX_COUNTRIES}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    if len(_pkg._POSTCODE_AGGREGATE_CACHE) >= _POSTCODE_AGGREGATE_CACHE_MAX:
        del _pkg._POSTCODE_AGGREGATE_CACHE[next(iter(_pkg._POSTCODE_AGGREGATE_CACHE))]
    _pkg._POSTCODE_AGGREGATE_CACHE[key] = rows
    return rows



def _covering_division_from_local(
    lat: float, lon: float, local_table: str, country: str | None = None
) -> dict | None:
    """Nearest locality-ish division to a point, from the #43 local table.

    60ms measured against the already-materialized table, which is why
    the postcode answer can afford to name a place per country rather than
    handing back bare coordinates.

    `country` constrains the search to the country the caller already knows
    the point is in. A postcode centroid near a border is otherwise named by
    whatever locality is nearest across it -- 68300 in France sits a couple of
    km from Basel, and a row reading {"country": "FR", admin_context ending
    "Basel"} contradicts itself. Distance alone cannot catch this: the nearest
    division genuinely is the foreign one.
    """
    lon_window = _pkg._locality_lon_window(lat)
    params: dict = {"lat": lat, "lon": lon}
    country_filter = ""
    if country:
        country_filter = "AND country = $country"
        params["country"] = country
    sql = f"""
        SELECT name, subtype, admin_chain,
               {_LOCAL_DISTANCE_EXPR} AS distance_m
        FROM read_parquet('{local_table}')
        WHERE subtype IN {_POSTCODE_LOCALITY_SUBTYPES}
          AND lat BETWEEN $lat - {_pkg._POSTCODE_LOCALITY_WINDOW_DEG}
                      AND $lat + {_pkg._POSTCODE_LOCALITY_WINDOW_DEG}
          AND lon BETWEEN $lon - {lon_window}
                      AND $lon + {lon_window}
          {country_filter}
        ORDER BY distance_m
        LIMIT 1
    """
    try:
        with overture._conn_lock:
            row = overture.conn().execute(sql, params).fetchone()
    except duckdb.Error as e:
        _pkg.logger.warning("local divisions lookup for postcode locality failed: %s", e)
        return None
    if row is None or row[3] > _pkg._POSTCODE_LOCALITY_MAX_M:
        return None
    return {"name": row[0], "admin_context": [*_pkg._admin_chain_context(row[2], self_name=row[0]),
                                              row[0]]}



def _covering_division(
    lat: float, lon: float, local_table: str | None, country: str | None = None
) -> dict | None:
    """The place a postcode centroid sits in, local table first.

    Falls back to _nearest_division's upstream scan when there is no local
    table (PLACEROOT_CACHE=off, or materialization failed) -- the same
    degrade every other #43 caller makes, and it keeps the postcode answer
    naming a place rather than dropping to coordinates just because caching
    is off. Both paths take the same `country` constraint, so turning the
    cache off changes what the answer costs but not what it says.
    """
    if local_table is not None:
        return _pkg._covering_division_from_local(lat, lon, local_table, country)
    return _pkg._nearest_division(lat, lon, country=country)



def _postcode_results(
    display: str, rows: list[tuple], local_table: str | None, limit: int
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

    `limit` is applied here rather than to the returned list, because each
    row costs a covering-division lookup: with the cache off that is an
    upstream scan retried at three radii, so trimming afterwards paid for up
    to _POSTCODE_MAX_COUNTRIES x 3 scans to build rows the caller never saw.
    Trimming first is safe for rank_score -- the aggregate is ordered by
    count descending, so the largest count is in the first row either way.
    """
    top = max((r[1] for r in rows), default=0) or 1
    results = []
    for country, count, lat, lon in rows[:limit]:
        lat, lon = round(lat, 6), round(lon, 6)
        covering = _pkg._covering_division(lat, lon, local_table, country)
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
    zero = ", ".join(_pkg._POSTCODE_ZERO_COUNTRIES)
    return (
        f"postcodes here come from Overture's addresses theme, which carries "
        f"{covered} countries (no UK, Ireland, India or China at all), and "
        f"{len(_pkg._POSTCODE_ZERO_COUNTRIES)} of those ({zero}) carry no postcode "
        f"values whatsoever"
    )



def _postcode_cold_scan_sentence() -> str:
    """Said only when the aggregate actually went over the network -- against
    a local dataset or mirror it would be a lie."""
    if not _pkg._is_remote(addresses._upstream_glob()):
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



def geocode(
    query: str, limit: int = DEFAULT_LIMIT, lang: str | None = None, country: str | None = None,
    near: NearConstraint | None = None,
) -> list[dict]:
    """Free-text place name -> ranked candidates. See geocode_detailed.

    country (#457) and near (#476): see geocode_detailed. May raise ValueError.
    """
    return _pkg.geocode_detailed(query, limit, lang=lang, country=country, near=near)["results"]



def geocode_batch(
    queries: list[str], limit_per_query: int = 3, country: str | None = None,
) -> list[dict]:
    """Geocode many names against ONE opened local divisions table (#329).

    Opens the name table (and its alt-name sibling) once, then looks every
    query up against that same path. A two-name walk must not pay N cold
    S3 scans or N table materializations. Each row is the top candidate
    for that query, or the standard error envelope {"query", "error":
    "not_found", "detail"} (roadmap §4, next tier: unified per-row batch
    error shape — a bare "no match" string used to stand in its place);
    input order is preserved. The caller (server.py) applies the 20-query
    cap.

    country (#457) applies the same ISO 3166-1 constraint to every query in
    the batch. May raise ValueError (validated once, up front, rather than
    per query).
    """
    if country is not None:
        country = _pkg.normalize_country(country)
    local_table = _pkg._local_divisions_table()
    alt_table = _pkg._local_alt_names_table(local_table)
    rows = []
    for query in queries:
        hits = _pkg.geocode_detailed(
            query, limit_per_query, local_table=local_table, alt_table=alt_table,
            country=country,
        )["results"]
        if not hits:
            rows.append({
                "query": query,
                "error": "not_found",
                "detail": f"no match for {query!r}",
            })
            continue
        top = hits[0]
        rows.append({
            "query": query,
            "name": top["name"],
            "type": top["type"],
            "lat": top["lat"],
            "lon": top["lon"],
            "id": top["id"],
            "rank_score": top["rank_score"],
        })
    return rows



def geocode_detailed(
    query: str, limit: int = DEFAULT_LIMIT, include_country: bool = False,
    *,
    local_table: str | None = None,
    alt_table: str | None = None,
    lang: str | None = None,
    country: str | None = None,
    near: NearConstraint | None = None,
) -> dict:
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

    A query that is entirely a postcode ("94110", "1011AB", "SW1A 1AA") is
    answered from the addresses theme instead of by name (#223): one row per
    country carrying that code, `type` "postcode", `id` None, plus `country`
    and `address_count`, with a "note" on both the granularity of a postcode
    centroid and the theme's coverage. See the module docstring's #223
    section.

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

    include_country adds each row's ISO country code (None for a places-
    fallback row, which has no admin chain to read one off) to the result
    dicts. Off by default, and deliberately not part of the MCP tool's
    payload — it exists for geocode_address, which has to reject a
    runner-up anchor sitting in a different country than the top candidate
    ("London" -> London, Ontario for a UK query) and cannot do that from
    admin_context alone once a row's chain is empty.

    lang (#410, a 2-3 letter code) requests Overture's language-tagged
    names.common variant for each division-kind row, when one exists for
    that id and language: `name` becomes the variant and the primary is
    added back as `name_primary` only when it differs — piggybacked as one
    extra lookup keyed by the batch of result ids, not a scan per row (see
    _lang_variants_for). Places-fallback rows (a row's `_category` is set)
    are unaffected — same scope line find_places itself draws this round.
    No lang given, or no variant found for a row, leaves it byte-identical
    to the no-lang answer.

    country (#457, ISO 3166-1 alpha-2/alpha-3, case-insensitive; aliases
    like "UK"/"USA" also accepted) is the explicit form of the "City,
    Country" suffix parsing below — constrains divisions candidates to
    that country's own `country` column. Raises ValueError if it isn't a
    recognized country code, or if it disagrees with a country/region
    suffix parsed off `query` itself (server.py turns either into a
    structured bad_request naming both).

    near (#476, a (lat, lon, radius_m) triple) is a caller that already
    knows where the query means — resolve_place with a city pin — telling
    this search so. It bounds every division pass (literal, alternate,
    variant, fuzzy) to that box, and it *is* the places-fallback anchor:
    _fallback_anchor_candidates' speculative "which trailing or leading
    word is the city" splits do not run at all, because the caller has
    already answered that question. Measured before: resolve_place("Marina
    Bay Sands Singapore") ran geocode("Marina Bay Sands") unconstrained,
    which anchored on Marina, California (the only division "Marina"
    matches outright), scanned places there, retried five more cities on
    three continents, and scheduled California's places and base-theme
    tiles for background materialization — for a query pinned to
    Singapore. With near, none of that has a reason to happen. No near
    given leaves this function byte-identical to before.
    """
    query = query.strip()
    limit = max(1, min(limit, _pkg.MAX_LIMIT))
    if not query:
        return {"results": []}

    normalized_country = _pkg.normalize_country(country) if country is not None else None

    note = None
    if local_table is None:
        local_table = _pkg._local_divisions_table()

    # #223: a query that is *entirely* a postcode is not a name lookup, and
    # searching division names for "94110" finds nothing by construction. One
    # upstream aggregate over the addresses theme answers it instead, per
    # country carrying the code. A postcode-shaped query that matches nothing
    # still falls through to the name search below -- the shape detector is
    # conservative but not infallible, and a real name that happens to be
    # shaped like a postcode must still be findable.
    #
    # A postcode that *does* match returns here and the name search never
    # runs, which is intended and is the one case where the detector costs
    # something: a division literally named "2100" is unreachable through
    # geocode in a dataset where 2100 is also a live Danish postcode. The
    # alternative -- merging the two halves -- would have to rank a point
    # count against a population on one scale, and would put a namesake
    # village in the middle of an answer about a postal code. Pinned by
    # test_a_postcode_hit_short_circuits_the_name_search.
    #
    # Its note is kept aside
    # (postcode_note) rather than assigned to `note`: when both halves come
    # back empty, "this is what an empty postcode answer means" is the more
    # useful of the two explanations, so it wins at the return below.
    postcode_note = None
    variants = _pkg._postcode_variants(query)
    if variants:
        display = _postcode_display(query)
        postcode_rows = _pkg._query_postcode_countries(variants)
        if postcode_rows:
            return {
                "results": _postcode_results(display, postcode_rows, local_table, limit),
                "note": _postcode_note(display),
            }
        postcode_note = _postcode_empty_note(display)

    # #214: None whenever there is no alternate-name table to search — cache
    # off, a cache directory predating the feature, a dataset without
    # names.common — in which case every _query_divisions call below is
    # exactly the primary-name-only search it was before.
    if alt_table is None:
        alt_table = _pkg._local_alt_names_table(local_table)
    base_query, region_code, _region_name = _pkg._parse_region_suffix(query, local_table)
    country_code = None
    _country_name = None
    qualifier_note = None
    if region_code is None:
        # #457: only tried once the region parse has failed — "Springfield,
        # IL" and "London, Ontario" never reach this at all, so the
        # region/country parses can't fight over the same suffix.
        base_query, country_code, _country_name = _pkg._parse_country_suffix(
            query, local_table, alt_table
        )
        if country_code is None and normalized_country is None:
            # #457: neither a region nor a country suffix resolved. "Never
            # search the joined string" — a comma-separated qualifier
            # (only a comma counts; see _unrecognized_comma_qualifier) that
            # names nothing this module recognizes still gets set aside,
            # with a note, rather than searching a literal string no
            # division name will ever equal.
            unrecognized = _pkg._unrecognized_comma_qualifier(query)
            if unrecognized:
                base_query, suffix_text = unrecognized
                qualifier_note = _pkg._unrecognized_qualifier_note(suffix_text, base_query)
            else:
                base_query = query

    # Whether country_code (if any) came from parsing `query` itself, as
    # opposed to the explicit `country=` param merged in just below — only
    # a *parsed* country gets the #46-style "degrade to unconstrained on
    # zero candidates" treatment. An explicit country= is a deliberate
    # filter the caller stated on purpose; zero matches inside it is a real
    # answer, not a misparse worth second-guessing.
    # (If both were given they already agree — the conflict check below
    # raises otherwise — and the explicit one wins the "no degrade" rule.)
    country_code_from_suffix = country_code is not None and normalized_country is None

    if normalized_country is not None:
        if country_code is not None and country_code != normalized_country:
            raise ValueError(
                f"country={country!r} conflicts with the parsed qualifier "
                f"{_country_name!r} ({country_code}) in {query!r}"
            )
        if region_code is not None:
            implied = region_code.split("-", 1)[0]
            if implied and implied != normalized_country:
                raise ValueError(
                    f"country={country!r} conflicts with the parsed region "
                    f"qualifier {_region_name!r} ({implied}) in {query!r}"
                )
        country_code = normalized_country

    search_query = base_query if (region_code or country_code or qualifier_note) else query
    # `region_code`/`country_code` are cleared below if a constrained
    # search comes up empty; the #215 fuzzy pass still wants the code the
    # query itself carried, so keep it.
    suffix_region_code = region_code
    suffix_country_code = country_code

    # #476: `near_kw` rather than a bare keyword so the no-constraint call
    # stays exactly the call it was (the pre-#476 signature is what every
    # test double of _query_divisions in the suite answers to).
    near_kw: dict = {"near": near} if near is not None else {}
    # The explicit country= filter, kept on every degrade/retry below: only
    # a qualifier *parsed* off the query is a guess worth withdrawing (the
    # contract stated at country_code_from_suffix). Passed as a kwarg only
    # when set, for the same reason as near_kw.
    explicit_country_kw: dict = (
        {"country_code": normalized_country} if normalized_country is not None else {}
    )
    divisions = _pkg._query_divisions(
        search_query, region_code, local_table, alt_table=alt_table,
        country_code=country_code, **near_kw,
    )
    if region_code and not divisions:
        # #46: recognized a region suffix, but nothing in this dataset
        # matches inside it — degrade to an unconstrained search of the
        # original query rather than returning empty for a query that
        # would otherwise have matched something.
        region_code = None
        country_code = normalized_country
        search_query = query
        divisions = _pkg._query_divisions(
            search_query, None, local_table, alt_table=alt_table,
            **explicit_country_kw, **near_kw,
        )
    elif country_code and not divisions and country_code_from_suffix:
        # #457: same idea, but degrading to the BASE name (not the whole
        # comma-joined string) — Overture names are bare, so re-searching
        # "Springfield, GB" verbatim could never match anything anyway,
        # unlike the region path above which still has today's plain
        # substring behavior to fall back on for a name+suffix combination
        # its own docstring already documents as unconstrained-original.
        #
        # Only for a country parsed off the query itself, not an explicit
        # country= (see country_code_from_suffix above) — a caller-stated
        # filter coming up empty is a real, precise answer.
        #
        # A bare trailing word (no comma — "Portland Jersey") degrades to
        # the whole query instead, like the region path: the base word
        # alone is a far broader search than the caller typed.
        country_code = None
        search_query = base_query if "," in query else query
        divisions = _pkg._query_divisions(
            search_query, None, local_table, alt_table=alt_table, **near_kw
        )
        qualifier_note = _pkg._country_degrade_note(search_query, suffix_country_code)

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
    best_literal_tier = max((_pkg._effective_tier(c, search_query) for c in divisions), default=0)
    literal_match_has_prominence = any(
        _pkg._effective_tier(c, search_query) == best_literal_tier
        and c.get("population") is not None
        for c in divisions
    )
    literal_answer_is_good_enough = (
        best_literal_tier >= _pkg._STRONG_TIER and literal_match_has_prominence
    )
    seen_ids = {c["id"] for c in divisions}
    variant_rows: list[dict] = []
    if not literal_answer_is_good_enough:
        for variant_query in _pkg._abbreviation_variant_queries(search_query):
            for row in _pkg._query_divisions(
                variant_query, region_code, local_table, country_code=country_code, **near_kw
            ):
                if row["id"] not in seen_ids:
                    row["_variant"] = True
                    # Tier against the variant text it actually matched
                    # (#53) — see _effective_tier's docstring for why this
                    # can't be recomputed against the original query later.
                    row["_tier"] = _pkg._match_tier(row["name"], variant_query)
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
    #
    # perf: under a caller's near box (#476 — resolve_place's city pin) a
    # literal answer that is already confident (_CONFIDENT_TIER or better,
    # with a population behind it) stands the folded pass down as well.
    # #221's reason for always running it — a populated namesake village
    # somewhere on Earth shadowing the folded spelling of a famous city — is
    # a worldwide problem; inside the one city's box the caller pinned, an
    # exact-or-prefix populated division IS the answer, and the folded pass
    # could only pad it. Unpinned callers keep #221's always-run rule.
    confident_literal = (
        near is not None
        and best_literal_tier >= _pkg._CONFIDENT_TIER
        and literal_match_has_prominence
    )
    if not confident_literal and (local_table is not None or not literal_answer_is_good_enough):
        stripped_query = _pkg._strip_diacritics(search_query)
        # Not when stripping left nothing: a query of only combining marks
        # folds to "", and searching for it is an ILIKE '%%' that matches
        # every division in the dataset — a nonsense query answered with
        # whichever places are most populous. The literal pass, matching raw
        # names, correctly returns nothing for those.
        rows = (
            _pkg._query_divisions(
                stripped_query, region_code, local_table,
                fold_diacritics=True, country_code=country_code, **near_kw,
            )
            if stripped_query
            else []
        )
        for row in rows:
            if row["id"] not in seen_ids:
                row["_variant"] = True
                row["_tier"] = _pkg._match_tier(row["name"], stripped_query)
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
    if not divisions and local_table is not None and not _pkg._has_like_metacharacter(fuzzy_query):
        if _pkg._is_bundled_table(local_table):
            # A >=0.92 near-miss against only the top-150k names is how
            # "Berkeley Springs" becomes Berkeley: on the stage-0 index the
            # fuzzy tier trades a recall gap for confidently wrong answers.
            # Skip it; the long-tail fallback below (and the full table,
            # once its background build lands) handle the real name.
            fuzzy_rows = []
        else:
            fuzzy_rows = _pkg._query_divisions_fuzzy(
                local_table, fuzzy_query, suffix_region_code, suffix_country_code, **near_kw
            )
        if not fuzzy_rows and (
            suffix_region_code or (suffix_country_code and normalized_country is None)
        ):
            # Retry without the *parsed* qualifier only; an explicit
            # country= is the caller's filter and stays on.
            if normalized_country is not None:
                fuzzy_rows = _pkg._query_divisions_fuzzy(
                    local_table, fuzzy_query, None, normalized_country, **near_kw
                )
            else:
                fuzzy_rows = _pkg._query_divisions_fuzzy(local_table, fuzzy_query, **near_kw)
        divisions = fuzzy_rows

    _bundled_recall_pending = not divisions and _pkg._is_bundled_table(local_table)
    if (
        _bundled_recall_pending
        # #268: no division is named "Eiffel Tower". This scan exists to
        # find long-tail *populated places* the bundled index omits, and a
        # query naming a feature is not one — it was 12.8s of a 13.1s call,
        # spent proving a negative the query's own shape already implies.
        and not _pkg._names_a_feature(search_query)
        # #476: a pinned query has its anchor already; the places search
        # gets its chance first, same as when a split derives one.
        and near is None
        and _pkg._fallback_anchor(
            search_query, [], region_code, local_table,
            alt_table=_pkg._local_alt_names_table(local_table),
            region_population=_pkg._region_population_lookup(local_table),
        ) is None
    ):
        # The stage-0 bundled index carries only populous divisions; a
        # long-tail name (an unpopulated hamlet) must not read as "no such
        # place" while the full table builds in the background. With no
        # anchor derivable, the anchored-places path can't answer either,
        # so run the upstream divisions scan (the pre-#43 recall path)
        # now. When an anchor IS derivable the places search gets its
        # chance first — "Shibuya Crossing Tokyo" measured 55-70s when a
        # zero-row divisions scan ran ahead of it — and the same upstream
        # scan runs after it instead, only if nothing at all was found
        # (see the empty-candidates recall retry below).
        divisions = _pkg._query_divisions(search_query, region_code, None, country_code=country_code)  # noqa: E501
        _bundled_recall_pending = False

    region_population = _pkg._region_population_lookup(local_table)
    _pkg._flag_namesake_localities(divisions, search_query)
    divisions.sort(key=lambda r: _pkg._rank_key(r, search_query, region_population))

    # #406: cheap on an already-sorted list — the disclosure note only ever
    # needs to know whether the home bias picked a *different* top division
    # than an unbiased sort would have, not the full unbiased ordering.
    # `_home_biased_winner_id` is the division id the disclosure names if it
    # does turn out to be the final answer's top result (checked once, right
    # before `out` is built below — a places-fallback row, or a later recall
    # pass, can still overtake it).
    _home_biased_winner_id = None
    if divisions and home_region.home_bias_active():
        unbiased_top = min(
            divisions,
            key=lambda r: _pkg._rank_key(r, search_query, region_population, home_bias=False),
        )
        if divisions[0]["id"] != unbiased_top["id"]:
            _home_biased_winner_id = divisions[0]["id"]

    # #329: a famous landmark's exact-matching namesake (Colosseo the
    # Frosinone microhood, Colosseum in Queensland) must not stand the
    # places fallback down or win the batch. If we have an alias pin,
    # keep only rows in that city and still search places there.
    alias_hit = _pkg._alias_anchor(search_query)
    if alias_hit is not None:
        alat, alon, _aname = alias_hit
        def _near_alias(d):
            return geo.haversine_m(alat, alon, d["lat"], d["lon"]) <= _pkg._CITY_HINT_RADIUS_M
        divisions = [d for d in divisions if _near_alias(d)]
        # #215 stands the places fallback down once *any* fuzzy row exists.
        # A far namesake that only survived as a typo correction (Colosseo
        # the Frosinone microhood for "Colosseo Roma") must not.
        fuzzy_rows = [d for d in fuzzy_rows if _near_alias(d)]

    # Skip the places fallback once an exact-name division match is already
    # in hand: places is Overture's largest, least-indexed theme, and an
    # unbounded ILIKE scan over it live can cost tens of seconds (measured)
    # — worth paying to fill out a weak result set (a prefix/substring-only
    # match, or none at all), not worth paying just to pad an already-exact
    # answer out to `limit`.
    has_exact_division = any(_pkg._effective_tier(c, search_query) == 3 for c in divisions)
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
        #
        # #476: under a caller's pin the pin is the anchor and the whole
        # search_query is the name to look for — the caller already split
        # the city off. The speculative splits are what aimed a Singapore
        # query at Marina, California; they have nothing to add here.
        if near is not None:
            anchor_options = [(near[0], near[1], search_query)]
        else:
            anchor_options = _pkg._fallback_anchor_candidates(
                search_query, divisions, region_code, local_table, alt_table=alt_table,
                region_population=region_population,
            )
        # #329: a famous landmark with no derivable city still has an
        # alias pin on the bundled index — use it as the places-fallback
        # anchor rather than skipping the search (or aiming at a random
        # "Tower"/"Center" division).
        if alias_hit is not None:
            pin = (round(alias_hit[0], 3), round(alias_hit[1], 3))
            rest = [
                a for a in anchor_options
                if (round(a[0], 3), round(a[1], 3)) != pin
            ]
            anchor_options = [alias_hit] + rest
        anchor_hit = anchor_options[0] if anchor_options else None
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
        elif anchor is None and _pkg._skip_unanchored_places_scan():
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
            # #476: an anchor derived by splitting the query is a guess
            # about where the query means, and the first scan is the test
            # of that guess. Its box's tiles are not scheduled for
            # background materialization until the guess has paid off —
            # measured, the losing guesses left 807 MB of Florida and
            # California places/base tiles behind two European landmark
            # resolves. A pin, an alias, or a division match already in
            # hand is not a guess and schedules as before.
            speculative = divisions == [] and alias_hit is None and near is None
            places = _pkg._query_places_fallback(
                name_query, anchor=anchor, also=search_query,
                schedule_tiles=not speculative,
            )
            if not places and len(anchor_options) > 1:
                # The best reading of the query found nothing where it
                # pointed. Ambiguous city names make that reading a coin
                # flip — "harvard square cambridge" anchored on Cambridge,
                # UK over Ontario's and Massachusetts's, and the answer sat
                # in the third city of that name. Sequential retries lose
                # that race one box at a time, so the second pass scans
                # every candidate city's box at once: one bounded query,
                # file-pruned per box, against answering nothing.
                alternates = [(a[0], a[1]) for a in anchor_options[1:] if (a[0], a[1]) != anchor]
                if alternates:
                    places, winner = _pkg._query_places_multi_anchor(
                        name_query, alternates, also=search_query,
                    )
                    if places and winner is not None:
                        anchor = winner
            if speculative and places and anchor is not None:
                # The guess held: this is the anchor that answered, so its
                # tiles are worth having for the repeat query (#476).
                _pkg._schedule_places_tiles_near(anchor)
            places = [p for p in places if p["name"].lower() not in seen_names]
            # Rank by the better of the two readings of the query: a row
            # named for the whole query beats one that merely contains the
            # residual the anchor split left (#268).
            #
            # Distance to the anchor breaks ties before confidence does. Names
            # repeat inside one metro area — "Millennium Park Chicago" anchored
            # correctly on the Loop and still answered with a Millennium Park
            # 31 km away in the suburbs, because that row happened to carry
            # more confidence. The anchor is the caller's own statement about
            # where they mean; among equally-good name matches, closer to it is
            # closer to what they asked for.
            #
            # #429: the bundled alias spellings of a landmark are readings of
            # the query too, and grading against them is what stops a
            # world-famous name from losing on a technicality. "Musée du
            # Louvre" merely *contains* the word "Louvre", so literal grading
            # made it a substring match and sorted it below every hotel whose
            # name happens to start with the word; against the alias spelling
            # "musee du louvre" it is the exact match it actually is.
            alias_readings = _pkg._alias_names_for(search_query)

            def _rank_place(r):
                near = 0.0
                if anchor is not None:
                    near = geo.haversine_m(anchor[0], anchor[1], r["lat"], r["lon"])
                return (
                    -max(
                        _pkg._match_tier(r["name"], reading)
                        for reading in (name_query, search_query, *alias_readings)
                    ),
                    round(near / 1000.0),
                    -r["_confidence"],
                    r["id"],
                )

            places.sort(key=_rank_place)
            candidates = candidates + places

    if not candidates and _bundled_recall_pending and not _pkg._names_a_feature(search_query):
        # Anchored-places had its chance and found nothing either; the
        # query may be a real division below the bundled index's
        # population cutoff. One upstream divisions scan preserves the
        # recall the full local table used to guarantee — paid only when
        # every faster path came up empty, and only until the background
        # full build lands.
        #
        # Not for a query naming a feature, though (#268): no division is
        # called "Eiffel Tower" or "Grand Central Terminal", so the scan can
        # only come back empty, and it was the entire cost of doing so —
        # 11.0s of a fresh install's first landmark query, spent proving a
        # negative that the query's own shape already implies.
        recalled = _pkg._query_divisions(
            search_query, region_code, None, country_code=country_code, **near_kw
        )
        _pkg._flag_namesake_localities(recalled, search_query)
        recalled.sort(key=lambda r: _pkg._rank_key(r, search_query, region_population))
        candidates = recalled
        # #406: the recall pass replaces `candidates` wholesale, so redo the
        # bias-changed-winner check against it rather than trusting the
        # (now superseded) one computed above.
        _home_biased_winner_id = None
        if recalled and home_region.home_bias_active():
            unbiased_top = min(
                recalled,
                key=lambda r: _pkg._rank_key(r, search_query, region_population, home_bias=False),
            )
            if recalled[0]["id"] != unbiased_top["id"]:
                _home_biased_winner_id = recalled[0]["id"]

    # #410: one lookup for the whole page of division-kind rows about to be
    # returned, keyed by the ids actually surviving [:limit] — not a scan per
    # row, and not run at all unless a lang was actually requested. Places-
    # fallback rows (row["_category"] is set) are excluded: they come from a
    # different theme/id-space than the divisions lang table indexes, same
    # scope line find_places draws this round (see the docstring above).
    lang_variants: dict[str, str] = {}
    if lang:
        page = candidates[:limit]
        division_ids = [r["id"] for r in page if r.get("id") and r.get("_category") is None]
        lang_variants = _pkg._lang_variants_for(_pkg._local_lang_names_table(local_table), division_ids, lang)  # noqa: E501

    out = []
    for row in candidates[:limit]:
        entry = {
            "name": row["name"],
            "type": row["subtype"],
            "lat": row["lat"],
            "lon": row["lon"],
            "id": row["id"],
            "admin_context": row["admin_context"],
            "rank_score": _pkg._rank_score(row, search_query) if "_confidence" not in row else round(  # noqa: E501
                0.4 + row["_confidence"] * 0.3, 3
            ),
        }
        if include_country:
            entry["country"] = row.get("country")
        if row.get("_category") is not None:
            # Places-fallback rows only: the place's Overture category, so
            # resolve_place (and any caller) doesn't lose it in the merge.
            entry["category"] = row["_category"]
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
        variant = lang_variants.get(row["id"]) if row.get("id") else None
        if variant and variant != entry["name"]:
            # #410: never invent or transliterate — variant only ever comes
            # from Overture's own names.common map, looked up above.
            # name_primary appears only when it actually differs, so a
            # no-variant answer stays byte-identical to the no-lang one.
            entry["name_primary"] = entry["name"]
            entry["name"] = variant
        out.append(entry)
    if out:
        _pkg._kick_autowarm(out[0])
    result = {"results": out}
    fuzzy_out = [row for row in candidates[:limit] if row.get("_fuzzy")]
    if fuzzy_out:
        # #215: unlike the skip notes below, this one accompanies real
        # results — it says which spelling they actually answer. Named
        # against the string actually fuzzed, which is the query minus any
        # region suffix.
        result["note"] = _pkg._fuzzy_correction_note(fuzzy_out, fuzzy_query)
    elif qualifier_note:
        # #457: same idiom — rides along with real results (an
        # unrecognized qualifier or a country degrade still searches the
        # base name and usually finds something), not instead of them.
        result["note"] = qualifier_note
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
    if (
        _home_biased_winner_id is not None
        and out
        and out[0]["id"] == _home_biased_winner_id
    ):
        # #406: only fires when the home-biased division is still the
        # actual top result after everything else (places fallback, limit
        # trim) has had its say — never when the bias was overridden by a
        # stronger later signal. Never a filter: the distant candidates it
        # displaced are still in `out`, just not first.
        home = home_region.get_home_region()
        disclosure = home_region.disclosure_note(home)
        result["note"] = f"{result['note']} {disclosure}" if result.get("note") else disclosure
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
    "names for (only common words like \"the\" or \"of\", or generic type words like "
    "\"Station\" or \"Park\"), so the places half of "
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
    return _pkg._is_remote(overture.upstream_glob(theme="places", type_="place"))


# Bbox radius (#22) for the name-filtered find_places call when no
# near_lat/near_lon hint is given but a division match is in hand — "same
# metro area" as the top division match, not a general-purpose area search.
_RESOLVE_PLACE_RADIUS_M = 20_000


# #481: how far from a bundled-alias pin a candidate still counts as *the*
# landmark the alias names. The alias table is a curated coordinate — the
# strongest evidence this module ever holds about where a query means — and
# a candidate sitting on it must outrank one that merely earned a better
# string tier 4 km away ("notre dame paris" answered Notre Dame de
# Clignancourt, a parish church in the 18th, on a prefix match while the
# cathedral 30 m from the pin only *contained* the words). 400 m is a
# landmark's footprint plus the shops named after it, not a neighbourhood.
_ALIAS_PIN_RADIUS_M = 400

# Rows fetched by the one unfiltered nearest-first scan at an alias pin —
# the landmark itself sits metres from its pin, so the nearest couple of
# dozen rows always include it even on a square packed with cafés and
# souvenir shops. find_places clamps to overture.MAX_ROWS (25) regardless.
_ALIAS_PIN_SCAN_LIMIT = 25


_MATCH_TIER_LABELS = {3: "exact", 2: "prefix", 1: "substring"}


# resolve_place's `match` labels, best first. "contains" (#475) is a place
# label only — the candidate's whole name contains the whole query, or the
# reverse — and sits above "substring", which for places also covers a
# single shared significant word (see _place_match_label). "fuzzy" (#215)
# is not a tier the literal search can produce — it means the name doesn't
# contain the query at all and was reached by edit distance instead — so
# it ranks below every literal label, matching how _rank_key already
# orders the rows themselves.
_MATCH_LABEL_RANK = {"exact": 4, "prefix": 3, "contains": 2, "substring": 1, "fuzzy": 0}


# Small enough to filter, generic enough that requiring them in a name
# match would be actively wrong ("the Whole Foods on Lamar" — "the"/"on"
# aren't part of any real place name). Dropped before a query is split into
# per-token find_places searches and before word-overlap scoring.
_STOPWORDS = {"the", "a", "an", "on", "in", "at", "near", "of", "and", "by"}


# #469: the generic type word a place query ends in -> the Overture category
# slugs a row of that kind carries. "Shibuya Station" is a *kind* of thing
# plus a name, and Overture files the thing under a category, not under the
# English word: the station is "Gare de Shibuya" / "京王井の頭線 渋谷駅",
# category train_station, and no row anywhere is named "Shibuya Station".
# A name-only search can never reach it; a category-filtered search on the
# distinctive word alone ("Shibuya", within train_station) finds it in one
# bounded scan. See _type_word_slugs and the #469 block in resolve_place.
#
# Curated from what categories.search_categories returns for each word
# rather than looked up live: that search is lexical, and for "station" it
# ranks gas_station and radio_station level with metro_station. Every slug
# here is a row in data/overture_categories.csv (tested), and rows come back
# through find_places' substring category filter, so a scan for "park" also
# returns "parking" — _type_scan_rows drops those with an exact/hierarchy
# check against the same CSV. Words whose category is too loose to be
# useful ("market", "square", "center", "hall", "terminal") are deliberately
# absent: they still count as generic for the gate, they just get no scan.
_TYPE_WORD_CATEGORIES: dict[str, tuple[str, ...]] = {
    "station": ("train_station", "metro_station", "light_rail_and_subway_stations"),
    "park": ("park",),
    "airport": ("airport",),
    "museum": ("museum",),
    "bridge": ("bridge",),
    "tower": ("tower",),
    "temple": ("temple",),
    "shrine": ("shinto_shrines",),
    "church": ("church_cathedral",),
    "cathedral": ("church_cathedral",),
    "stadium": ("stadium_arena",),
    "arena": ("stadium_arena",),
    "university": ("college_university",),
    "college": ("college_university",),
    "school": ("school",),
    "hospital": ("hospital",),
    "zoo": ("zoo",),
    "beach": ("beach",),
    "plaza": ("plaza",),
    "hotel": ("hotel",),
    "library": ("library",),
    "theater": ("theaters_and_performance_venues",),
    "theatre": ("theaters_and_performance_venues",),
    "palace": ("palace",),
    "castle": ("castle",),
    "mall": ("shopping_center",),
}


# #469: two rows of the same category this close together are the same
# feature — a station's exits, lines and operators are each their own
# places row, a park's lawn and its athletic track likewise. A row that
# stands alone is the one mis-pinned across town (a "Shibuya Station Tokyo.
# Japan" 7 km east of every other Shibuya station row, at confidence 0.71).
_TYPE_SCAN_SUPPORT_RADIUS_M = 1_000



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
    return _MATCH_TIER_LABELS[_pkg._match_tier(row["name"], query)]



def _division_match_label(row: dict, query: str, search_query: str) -> str:
    """A division candidate's match label in resolve_place (#344): the
    better of its label against the full `query` and against the
    city-stripped `search_query`.

    Place candidates already retry against `search_query` when a match
    against the full query is weak (see the `_place_match_label` call
    sites below) — a query like "Times Square New York" strips its trailing
    city hint before searching, and the landmark a caller means is judged
    against what is actually left of their words, not against text a city
    hint added back on. Divisions never got that retry: graded only against
    the untouched original query, the *correct* "Times Square" division
    read as a bare substring ("Times Square" is not a prefix of "Times
    Square New York"), while an unrelated, coincidentally-named place
    elsewhere could read as a stronger "prefix" match purely because places'
    tiering already checks both directions. Kind-agnostic ranking (the
    point of this merge) needs kind-consistent tiering, not just a
    kind-agnostic sort key sitting on top of two different ones.
    """
    label = _match_label(row, query)
    if search_query != query:
        alt_label = _match_label(row, search_query)
        if _pkg._MATCH_LABEL_RANK[alt_label] > _pkg._MATCH_LABEL_RANK[label]:
            return alt_label
    return label



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



def _nothing_but_generic(text: str) -> bool:
    """Whether `text` holds no word a place could be *named* — every word in
    it is a _STOPWORD or a _GENERIC_PLACE_WORDS member, or there are no words
    at all.

    #472: _nothing_but_stopwords, one level up. "Shibuya Station" anchors on
    the Shibuya division and leaves "Station" as the residual, and ILIKE
    '%Station%' over the anchor's box is the same scan #216 refused for
    '%the%' with a smaller haystack: 5.5s of a 5.7s geocode() call, measured
    live, answering with "Nakameguro Station" and "Tokyo Station Beer
    Stand". A type word says what the place is, never which one — every
    station inside the box matches equally, so the results are junk by
    construction and the time is spent proving it. Same for "Park",
    "Museum", "Airport": any anchor split whose residual is only a feature
    noun.

    This is the residual gate _fallback_anchor_candidates and _alias_anchor
    actually apply. _nothing_but_stopwords is kept as it was so its meaning
    stays exact ("H&M" and a two-character CJK name still pass, and so does
    "Station" — a type word is not a stopword); this widens the *question*
    rather than the stopword set. One word being generic is not enough:
    "Blue Bottle Station" and "Westfield Valley Fair" carry a distinctive
    word alongside the type word and must reach the anchored scan.
    """
    return not any(
        w.lower() not in _STOPWORDS and w.lower() not in _pkg._GENERIC_PLACE_WORDS
        for w in re.findall(r"[\w'-]+", text)
    )



def _significant_tokens(query: str) -> list[str]:
    """query -> its meaningful words: >=3 chars, not a stopword, in order of
    appearance, capped at _MAX_RESOLVE_TOKENS. Falls back to the whole query
    if nothing survives (e.g. a query that's all short/stopword tokens)
    rather than searching nothing.
    """
    tokens = [t for t in re.findall(r"[\w'-]+", query) if len(t) >= 3]
    significant = [t for t in tokens if t.lower() not in _STOPWORDS]
    return (significant or tokens or [query])[:_pkg._MAX_RESOLVE_TOKENS]



def _is_word_prefix(prefix: str, text: str) -> bool:
    """`text` starts with `prefix` *at a word boundary*: "Mall" is a prefix
    of "Mall of America", "Ma" is not — the character after the prefix must
    end a word (or the string), or the label is only "contains"."""
    if not prefix or not text.startswith(prefix):
        return False
    return len(text) == len(prefix) or not text[len(prefix)].isalnum()



def _place_match_label(
    name: str, query: str, context_words: frozenset[str] = frozenset()
) -> str | None:
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

    Whole-name containment and a shared word are not the same strength of
    claim, and labeling them both "substring" (#475) let the tie-breaks
    decide between them: "Marina Bay Sands Singapore" answered "Freia
    Aesthetics | Marina Square" — an aesthetics clinic that shares the one
    word "marina" — over "Skypark#Marina Bay Sands Hotel,Singapore.", whose
    name contains the caller's entire query. Both read as "substring"; the
    city pin (Singapore's centroid, 1 km from the clinic and ~1.1 km from
    the hotel, the same km once rounded) settled nothing; prominence
    picked the clinic. So containment in either direction is its own label,
    "contains", ranked between "prefix" and "substring": a name that holds
    every word the caller typed, or a query that holds the candidate's
    whole name ("the Blue Bottle Roastery Austin" vs. "Blue Bottle
    Roastery" — the #22 shape, with the name mid-query rather than leading
    it), beats any one-word coincidence, and the distance/prominence
    tie-breaks then only ever choose among names that actually contain
    each other. A shared significant word stays "substring": still related
    enough to keep, still the weakest literal claim.

    #469: the shared word has to be a *distinctive* one. "Shibuya Station"
    and "Snow Peak Land Station" share "station"; "Yoyogi Park" and "Park
    Hotel Tokyo" share "park". Those words say what kind of thing the
    caller means, and every downtown has hundreds of names carrying them —
    letting one through as relatedness is what resolved the station to a
    sporting-goods store 7 km away and routed a 1.5 km walk as 8.6 km.
    So the generic feature words (_GENERIC_PLACE_WORDS) are set aside from
    the query's side of the overlap first, and the candidate must share
    one of the words that remain. Only when the query is *nothing but*
    generic words ("Station") is there no distinctive word to insist on,
    and the plain overlap rule stands. The containment rules above are
    untouched: a name that contains the whole query, type word and all,
    is still a match.

    context_words are the query's location words — the trailing city
    resolve_place split off to anchor the search ("tokyo" in "Shibuya
    Station Tokyo"). They say where, not which, and are set aside from the
    overlap the same way: "Tokyo Station Beer Stand" shares "tokyo" with
    that query and is no more the answer for it than Snow Peak was.
    """
    n, q = _pkg._normalize_for_match(name), _pkg._normalize_for_match(query)
    if n == q:
        return "exact"
    if _pkg._is_word_prefix(q, n) or _pkg._is_word_prefix(n, q):
        return "prefix"
    if n in q or q in n:
        return "contains"
    n_tokens = set(_pkg._significant_tokens(n))
    q_tokens = set(_pkg._significant_tokens(q))
    q_distinctive = {
        t for t in q_tokens
        if t.strip(".,") not in _pkg._GENERIC_PLACE_WORDS and t not in context_words
    }
    if n_tokens & (q_distinctive or q_tokens):
        return "substring"
    return None



def _type_word_slugs(search_query: str) -> tuple[str, ...]:
    """#469: the category slugs for the generic type word `search_query`
    ends in, or () when it ends in something else (or in a type word with
    no usable category — see _TYPE_WORD_CATEGORIES)."""
    tokens = _pkg._significant_tokens(search_query)
    if not tokens:
        return ()
    return _pkg._TYPE_WORD_CATEGORIES.get(tokens[-1].lower().strip(".,"), ())



# perf: how many of resolve_place's bounded places scans run side by side.
# Each runs on its own cursor (db.isolated_reads) rather than under the
# global connection lock; small because every scan still competes for the
# same S3 bandwidth and DuckDB thread pool, and a query has at most a
# handful of distinctive words.
_PLACE_SCAN_WORKERS = 4


# perf: the label a first-round row must earn for resolve_place to skip its
# per-word scans. "exact" is the one label a single-word scan can never
# beat — a row found by one word alone is at best "prefix" (its name is a
# word-prefix of the query; see _place_match_label), so skipping those
# scans cannot change the top result.
_CONFIDENT_PLACE_LABEL = "exact"



def _find_places_kwargs(**extra) -> dict:
    """The keywords in `extra` that the installed overture.find_places
    accepts. The suite's test doubles answer to the six-parameter pre-#373
    signature (lat, lon, radius_m, category, name, limit); a newer, rarely
    non-default keyword is passed only when the callee can take it — the
    same opt-in idiom as #457's `country_kw`."""
    try:
        params = inspect.signature(overture.find_places).parameters
    except (TypeError, ValueError):  # pragma: no cover - a C callable
        return dict(extra)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(extra)
    return {k: v for k, v in extra.items() if k in params}



def _run_place_scans(jobs: list[Callable[[], list[dict]]]) -> list[list[dict]]:
    """Run independent bounded places scans side by side; results in job
    order, so the caller merges them exactly as a serial loop would have.

    Each worker reads on a private cursor (db.isolated_reads) so the scans
    overlap instead of queueing on overture._conn_lock, and runs under a
    copy of the calling context so trace/progress records land in the
    caller's request. The first job's exception (in job order) is
    re-raised after every job has finished — UpstreamUnavailable from a
    scan propagates exactly as it did from the serial loop.
    """
    if len(jobs) <= 1:
        return [job() for job in jobs]

    def _isolated(job):
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(db.isolated_reads())
            except duckdb.Error:
                # No private cursor to be had (the shared instance could
                # not be opened — offline, httpfs missing): the scan still
                # runs, just serialized on the global lock as before, and
                # whether it can answer is its own business to report.
                _pkg.logger.debug("isolated cursor unavailable; scanning unisolated", exc_info=True)
            return job()

    with ThreadPoolExecutor(max_workers=min(_PLACE_SCAN_WORKERS, len(jobs))) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _isolated, job) for job in jobs
        ]
        return [f.result() for f in futures]



def _type_scan_rows(
    lat: float, lon: float, slugs: tuple[str, ...], token: str
) -> list[dict]:
    """#469: one bounded find_places scan for rows OF the query's kind whose
    name carries its distinctive word — "Shibuya" within train_station.

    Same radius and limit as the per-token scans. find_places' category
    filter is a substring match (so "park" returns parking lots too); rows
    are kept only when their own category is one of `slugs` or sits under
    one in the taxonomy, the exact/hierarchy reading the compose tools use.
    A row with no category at all is kept — a degraded taxonomy column is
    not evidence against it. Each kept row is tagged "_type_scan" for the
    grading in resolve_place.
    """
    # perf: one word of the query within one category — a close-spelling
    # match of it is not the answer, so the fuzzy tier is off (see
    # find_places' fuzzy_fallback); the alt-name tier still runs.
    rows = overture.find_places(
        lat, lon, radius_m=_pkg._RESOLVE_PLACE_RADIUS_M,
        categories=list(slugs), name=token, limit=_RESOLVE_OVERFETCH,
        **_pkg._find_places_kwargs(fuzzy_fallback=False),
    )
    kept = []
    for row in rows:
        cat = row.get("category")
        if cat:
            chain = categories.hierarchy_for(cat) or [cat]
            if not any(slug in chain for slug in slugs):
                continue
        row["_type_scan"] = True
        kept.append(row)
    return kept



def _best_place_label(
    name: str, query: str, alternates: list[str],
    context_words: frozenset[str] = frozenset(),
) -> str | None:
    """The strongest label `name` earns against `query` or any of `alternates`.
    context_words pass through to _place_match_label (#469).

    #429: the alternates are other spellings of the same thing the caller
    asked for — the bundled landmark aliases, and the city-stripped
    search_query — so the label they earn is a label for the caller's own
    question, not a consolation prize. Taking the *first* non-None one
    (the previous rule) meant a literal label, however weak, blocked the
    alternates from ever being tried: "Louvre" reads as a bare substring of
    "Musée du Louvre", so the museum was graded a substring match and lost
    the tier sort to "Louvre Luxury Apartment & SPA", whose name merely
    starts with the word. Graded against the alias spelling "musee du
    louvre" it is an exact match, which is what it actually is.
    """
    best = _pkg._place_match_label(name, query, context_words)
    for alt in alternates:
        label = _pkg._place_match_label(name, alt, context_words)
        if label is not None and (
            best is None or _pkg._MATCH_LABEL_RANK[label] > _pkg._MATCH_LABEL_RANK[best]
        ):
            best = label
    return best



def _jaro_winkler(a: str, b: str) -> float:
    """Plain-Python Jaro-Winkler (0..1), matching DuckDB's
    jaro_winkler_similarity closely enough to share
    _FUZZY_SIMILARITY_THRESHOLD: standard Jaro, then the Winkler common-
    prefix boost (scaling 0.1, prefix capped at 4, applied above 0.7 —
    the same shape as the rapidfuzz implementation DuckDB vendors).

    Exists for the #374 re-score below, where a per-row SQL round-trip per
    candidate would be all overhead: the SQL tiers keep using DuckDB's own
    function.
    """
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(max(la, lb) // 2 - 1, 0)
    a_matched = [False] * la
    b_matched = [False] * lb
    matches = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - window), min(lb, i + window + 1)):
            if not b_matched[j] and b[j] == ca:
                a_matched[i] = b_matched[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = 0
    j = 0
    for i in range(la):
        if a_matched[i]:
            while not b_matched[j]:
                j += 1
            if a[i] != b[j]:
                transpositions += 1
            j += 1
    jaro = (
        matches / la + matches / lb + (matches - transpositions / 2) / matches
    ) / 3
    if jaro <= 0.7:
        return jaro
    prefix = 0
    for ca, cb in zip(a, b):
        if ca != cb or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)



def _fuzzy_place_covers_query(name: str, tokens: list[str]) -> bool:
    """#374: whole-query relatedness re-score for a #373 fallback row that
    was matched against a single TOKEN of a multi-token query.

    The per-token find_places loop in resolve_place means a fuzzy/alt-name
    hit only proved similarity to ONE word — trusting its matched_by label
    outright let a bar named "King" (fuzzy-close to the token "kings")
    resolve the whole query "Kings Barbershop Chicago". Such a row is only
    kept when every distinctive query token is covered by the row's actual
    name: contained in it, or typo-close (same 0.92 threshold as the SQL
    tier) to one of its words. Rows matched against the whole query never
    come through here — their similarity was already scored against
    everything the caller typed.
    """
    folded_name = overture._fold_poi_name(name)
    name_words = folded_name.split()
    for tok in tokens:
        folded_tok = overture._fold_poi_name(tok)
        if not folded_tok or folded_tok in folded_name:
            continue
        if any(
            _pkg._jaro_winkler(word, folded_tok) >= _pkg._FUZZY_SIMILARITY_THRESHOLD
            for word in name_words
        ):
            continue
        return False
    return True



# #344: subtypes a `city` hint is allowed to resolve to without falling
# back to the top (population-ranked) hit. Deliberately narrower than
# _SUBTYPE_WEIGHT's full ladder — a hint named "city" should not silently
# resolve to a neighborhood, which is finer-grained than any caller means
# by "the city".
_CITY_HINT_SUBTYPES = frozenset({"locality", "localadmin"})


# #427: how deep to look for the division a comma qualifier names. Same
# depth resolve_place's own `city` hint uses — enough for _pick_city_hint_row
# to find a city-level hit under a same-named region, not so deep that a
# qualifier turns into a survey.
_ANCHOR_LOOKUP_LIMIT = 10


# #427: how many candidates the anchored division pass asks geocode() for.
# Deliberately far above _RESOLVE_OVERFETCH: geocode ranks by prominence,
# with no idea an anchor is in play, and a name like "Hilltop" or "Le
# Marais" has dozens of homonyms worldwide — the one inside the anchor is
# routinely past the first ten. The rows are already ranked and in memory
# by then, so a deeper page costs a slice, not a scan.
_ANCHORED_OVERFETCH = 50



def _pick_city_hint_row(hits: list[dict]) -> dict:
    """Pick the row a `city` hint means, from geocode()'s ranked hits.

    geocode()'s own ranking (_rank_key) orders same-named divisions by raw
    population, which is the right call for a bare geocode() query — but a
    `city` hint's whole meaning is "a city", and a state/region can share a
    populous namesake with the city it was named after ("New York" the
    state outranks New York City there). Preferring the first city-level
    hit over a broader admin unit resolves that without touching geocode()'s
    general ranking, which other callers (and the ranking corpus) depend on
    exactly as it is. Falls back to the top hit when nothing at the city
    level matched — the hint may genuinely name a country or region.
    """
    for hit in hits:
        if hit.get("type") in _CITY_HINT_SUBTYPES:
            return hit
    return hits[0]



def resolve_place(
    query: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
    limit: int = 3,
    city: str | None = None,
    lang: str | None = None,
    country: str | None = None,
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
    "lat", "lon", "match": "exact" | "prefix" | "contains" | "substring" |
    "fuzzy", plus "admin_context" (division) or "category" (place)}.
    "contains" (#475, places only) means the name contains the whole query
    or the query contains the whole name; "substring" for a place can mean
    as little as one shared significant word, and for a division the usual
    literal substring. "fuzzy" (#215 for divisions, #373 for places) means
    the name doesn't contain the query at all and was reached by close
    spelling instead — the caller asked for one string and is being handed
    the answer to another, so it ranks below every literal label. A place
    candidate reached through #373's fallback tiers (an alt-spelling or
    fuzzy match on the underlying find_places call) additionally carries
    "matched_by": "alt_name" | "fuzzy", absent on an ordinary literal match.
    Ranked by match tier first — kind-agnostic, an exact place beats a
    prefix-matched division — then by prominence
    (division rank_score / place confidence, both roughly 0-1 scales), then
    id for determinism. Never more than `limit` results.

    One exception to tier-first (#481): when the query hit the bundled
    landmark alias table, candidates within _ALIAS_PIN_RADIUS_M of that
    curated pin rank ahead of every other candidate, whatever their tier.
    The alias coordinate is the strongest evidence this module has about
    where the caller means — "notre dame paris" pinned on the cathedral
    must answer the cathedral, not a same-named parish church 4 km away
    that happened to earn a prefix label. Tier, distance and prominence
    still order the pinned rows among themselves; a pin with nothing
    inside its radius changes nothing.

    No match is a valid answer, not an error: an unresolvable query returns
    an empty list. Raises overture.UpstreamUnavailable if a remote scan
    fails after retries, or overture.SchemaDegraded if the places dataset
    is missing bbox — the caller (server.py) turns either into a structured
    error like every other tool.

    #469: a query that ends in a generic type word — "Shibuya Station",
    "Yoyogi Park", "Heathrow Airport" — names a *kind* of feature plus the
    word that picks it out, and Overture files the kind under a category
    rather than in the name: the station is "Gare de Shibuya", category
    train_station, and nothing is literally named "Shibuya Station". So
    alongside the name scans, one bounded find_places scan filtered to that
    kind's categories and to the distinctive word runs from the same
    reference (_TYPE_WORD_CATEGORIES, _type_scan_rows). A row it returns
    whose name covers every distinctive word has answered both halves of
    the query — the category is the type word — and is graded "exact";
    among such rows the one with other rows of its kind within
    _TYPE_SCAN_SUPPORT_RADIUS_M ranks first, because a station is many rows
    (its exits, lines, operators) while a mis-pinned duplicate stands alone.
    The relatedness gate (_place_match_label) meanwhile no longer accepts
    the type word itself as the shared word, so when nothing of the kind is
    found the answer is [] — which server.py turns into `need: location` —
    rather than the nearest business with "Station" in its name.

    lang (#410) requests Overture's language-tagged names.common variant,
    the same as geocode() — but only for "kind": "division" candidates
    (threaded through the internal geocode() call this function already
    makes). "kind": "place" candidates come from find_places, which is out
    of scope for #410 this round (the same scope line find_places' own
    docstring draws), so they always carry their primary name. A division
    candidate's `name_primary` is present under the same rule as
    geocode()'s: only when the variant actually differs from the primary.

    #464: with no near/city hint and no division matching the whole query,
    the reference is a *guess* (_fallback_anchor_candidates' split of the
    caller's own words), and the shared-one-word relatedness gate above is
    too loose next to a guess — "Grand Central Terminal" anchored on a
    Chelyabinsk district called Central and returned a restaurant named
    "Grand Royal". Near a split anchor a place must account for every
    significant word of the query (see _fallback_anchor_details' `strong`
    for which words the anchor itself may account for). When nothing
    survives, the query is treated as the bare name it is: one unanchored,
    LIMIT-capped scan for the whole query, keeping exact/prefix rows only,
    under the same remote-dataset gate as geocode()'s own (#105) — so
    against the live S3 release a bare famous name with no bundled alias
    returns [] (the server answers need: location) rather than a place on
    the wrong continent.

    country (#457) composes with `city`: it constrains the divisions half
    of the merge (threaded through the internal geocode() calls this
    function makes), the same ISO 3166-1 filter geocode() itself takes.
    The places half is left unconstrained by it — find_places has no
    country column to filter on here, and the bbox this function already
    derives from `city`/near_lat/near_lon does the same job for that half.
    May raise ValueError for an unrecognized country or one that conflicts
    with a country/region qualifier parsed off `query` itself.
    """
    query = query.strip()
    limit = max(1, min(limit, _pkg.MAX_LIMIT))
    if not query:
        return []
    if country is not None:
        country = _pkg.normalize_country(country)

    # #329: parse a trailing city / POI alias, or reuse the last good city
    # for a POI-shaped query. Infer *before* the LRU lookup so the key
    # includes the effective hint — otherwise "Observation Tower" after
    # Brooklyn is cached under a bare query and replayed after Paris.
    place_query = query
    city_bounded = False
    # #481: the pin _extract_city_hint hands back comes only from the bundled
    # alias table (a plain city split or a caller's near hint yields None
    # here) — kept separately from near_lat/near_lon because the ranking
    # below treats it differently from any other reference.
    alias_pin: tuple[float, float] | None = None
    if city is None and near_lat is None and near_lon is None:
        place_query, inferred_city, inferred_coords = _pkg._extract_city_hint(query)
        if inferred_coords is not None:
            near_lat, near_lon = inferred_coords
            alias_pin = inferred_coords
            city = inferred_city or city
            city_bounded = True
        elif inferred_city:
            city = inferred_city
        elif _pkg._query_is_poi_shaped(query):
            last_city, last_coords = _pkg._last_good()
            if last_city:
                city = last_city
                if last_coords is not None:
                    near_lat, near_lon = last_coords
                    city_bounded = True

    cache_city, cache_lat, cache_lon = city, near_lat, near_lon
    cached = _pkg._resolve_cache_get(query, cache_city, cache_lat, cache_lon, lang, country)
    if cached is not None:
        return cached[:limit]

    # #271: a caller-supplied (or now inferred) city is the location half
    # of the query, stated rather than guessed at. Resolving it first and
    # using its coordinates as the reference skips _fallback_anchor's
    # whole "which of these words is the place" problem — the problem
    # behind every wrong-hemisphere answer this module has had. It is a
    # hint, never an answer: it bounds where the search looks, and every
    # row returned still comes from the data.
    #
    # #344: geocode()'s general ranking orders same-named divisions by raw
    # population, which is right for a bare geocode() call but wrong here —
    # "New York" the state (20.2M) outranks New York City (8.5M) there, and
    # taking hits[0] anchored "Times Square New York" 200km away, on the
    # state's geographic centroid, with the real Times Square outside the
    # city-hint radius from it. A `city` hint's entire meaning is "a city";
    # _pick_city_hint_row prefers the first locality/localadmin-level hit
    # over a same-named region/country one, falling back to the top hit
    # when nothing at that level matched (the hint may genuinely name a
    # country or region and nothing finer).
    # #457: only pass `country` through when set, so the many callers and
    # test doubles that monkeypatch `geocode` with the pre-#457 signature
    # keep working unchanged; the constraint is opt-in either way.
    country_kw = {"country": country} if country is not None else {}
    if city and near_lat is None and near_lon is None:
        try:
            hits = _pkg.geocode(city, limit=5, **country_kw)
        except (overture.UpstreamUnavailable, overture.SchemaDegraded):
            hits = []
        if hits:
            pin = _pick_city_hint_row(hits)
            near_lat, near_lon = pin["lat"], pin["lon"]
            city_bounded = True
        else:
            _pkg.logger.info("resolve_place: city hint %r did not resolve; ignoring it", city)

    if near_lat is not None and near_lon is not None:
        city_bounded = True

    # Search the place half when we stripped a trailing city, so
    # "Colosseo Roma" does not return Rome the city as the pin.
    search_query = place_query if city_bounded and place_query != query else query
    if city_bounded and near_lat is not None and near_lon is not None:
        # #476: the pin bounds geocode's own search, not just its output.
        # Filtering afterwards (below, kept as belt-and-braces) had already
        # let the unconstrained pass match "Marina Bay Sands" to five
        # "Marina Bay" neighbourhoods in the US by spelling, anchor its
        # places fallback on Marina, California, and schedule that box's
        # tiles for background download — for a query the caller had
        # pinned to Singapore. With the constraint, divisions outside the
        # city-hint radius are never candidates, and geocode anchors its
        # places search on the pin instead of guessing from the words.
        geocode_hits = _pkg.geocode(
            search_query, limit=_RESOLVE_OVERFETCH, lang=lang,
            near=(near_lat, near_lon, _pkg._CITY_HINT_RADIUS_M), **country_kw,
        )
        geocode_hits = [
            r for r in geocode_hits
            if geo.haversine_m(near_lat, near_lon, r["lat"], r["lon"]) <= _pkg._CITY_HINT_RADIUS_M
        ]
    else:
        geocode_hits = _pkg.geocode(search_query, limit=_RESOLVE_OVERFETCH, lang=lang, **country_kw)
    division_hits = [r for r in geocode_hits if r["type"] != "place"]

    # #464: None unless the reference below is a split-derived guess; then
    # the query words a place candidate must cover to count (see there).
    split_cover_tokens: list[str] | None = None
    if near_lat is not None and near_lon is not None:
        reference = (near_lat, near_lon)
    elif division_hits:
        reference = (division_hits[0]["lat"], division_hits[0]["lon"])
    else:
        # No division matched the whole query, but the anchor machinery can
        # usually still say where the query means — "plaza mayor madrid"
        # matches no division as a string, yet its anchor is Madrid's centre.
        # Without this the merged ranking has no distance term for exactly
        # the POI-shaped queries that need one, and answered that query with
        # a Plaza Mayor 25 km out of town (#272). One local-index lookup.
        local_table = _pkg._local_divisions_table()
        options = _pkg._fallback_anchor_details(
            query, [], None, local_table,
            alt_table=_pkg._local_alt_names_table(local_table),
            region_population=_pkg._region_population_lookup(local_table),
        )
        reference = (options[0]["lat"], options[0]["lon"]) if options else None
        if options and options[0]["split"]:
            # #464: the reference is a guess at which of the caller's own
            # words locate the query, and _place_match_label's shared-word
            # gate was written for a reference the caller *stated* (a near
            # hint, a city, a division that matched the whole query). Near a
            # guessed one, "shares a word" is exactly the coincidence that
            # produced the anchor in the first place: "Grand Central
            # Terminal" anchored on a Chelyabinsk district called Central
            # and resolved to "Grand Royal, банкет-холл и ресторан" — a
            # restaurant that shares the word "Grand" with the query and
            # nothing else. So a place found near a split anchor must
            # account for every significant word of the query — in its own
            # name, or (when the anchor is strong: the caller typed a
            # city-level division's name) by sitting in the place the
            # anchor's words named. Same containment-or-typo-close test as
            # #374's cover rule, over the whole query rather than a
            # residual.
            exempt = set(overture._fold_poi_name(options[0]["candidate"]).split()) if (
                options[0]["strong"]
            ) else set()
            split_cover_full = _pkg._significant_tokens(query)
            split_cover_tokens = [
                t for t in split_cover_full if overture._fold_poi_name(t) not in exempt
            ]

    place_rows: list[dict] = []
    gate_tokens: list[str] = []  # #374: set alongside the token loop below
    query_alternates = _pkg._alias_names_for(query) + (
        [search_query] if search_query != query else []
    )
    # #469: the city words split off the query — location context for the
    # gate to set aside, not a word a candidate can be related through.
    context_words = frozenset(
        t.lower() for t in _pkg._significant_tokens(query)
    ) - frozenset(t.lower() for t in _pkg._significant_tokens(search_query))
    # #469: what each of geocode()'s place-kind rows earns from the gate,
    # decided once here — both for the coverage count just below (a row the
    # gate will drop as unrelated is not coverage: before this, ten
    # "...Station" businesses counted as having answered "Shibuya Station"
    # and stood the one scan down that could have) and for the merge
    # further on.
    geocode_place_labels: dict[str, str | None] = {
        r["id"]: _pkg._best_place_label(r["name"], query, query_alternates, context_words)
        for r in geocode_hits
        if r["type"] == "place" and r["id"] and r["name"]
    }
    # geocode()'s own anchored fallback often already searched the places
    # theme near this same reference with the whole query and its tokens —
    # when it came back with enough place-kind rows, re-scanning per token
    # here buys near-duplicates for the price of two more bounded scans
    # ("notre dame paris" measured 11.9s with them, 5s without).
    #
    # "Near this same reference" is load-bearing, not decorative: geocode may
    # have anchored somewhere else entirely — "hoover dam" anchors on Hoover,
    # Alabama, whose %dam% scan returns Adam Cox and Damascus Baptist Church
    # as place hits. Counting those as coverage skipped the one search that
    # would have found the actual dam 200m from the caller's reference.
    geocode_places = sum(
        1 for r in geocode_hits
        if r["type"] == "place"
        and reference is not None
        and geocode_place_labels.get(r["id"]) is not None
        and geo.haversine_m(reference[0], reference[1], r["lat"], r["lon"])
        <= _pkg._RESOLVE_PLACE_RADIUS_M
    )
    if reference is not None:
        ref_lat, ref_lon = reference
        seen_place_ids: set[str] = set()
        # Not every word deserves its own scan (#272). A feature noun
        # ("square") matches half the businesses in any downtown, and the
        # city word the reference was derived FROM ("cambridge") matches
        # everything named after the city — both are pure noise that then
        # outranked real answers, and each costs a bounded scan. Searching
        # "harvard square cambridge" token-by-token means searching
        # "harvard": the one word that distinguishes the place.
        tokens = [
            t for t in _pkg._significant_tokens(search_query)
            if t.lower().strip(".,") not in _pkg._GENERIC_PLACE_WORDS
        ]
        # Phrase first: "harvard square" as one name, not just "harvard"
        # (square is a feature noun and would otherwise be dropped, and
        # the one-word scan then answers Harvard FCU).
        if search_query and search_query.lower() not in {t.lower() for t in tokens}:
            tokens.insert(0, search_query)
        alias_tokens: set[str] = set()
        for alias_name in _pkg._alias_names_for(search_query):
            for tok in alias_name.split():
                if (
                    tok not in tokens
                    and tok.lower().strip(".,") not in _pkg._GENERIC_PLACE_WORDS
                ):
                    tokens.append(tok)
                    alias_tokens.add(tok)
        if len(tokens) > 1:
            folded_city = {t.lower() for t in tokens}
            for div in division_hits[:1]:
                # #410: prune against the *primary* name — under a lang
                # override div["name"] may be the localized variant
                # ("Munich"), and the caller's own city word ("München")
                # must still be recognized and pruned.
                div_name = div.get("name_primary") or div.get("name") or ""
                folded_city &= {w.lower() for w in div_name.split()}
            tokens = [t for t in tokens if t.lower() not in folded_city] or tokens
        # #374: the query words a fallback-matched row must account for —
        # the single distinctive tokens that survived the generic/city
        # pruning above, minus alias-derived ones (an alias is an
        # *alternative* spelling of the query, not extra context the name
        # has to contain too). See _fuzzy_place_covers_query.
        gate_tokens = [t for t in tokens if " " not in t and t not in alias_tokens]
        # #469: the query's kind, searched by category on its distinctive
        # word. Runs regardless of coverage — the name scans cannot reach a
        # row filed under the category rather than the English word,
        # however many rows they return. Skipped when no distinctive word
        # survived the pruning ("Station" alone, or "Tokyo Station" once
        # the city word is set aside): a category scan with nothing to
        # match names against is every station in the metro.
        type_slugs = _type_word_slugs(search_query)
        type_token = gate_tokens[0] if type_slugs and gate_tokens else None
        # perf: these scans used to run one after another, each queued on
        # the global connection lock and each paying tier 1 + alt-name +
        # fuzzy on a miss — the cold half of the c15 corpus leg. Now a
        # first round runs the two scans most likely to settle the query
        # side by side: the whole phrase (all its tiers, as before) and the
        # #469 type scan. When either produced a row the grading below will
        # call _CONFIDENT_PLACE_LABEL, the per-word scans are skipped: a row
        # found by one word alone is at best "prefix", so none of them could
        # have outranked it. Otherwise the second round runs every remaining
        # word side by side with the fuzzy tier off (one word's close
        # spelling is not the answer to a longer query). Results are merged
        # in the serial loop's order — phrase, words, type scan — so the
        # candidate set and ranking are what they were whenever the first
        # round is not confident. Not under an alias pin (#481: the pin, not
        # the label, picks first place) or a guessed anchor (#464: the cover
        # rule may drop the confident row), where every scan runs as before.
        name_tokens = list(tokens) if geocode_places < limit else []
        phrase = name_tokens[0] if name_tokens and name_tokens[0] == search_query else None

        def _name_scan(token: str, fuzzy: bool) -> Callable[[], list[dict]]:
            return lambda: overture.find_places(
                ref_lat, ref_lon, radius_m=_pkg._RESOLVE_PLACE_RADIUS_M,
                name=token, limit=_RESOLVE_OVERFETCH,
                **({} if fuzzy else _pkg._find_places_kwargs(fuzzy_fallback=False)),
            )

        first_round: list[Callable[[], list[dict]]] = []
        if phrase is not None:
            first_round.append(_name_scan(phrase, fuzzy=True))
        if type_token is not None:
            first_round.append(lambda: _type_scan_rows(ref_lat, ref_lon, type_slugs, type_token))
        first_rows = _pkg._run_place_scans(first_round)
        phrase_rows = first_rows.pop(0) if phrase is not None else []
        type_rows = first_rows.pop(0) if type_token is not None else []
        confident = alias_pin is None and split_cover_tokens is None and (
            any(
                r["name"] and not r.get("matched_by")
                and _pkg._best_place_label(r["name"], query, query_alternates, context_words)
                == _pkg._CONFIDENT_PLACE_LABEL
                for r in phrase_rows
            )
            or any(
                r["name"] and _fuzzy_place_covers_query(r["name"], gate_tokens)
                for r in type_rows
            )
        )
        word_tokens = [] if confident else [t for t in name_tokens if t != phrase]
        word_rows = _pkg._run_place_scans([_name_scan(t, fuzzy=False) for t in word_tokens])
        for token, rows in [(phrase, phrase_rows), *zip(word_tokens, word_rows)]:
            for row in rows:
                if row["id"] and row["id"] not in seen_place_ids:
                    seen_place_ids.add(row["id"])
                    if row.get("matched_by"):
                        # Which token the #373 fallback actually matched
                        # this row against — the #374 gate below trusts
                        # the fuzzy label only when that was the whole
                        # query.
                        row["_matched_token"] = token
                    place_rows.append(row)
        for row in type_rows:
            if row["id"] and row["id"] not in seen_place_ids:
                seen_place_ids.add(row["id"])
                place_rows.append(row)
                continue
            # Already in hand from a name scan — carry the tag over so
            # the grading below sees the row for the kind it is.
            for held in place_rows:
                if held["id"] == row["id"]:
                    held["_type_scan"] = True
                    break

    # #481: the alias pin is a curated coordinate for a specific landmark,
    # so look at what actually stands there. The name-filtered scans above
    # can miss it entirely — "Cathédrale Notre-Dame de Paris" is not a LIKE
    # match for "notre dame" or "notre dame cathedral", and when geocode()
    # already came back with enough same-named shops those scans are
    # skipped anyway — leaving nothing inside the pin radius for the
    # pinned-first sort to promote. One nearest-first scan bounded to the
    # pin radius (warm tiles, no name filter); every row still has to earn
    # a label against the query or an alias spelling below, so the shops
    # and bus stops sharing the square are dropped as unrelated, exactly as
    # a per-token hit would be.
    if alias_pin is not None:
        pinned_ids = {r["id"] for r in place_rows}
        for row in overture.find_places(
            alias_pin[0], alias_pin[1], radius_m=_pkg._ALIAS_PIN_RADIUS_M,
            limit=_ALIAS_PIN_SCAN_LIMIT,
        ):
            if row["id"] and row["id"] not in pinned_ids:
                pinned_ids.add(row["id"])
                place_rows.append(row)

    candidates = []
    seen_ids: set[str] = set()
    for r in division_hits:
        if not r["id"] or r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])
        candidate = {
            "id": r["id"], "kind": "division", "name": r["name"],
            "lat": r["lat"], "lon": r["lon"],
            "type": r.get("type"),
            "admin_context": r["admin_context"],
            # #410: label (and therefore rank) off the primary name — the
            # caller's query was written against it, and grading "München"
            # against a lang-swapped "Munich" would demote the correct
            # division to a substring match below coincidentally-named
            # places.
            "match": _division_match_label(
                {**r, "name": r.get("name_primary") or r["name"]}, query, search_query
            ),
            "_prominence": r["rank_score"],
        }
        if r.get("name_primary"):
            # #410: geocode()'s own lang enrichment already applied — just
            # carry it through the merge rather than re-deriving it.
            candidate["name_primary"] = r["name_primary"]
        candidates.append(candidate)
    for r in place_rows:
        if not r["id"] or r["id"] in seen_ids or not r["name"]:
            continue
        # #373: a row overture.find_places tagged as an alt-name/fuzzy
        # fallback hit doesn't contain the typo the caller typed and would
        # be dropped as unrelated by _place_match_label's containment
        # check. But the tag only certifies similarity to the STRING IT WAS
        # SEARCHED WITH (#374): trusted outright only when that was the
        # whole query; a row matched against one token of a multi-token
        # query must additionally cover the rest of the query's distinctive
        # words, or a one-token fuzzy coincidence (a bar named "King")
        # would resolve the whole of "Kings Barbershop Chicago".
        if r.get("matched_by"):
            matched_token = (r.get("_matched_token") or "").lower()
            if matched_token in {query.lower(), search_query.lower()}:
                label = "fuzzy"
            elif _fuzzy_place_covers_query(r["name"], gate_tokens):
                label = "fuzzy"
            else:
                continue
        else:
            label = _pkg._best_place_label(r["name"], query, query_alternates, context_words)
        # #469: a row of the query's own kind whose name covers every
        # distinctive word has matched the whole query — the category
        # stands in for the type word ("Gare de Shibuya" + train_station is
        # "Shibuya Station"). Graded exact so it outranks every name-only
        # reading, however that reading was labelled.
        if r.get("_type_scan") and _fuzzy_place_covers_query(r["name"], gate_tokens):
            label = "exact"
        if label is None:
            continue
        if split_cover_tokens is not None and not _fuzzy_place_covers_query(
            r["name"], split_cover_tokens
        ):
            continue  # #464: shares a word with the query, near a guessed anchor
        seen_ids.add(r["id"])
        candidate = {
            "id": r["id"], "kind": "place", "name": r["name"],
            "lat": r["lat"], "lon": r["lon"],
            "category": r["category"],
            "match": label,
            "_prominence": r.get("confidence") or 0.0,
            "_type_scan": bool(r.get("_type_scan")),
        }
        if r.get("matched_by"):
            candidate["matched_by"] = r["matched_by"]
        candidates.append(candidate)
    # geocode() can resolve a place on its own — its anchored fallback
    # handles "Shibuya Crossing Tokyo" by splitting the trailing city off
    # and searching places around it. Discarding those hits (the previous
    # behavior kept only division hits) meant resolve_place returned
    # nothing for a query geocode could answer: a place resolver that
    # loses to the plain geocoder on place queries. Merge them in with the
    # same relatedness gate the near-reference path uses.
    for r in geocode_hits:
        if r["type"] != "place" or not r["id"] or r["id"] in seen_ids or not r["name"]:
            continue
        label = geocode_place_labels.get(r["id"])
        if label is None:
            continue
        if split_cover_tokens is not None:
            # #464: geocode()'s fallback guessed an anchor the same way this
            # function did ("Mall of America" -> a Virginia division named
            # "...of America", whose %Mall% scan returned "Mercy Mall of
            # VA"); its rows are held to the same whole-query rule. The
            # anchor's own words are only accounted for by *location* for a
            # row that is actually near this function's anchor — geocode()
            # may have picked a different namesake (traced: a Bolivian
            # hamlet named America here, Virginia there), and a row 6,000 km
            # from the anchor whose word it is excused from containing is
            # excused from nothing.
            near_anchor = reference is not None and (
                geo.haversine_m(reference[0], reference[1], r["lat"], r["lon"])
                <= _pkg._PLACES_FALLBACK_RADIUS_M
            )
            required = split_cover_tokens if near_anchor else split_cover_full
            if not _fuzzy_place_covers_query(r["name"], required):
                continue
        seen_ids.add(r["id"])
        candidates.append({
            "id": r["id"], "kind": "place", "name": r["name"],
            "lat": r["lat"], "lon": r["lon"],
            "category": r.get("category"),
            "match": label,
            "_prominence": r.get("rank_score") or 0.0,
        })

    if split_cover_tokens is not None and not candidates:
        # #464: the guessed anchor explained nothing — the query is a bare
        # name, and the right thing to do with a bare name is what the
        # #83 docstring already promises one: the unanchored, LIMIT-capped
        # scan of the places theme for the WHOLE query. Only exact/prefix
        # rows count here: the scan is a substring ILIKE, and the one
        # thing this path must never do is hand back "Mercy Mall of VA"
        # for "Mall of America" because both contain "Mall" — no result
        # (and the server's need: location) beats a fast wrong one. Gated
        # exactly as geocode()'s own unanchored scan is (#105): against a
        # remote dataset the scan is a full read of the largest theme, so
        # it does not run and the caller is asked for a location instead.
        reference = None
        if not _pkg._skip_unanchored_places_scan():
            for r in _pkg._query_places_fallback(query):
                if not r["id"] or r["id"] in seen_ids or not r["name"]:
                    continue
                tier = _pkg._match_tier(r["name"], query)
                if tier < _pkg._STRONG_TIER:
                    continue
                seen_ids.add(r["id"])
                candidates.append({
                    "id": r["id"], "kind": "place", "name": r["name"],
                    "lat": r["lat"], "lon": r["lon"],
                    "category": r.get("_category"),
                    "match": _MATCH_TIER_LABELS[tier],
                    "_prominence": r.get("_confidence") or 0.0,
                })

    # Distance to the reference before prominence (#272): the reference is
    # the caller's own statement of where they mean (their near-hint, city
    # hint, or the resolved anchor), and names repeat — "plaza mayor madrid"
    # anchored dead-centre on Madrid and still answered with a Plaza Mayor
    # 25 km out, because that row carried more confidence and this sort had
    # no distance term. Same judgment _rank_place applies in geocode's own
    # fallback, applied to the merged list. Km-rounded so GPS-grade jitter
    # never reorders genuinely co-located candidates.
    #
    # #469: among the rows the type scan returned, agreement before distance.
    # The reference is the *city's* pin, and at metro scale the row nearest
    # it is whichever duplicate happens to be mis-pinned toward downtown; a
    # row with other rows of its kind within _TYPE_SCAN_SUPPORT_RADIUS_M is
    # the real complex. Name-scan rows carry no support and are unaffected
    # relative to one another.
    typed = [c for c in candidates if c.get("_type_scan")]
    for c in candidates:
        c["_support"] = sum(
            1 for other in typed
            if other is not c
            and geo.haversine_m(c["lat"], c["lon"], other["lat"], other["lon"])
            <= _TYPE_SCAN_SUPPORT_RADIUS_M
        ) if c.get("_type_scan") else 0

    # #481: when the pin came from the bundled alias table, that pin is a
    # curated landmark coordinate — the strongest evidence the server has
    # about where the query means — and a string-tier heuristic must not
    # outvote it. "notre dame paris" pinned 30 m from the cathedral and still
    # answered Notre Dame de Clignancourt 4.4 km north, because the parish
    # church's name *starts with* the query (prefix) while the cathedral's
    # only contains it (substring), and tier sorts before distance. Every
    # candidate within _ALIAS_PIN_RADIUS_M of the pin is tagged and sorts
    # ahead of all others; tier → distance → prominence still decide among
    # the pinned rows (the cathedral over the museum named after it). When
    # nothing sits inside the radius — the alias points at something the
    # scans missed — no row is tagged and the ordering is unchanged.
    if alias_pin is not None:
        for c in candidates:
            if (
                geo.haversine_m(alias_pin[0], alias_pin[1], c["lat"], c["lon"])
                <= _pkg._ALIAS_PIN_RADIUS_M
            ):
                c["_alias_pinned"] = True

    def _rank_candidate(c):
        near_km = 0.0
        if reference is not None:
            near_km = round(
                geo.haversine_m(reference[0], reference[1], c["lat"], c["lon"]) / 1000.0
            )
        return (
            0 if c.get("_alias_pinned") else 1,
            -_pkg._MATCH_LABEL_RANK[c["match"]],
            -c["_support"],
            near_km,
            -c["_prominence"],
            c["id"],
        )

    candidates.sort(key=_rank_candidate)
    for c in candidates:
        del c["_prominence"]
        del c["_support"]
        c.pop("_type_scan", None)
        c.pop("_alias_pinned", None)
    out = candidates[:limit]
    # The whole ranked list, not `out`: the key carries no limit, and a
    # later call with a larger limit slices the cached list on read.
    _pkg._resolve_cache_put(query, cache_city, cache_lat, cache_lon, candidates, lang, country)
    if out:
        _pkg._remember_last_city(city, out[0])
        _pkg._kick_autowarm(out[0])
    return out



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
        r for r in _pkg.geocode(area, limit=_RESOLVE_OVERFETCH)
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
        r for r in _pkg.geocode(query, limit=_RESOLVE_OVERFETCH)
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
        hits = _pkg.resolve_place(query, limit=_RESOLVE_OVERFETCH)
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
            h for h in _pkg.geocode(candidate, limit=_ANCHOR_LOOKUP_LIMIT)
            if h.get("type") != "place"
            and h.get("lat") is not None
            and h.get("lon") is not None
            and _names_qualifier(h, folded)
        ]
        if hits:
            pin = _pick_city_hint_row(hits)
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
        r for r in _pkg.geocode(head, limit=_ANCHORED_OVERFETCH)
        if r.get("lat") is not None and r.get("lon") is not None and _inside_anchor(r, anchor)
    ]
    if rows:
        return _pick_named_place(query, rows)

    # No division inside the anchor. The neighborhood spellings this fix
    # exists for ("Le Marais") live in the places theme, not the divisions
    # one, so the anchored places search is the answer rather than a
    # consolation prize — bounded by the same anchor, never worldwide.
    anchored = _pkg.resolve_place(
        head, near_lat=anchor["lat"], near_lon=anchor["lon"], limit=_RESOLVE_OVERFETCH
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
