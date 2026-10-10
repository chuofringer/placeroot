"""Postcode-shaped queries: patterns, per-country postcode lookups and notes."""

import math
import re
import sys as _sys

import duckdb

_pkg = _sys.modules["placeroot.geocode"]


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
    re.compile(r"^\d{4}$"),  # AT BE AU CH DK HU LU NO NZ SI ...
    re.compile(r"^\d{5}$"),  # US ZIP, DE, FR, ES, IT, FI, MX
    re.compile(r"^\d{6}$"),  # SG
    re.compile(r"^\d{5}-\d{4}$"),  # US ZIP+4
    re.compile(r"^\d{5}-\d{3}$"),  # BR CEP
    re.compile(r"^\d{4}-\d{3}$"),  # PT
    re.compile(r"^\d{4} ?[A-Z]{2}$"),  # NL 1011AB / 1011 AB
    re.compile(r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$"),  # CA M5V 3L9
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
    glob = _pkg.addresses._upstream_glob()
    key = (glob, tuple(variants))
    cached = _pkg._POSTCODE_AGGREGATE_CACHE.get(key)
    if cached is not None:
        return cached
    cols = _pkg.overture.probe_schema(glob)
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
        with _pkg.overture.read_conn() as rc:
            rows = rc.execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise _pkg.overture.UpstreamUnavailable(str(e)) from e
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
        with _pkg.overture.read_conn() as rc:
            row = rc.execute(sql, params).fetchone()
    except duckdb.Error as e:
        _pkg.logger.warning("local divisions lookup for postcode locality failed: %s", e)
        return None
    if row is None or row[3] > _pkg._POSTCODE_LOCALITY_MAX_M:
        return None
    return {
        "name": row[0],
        "admin_context": [*_pkg._admin_chain_context(row[2], self_name=row[0]), row[0]],
    }


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
        results.append(
            {
                "name": display,
                "type": "postcode",
                "lat": lat,
                "lon": lon,
                "id": None,
                "admin_context": covering["admin_context"] if covering else [],
                "rank_score": round(count / top, 3),
                "country": country,
                "address_count": count,
            }
        )
    return results


def _postcode_coverage_sentence() -> str:
    covered = len(_pkg.addresses.COVERED_COUNTRIES)
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
    if not _pkg._is_remote(_pkg.addresses._upstream_glob()):
        return ""
    return (
        " This is an unindexed scan of the whole addresses theme, so the first "
        "such query in a session costs ~12s."
    )


def _postcode_note(display: str) -> str:
    """Note accompanying a postcode answer that found something."""
    return (
        f'"{display}" was read as a postcode, not a name: one aggregate over '
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
        f'"{display}" is postcode-shaped, but no address point in Overture '
        f"carries it -- which is not the same as it not existing: "
        f"{_postcode_coverage_sentence()}. So a postcode in the UK, Italy or "
        f"Japan comes back empty here whether or not it is real. The name "
        f"search was run too and also found nothing."
        f"{_postcode_cold_scan_sentence()}"
    )
