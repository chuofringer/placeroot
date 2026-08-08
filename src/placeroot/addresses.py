"""address_at: the nearest Overture address points to a coordinate (issue #188).

geocode.py's `_nearest_address` already queries this theme, but collapses it
to a single hop inside reverse_geocode's answer. That is the right shape for
"what is this coordinate called"; it is the wrong shape for "which addresses
are here" — a doorway lookup wants the few candidates around the point, with
the unit/postcode/postal_city attributes reverse_geocode drops. This module
generalizes that query to a ranked top-N and adds the one thing a bare
nearest-address query cannot express honestly: coverage.

--- Coverage is the crux ---

addresses is Overture's only non-GA theme (alpha as of release 2026-07-22.0)
and, unlike places/divisions/buildings, it is not global: 474M points across
COVERED_COUNTRIES, which is 39 countries. There is no UK, Ireland, India,
China, Korea, Russia; no Africa, no Middle East; and most of Latin America
outside Brazil, Mexico, Chile, Colombia and Uruguay. A query in any of those
finds nothing — and an empty list, on its own, reads as "there are no
addresses here", which is the opposite of true for a London high street.

So an empty answer is never a bare `[]`: the point's country is resolved
from the divisions theme (the same theme admin_lookup and reverse_geocode
already read) and the response carries a note saying either "this country
isn't in the addresses theme at all" or "this country is covered but nothing
was found within the search radius". Both are structured, non-error results —
"no data" is a real answer to a spatial question, not a failure.

The country behind that note is resolved by *containment* — the same
ST_Contains-over-division_area test admin_lookup runs — and not by taking the
nearest labelled division point. The difference is not academic: division
points sit kilometres apart, so a nearest-point answer is wrong wherever the
closest label happens to belong to the other side of a border, which is a
km-scale error band rather than a metres-one, and lands squarely on HK/CN,
SG/MY, PL/BY, FI/RU and EE/RU. Nearest-division survives only as a fallback
for coordinates no polygon contains at all (offshore, mostly). A third
outcome exists and is reported distinctly: the lookup can simply *fail*, and
that is worded as our scan failing, never as a fact about the world.

--- No id ---

Deliberately absent from every row: the address point's `id`. Overture
documents address ids as NOT GERS-stable — they are matched by identical
value rather than by a stable matcher, so an id can change between releases
for what a human would call the same doorway. Every other id this server
hands out (places, divisions, buildings) is a durable handle an agent can
store and look up again; handing out an address id alongside them would
imply a durability this theme does not have.

--- Alpha schema churn ---

Only bbox (no bbox, no distance) and street (no street, nothing to name an
address with) are essential; every other column degrades to NULL and is
reported in `degraded_fields`, so a mid-alpha column rename costs attributes
rather than the whole tool.

--- Tile cache ---

Address scans go through cache.py's tile cache under theme "addresses", the
same way places/buildings/land_use do (issue #189) — a repeat query in an
already-materialized area reads local parquet instead of re-paying the ~5s
S3 scan. `_from_source` is this module's copy of the three-line pattern each
themed module carries (overture._from_source hardcodes type_="place"); it is
also what geocode._nearest_address calls, so reverse_geocode's address hop
and address_at share one data path and one set of tiles.

Tiles are materialized SELECT *, as for every other theme. The theme is 7x
the rows of places (474M vs 68.7M), which raised the fair question of
whether a per-theme, column-pruned materialization was needed to keep tiles
inside cache.DEFAULT_MAX_MB. Measured against release 2026-07-22.0, it is
not: the densest tile we have (tx=-74, ty=40 — Manhattan and most of the
NYC metro) is 124.9 MB / 1,994,339 address points, against 113.6 MB for the
*places* tile covering the identical square. Copying only the eight columns
this module selects produced a byte-identical 124.9 MB, because an address
point carries almost nothing else — the row count is high but each row is
tiny, and pruning a schema that is already narrow buys nothing. So there is
no per-theme column list to keep in sync with REQUIRED_COLUMNS, and the
existing LRU cap governs addresses tiles exactly as it governs every other
theme's.
"""

import logging
from typing import NamedTuple

import duckdb

from placeroot import cache, db, geo, overture, release

logger = logging.getLogger(__name__)

THEME = "addresses"
TYPE_ = "address"

DEFAULT_LIMIT = 3
# Capped low on purpose: the useful answer to "what is at this coordinate" is
# the handful of doorways around it. Past that it stops being an answer and
# starts being a data dump of a street.
MAX_LIMIT = 5

# Widened until `limit` rows are found. Address points sit metres apart in a
# covered city, so the first radius answers nearly every real query; the wider
# steps are what keep a rural point in a covered country from coming back
# empty just because its nearest doorway is down the road.
SEARCH_RADII_M = (200, 1000, 5000)

# Radii for the *fallback* nearest-division country lookup, only reached when
# the point-in-polygon pass finds no containing division at all (a coordinate
# offshore, or a dataset without division_area geometry). Wider than the
# address radii because divisions are sparse.
_COUNTRY_RADII_M = (2000, 20000, 100000)

REQUIRED_COLUMNS = [
    "bbox",
    "street",
    "number",
    "unit",
    "postcode",
    "postal_city",
    "address_levels",
    "country",
]
ESSENTIAL_COLUMNS = {"bbox", "street"}

# Optional row fields dropped from a result row when null, rather than
# emitted as explicit nulls — most address points carry only some of these,
# and a row of `"unit": null, "postal_city": null` is padding, not an answer.
_OMIT_IF_NULL = ("unit", "postcode", "postal_city", "address_levels")

# Attribute columns selected, in the order _row_to_result unpacks them
# (distance_m is appended by the query itself and is not a source column).
_SELECT_COLUMNS = (
    "number", "street", "unit", "postcode", "postal_city", "address_levels", "country",
)

# ISO 3166-1 alpha-2 codes of the countries the addresses theme actually
# covers, per Overture's own coverage list for release 2026-07-22.0
# (https://docs.overturemaps.org/guides/addresses/). Coverage inside a listed
# country is not necessarily complete — Overture flags the US, Germany and
# Taiwan as partial — so membership here means "this theme has data for this
# country", not "every doorway in it is present".
COVERED_COUNTRIES = frozenset({
    "AT", "AU", "BE", "BR", "CA", "CH", "CL", "CO", "CZ", "DE",
    "DK", "EE", "ES", "FI", "FO", "FR", "GL", "HK", "HR", "IS",
    "IT", "JP", "LI", "LT", "LU", "LV", "MX", "NL", "NO", "NZ",
    "PL", "PT", "RS", "SG", "SI", "SK", "TW", "US", "UY",
})

# Dependent territories that Overture's divisions theme labels with their own
# ISO 3166-1 code, but whose address points are carried in the *parent*
# country's data and tagged with the parent's code. Without this map a query
# in, say, Martinique resolves country "MQ", finds MQ absent from
# COVERED_COUNTRIES and reports "no coverage" for a territory that has tens of
# thousands of address points.
#
# Every entry below was verified against release 2026-07-22.0 by counting
# address rows in the territory's main settlement — they are the territories
# that actually have data, not every territory that theoretically could:
#   MQ/GP/RE/GF/YT/NC/PF/PM/BL/MF -> FR, AX -> FI, NF -> AU.
# Deliberately absent, because the same check found *zero* address rows for
# them: PR, VI, GU, MP, AS (US), and AW/CW/BQ (NL). Mapping those to their
# parent would claim coverage the theme does not have — the opposite error,
# and the worse one, since it turns an honest "no data here" into a silent
# empty list. The territory's own code and name are still what the response
# reports; this mapping only decides which coverage sentence is true.
_TERRITORY_PARENT = {
    "MQ": "FR", "GP": "FR", "RE": "FR", "GF": "FR", "YT": "FR",
    "NC": "FR", "PF": "FR", "PM": "FR", "BL": "FR", "MF": "FR",
    "AX": "FI",
    "NF": "AU",
}


def _is_covered(code: str) -> bool:
    """Does the addresses theme carry data for this ISO code's country?

    Resolves dependent territories to the parent whose feed carries their
    points first — see _TERRITORY_PARENT.
    """
    return _TERRITORY_PARENT.get(code, code) in COVERED_COUNTRIES


def _upstream_glob() -> str:
    return overture._upstream_glob(THEME, type_=TYPE_)


def _from_source(bbox: tuple[float, float, float, float]) -> str:
    """SQL FROM-clause source for an addresses query: local cache tiles, or upstream.

    Mirrors buildings._from_source / land_use._from_source, pinned to this
    module's theme+type — see the module docstring's "Tile cache" section for
    why it can't reuse overture._from_source (which hardcodes type_="place").

    Public to the package rather than to this module alone: geocode's
    reverse_geocode address hop calls it too, so both address readers share
    one set of tiles.
    """
    upstream = _upstream_glob()
    if cache.enabled():
        try:
            with db.conn_lock:
                paths = cache.local_paths_for_query(
                    db.shared_conn(), release.resolve_release(), THEME, bbox, upstream,
                    db.new_connection,
                )
        except duckdb.Error as e:
            raise overture.UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


def _check_schema(glob: str) -> list[str]:
    """Missing REQUIRED_COLUMNS for glob, raising SchemaDegraded if any are essential.

    Note the deliberate difference from reverse_geocode, which silently
    degrades to a divisions-only answer when the addresses theme is missing
    street: reverse_geocode has a second, independent source to fall back on,
    and address_at does not — the addresses theme *is* the answer here, so a
    dataset that cannot produce one is reported rather than papered over.
    """
    missing = overture.missing_columns(glob, REQUIRED_COLUMNS)
    essential_missing = [c for c in missing if c in ESSENTIAL_COLUMNS]
    if essential_missing:
        raise overture.SchemaDegraded(essential_missing)
    return missing


def degraded_fields() -> list[str]:
    """Non-essential REQUIRED_COLUMNS missing from the active addresses dataset."""
    missing = overture.missing_columns(_upstream_glob(), REQUIRED_COLUMNS)
    return [c for c in missing if c not in ESSENTIAL_COLUMNS]


def _column_expr(column: str, missing: set[str]) -> str:
    """`column` if the dataset has it, else a typed NULL under the same name.

    address_levels is flattened from Overture's list-of-structs to a plain
    list of values on the way out: the struct wrapper carries no information
    a caller can use and roughly doubles the field's token cost.
    """
    if column in missing:
        return f"NULL AS {column}"
    if column == "address_levels":
        return "list_transform(address_levels, x -> x.value) AS address_levels"
    return column


def _row_to_result(row: tuple) -> dict:
    """One query row -> the response shape. No `id` — see the module docstring."""
    number, street, unit, postcode, postal_city, address_levels, country, distance_m = row
    result = {
        "number": number,
        "street": street,
        "unit": unit,
        "postcode": postcode,
        "postal_city": postal_city,
        # DuckDB hands a NULL list back as None; an all-null list_transform
        # result is equally uninformative, so both collapse to absent.
        "address_levels": [v for v in address_levels if v] if address_levels else None,
        "country": country,
        "distance_m": distance_m,
    }
    for field in _OMIT_IF_NULL:
        if not result[field]:
            del result[field]
    return result


def _scan_addresses(
    columns: str, lat: float, lon: float, radius_m: int, limit: int
) -> list[tuple]:
    """One radius pass: the `limit` nearest address rows within radius_m.

    Split out of the widening loop so the loop can decide, between passes,
    whether widening is worth another remote scan at all (see address_at).

    Reads through the tile cache when one covers this bbox — hence no `glob`
    parameter: _from_source resolves the source per bbox, and a widening loop
    is exactly the case where the second pass should reuse what the first
    materialized.
    """
    bbox_filter, distance_filter, params, bbox, _radius = overture.area_geometry(
        lat, lon, radius_m
    )
    sql = f"""
        SELECT {columns},
               round({overture.DISTANCE_EXPR}, 1) AS distance_m
        FROM {_from_source(bbox)}
        WHERE {bbox_filter} AND {distance_filter}
        ORDER BY distance_m, street NULLS LAST, number NULLS LAST
        LIMIT {limit}
    """
    try:
        with overture._conn_lock:
            return overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e


# --- Which country is this? ------------------------------------------------
#
# Three-state on purpose. "We looked and no division covers this point" (an
# ocean coordinate, or a dataset with no divisions in range) and "the lookup
# itself broke" are different answers, and the note has to say which: the
# first is a fact about the data, the second is a fact about our scan, and
# printing the second as the first is how a transient S3 error turns into a
# confident, wrong statement about the world.
RESOLVED = "resolved"
NOT_FOUND = "not_found"
LOOKUP_FAILED = "lookup_failed"


class Country(NamedTuple):
    """Outcome of the containing-country lookup behind a coverage note."""

    status: str
    code: str | None = None
    name: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.code})" if self.name else str(self.code)


# division_area rows whose names.primary is the name of the country-level
# entity. Overture files dependent territories (Martinique, Guam, Aland) as
# subtype "dependency" rather than "country", and those are exactly the rows
# _TERRITORY_PARENT cares about, so both count.
_COUNTRY_SUBTYPES = ("country", "dependency")

# Cap on the containing-division rows the coverage lookup reads back. Division
# areas nest — country > region > county > locality — and a point sits inside
# one polygon per level, so a handful of rows is the real shape of this result;
# the limit only stops a pathological dataset from streaming an unbounded
# result set into memory for a note nobody reads more than one line of. It is
# applied with ORDER BY area ASC, which keeps the *most specific* rows — the
# ones _country_by_containment actually chooses between.
_CONTAINMENT_ROW_LIMIT = 32


def _country_by_containment(lat: float, lon: float) -> Country:
    """The country actually *containing* the point, via ST_Contains on division_area.

    The same point-in-polygon path divisions.admin_lookup runs, narrowed to
    the country columns: bbox prunes remote row groups, ST_Contains decides.

    Division areas nest, so a point is inside several of them at once; the
    answer is the most specific country-or-dependency-level one — code and
    name together, off a single row. See the comment at the return.
    """
    try:
        db.ensure_spatial()
    except duckdb.Error as e:
        logger.warning("spatial extension unavailable for coverage lookup: %s", e)
        return Country(LOOKUP_FAILED)
    glob = overture.upstream_glob(theme="divisions", type_="division_area")
    missing = set(
        overture.missing_columns(glob, ["country", "geometry", "bbox", "names", "subtype"])
    )
    if "country" in missing or "geometry" in missing:
        return Country(LOOKUP_FAILED)
    geom = geo.geom_expr(glob)
    name_expr = "NULL" if "names" in missing else "names.primary"
    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    bbox_prefilter = (
        "bbox.xmin <= $lon AND bbox.xmax >= $lon"
        " AND bbox.ymin <= $lat AND bbox.ymax >= $lat AND "
        if "bbox" not in missing
        else ""
    )
    sql = f"""
        SELECT country, {name_expr} AS name, {subtype_expr} AS subtype,
               ST_Area({geom}) AS area
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE {bbox_prefilter}country IS NOT NULL
          AND ST_Contains({geom}, ST_Point($lon, $lat))
        ORDER BY area ASC
        LIMIT {_CONTAINMENT_ROW_LIMIT}
    """
    try:
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, {"lat": lat, "lon": lon}).fetchall()
    except duckdb.Error as e:
        logger.warning("containment country lookup for address coverage failed: %s", e)
        return Country(LOOKUP_FAILED)
    if not rows:
        return Country(NOT_FOUND)
    # Code and name must come off the SAME row. Division areas nest, and a
    # dependency sits inside its parent country's polygon: taking the code
    # from the smallest containing row (AX) and the name from the largest
    # country-level one (Finland) built the label "Finland (AX)" — two
    # different places in one parenthesis, and the coverage sentence built
    # from it then said Finland was covered while checking Aland's code.
    #
    # So: the *most specific* country-or-dependency-level row containing the
    # point, which is the entity the code identifies. Rows are sorted by area
    # ascending, so that is the first match, not the last.
    named = [r for r in rows if r[2] in _COUNTRY_SUBTYPES and r[1]]
    if named:
        return Country(RESOLVED, named[0][0], named[0][1])
    # No named country-level row — a dataset without names/subtype, say.
    # Keep the old code (smallest containing row) and no label rather than
    # borrowing a sub-country row's name, which would put a county in the
    # sentence that is supposed to name a country.
    return Country(RESOLVED, rows[0][0], None)


def _country_by_nearest(lat: float, lon: float) -> Country:
    """Fallback: the nearest type=division row's country, at widening radii.

    An approximation — it answers with whichever labelled division point is
    closest, which near a border can be the wrong side of it — so it only
    runs when containment found nothing to be exact about.
    """
    glob = overture.upstream_glob(theme="divisions", type_="division")
    missing = set(overture.missing_columns(glob, ["country", "hierarchies"]))
    if "country" in missing:
        return Country(LOOKUP_FAILED)
    # hierarchies[1] is the first containing-chain path, top-level ancestor
    # (the country) first — the same structure geocode._admin_context reads.
    name_expr = "NULL" if "hierarchies" in missing else "hierarchies[1][1].name"
    for radius_m in _COUNTRY_RADII_M:
        bbox_filter, distance_filter, params, _bbox, _radius = overture.area_geometry(
            lat, lon, radius_m
        )
        sql = f"""
            SELECT country, {name_expr} AS country_name,
                   round({overture.DISTANCE_EXPR}, 1) AS distance_m
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND {distance_filter} AND country IS NOT NULL
            ORDER BY distance_m
            LIMIT 1
        """
        try:
            with overture._conn_lock:
                row = overture.conn().execute(sql, params).fetchone()
        except duckdb.Error as e:
            logger.warning("nearest-division country lookup failed: %s", e)
            return Country(LOOKUP_FAILED)
        if row:
            return Country(RESOLVED, row[0], row[1])
    return Country(NOT_FOUND)


def _country_at(lat: float, lon: float) -> Country:
    """Which country contains this coordinate, for the coverage note.

    Containment first (exact), nearest-division second (approximate) and only
    when containment has nothing to say. The fallback's verdict is never
    upgraded into a claim containment did not support: if containment merely
    *errored*, a fallback miss stays LOOKUP_FAILED rather than becoming
    NOT_FOUND, because "no division covers this point" was never established.
    """
    contained = _country_by_containment(lat, lon)
    if contained.status == RESOLVED:
        return contained
    nearest = _country_by_nearest(lat, lon)
    if nearest.status == RESOLVED:
        return nearest
    return contained if contained.status == NOT_FOUND else Country(LOOKUP_FAILED)


def _coverage_note(country: Country) -> str:
    """Why an address query came back empty — never left to an unexplained []."""
    widest = SEARCH_RADII_M[-1]
    if country.status == LOOKUP_FAILED:
        return (
            f"no address point within {widest} m. The divisions lookup that would say "
            "which country this coordinate is in did not complete (an upstream or "
            "dataset problem on our side, not a statement about the data), so address "
            "coverage could not be checked here — retrying may resolve it."
        )
    if country.status == NOT_FOUND:
        return (
            f"no address point within {widest} m, and no division in the active dataset "
            "identifies this coordinate's country, so its address coverage could not be "
            "checked. Overture's addresses theme is alpha and covers "
            f"{len(COVERED_COUNTRIES)} countries, so an empty result may mean no data "
            "rather than no addresses."
        )
    label = country.label
    if not _is_covered(country.code):
        return (
            f"no Overture address coverage for {label}. The addresses theme is alpha and "
            f"carries data for {len(COVERED_COUNTRIES)} countries; this is not one of "
            "them, so this empty result means no data, not no addresses. Try "
            "reverse_geocode, which falls back to the containing admin areas."
        )
    parent = _TERRITORY_PARENT.get(country.code)
    via = f", whose address points are carried under {parent}" if parent else ""
    return (
        f"no address point within {widest} m of this coordinate. {label} is covered by "
        f"Overture's addresses theme{via}, but coverage inside a covered country is "
        "partial, so this may be a gap in the data rather than an unaddressed location."
    )


def address_at(lat: float, lon: float, limit: int = DEFAULT_LIMIT) -> dict:
    """The nearest Overture address points to a coordinate, nearest first.

    Returns {"results": [{number, street, unit?, postcode?, postal_city?,
    address_levels?, country, distance_m}, ...]} and, whenever `results` is
    empty, a "note" saying why — see _coverage_note. `degraded_fields` is
    added when the active dataset is missing non-essential columns.

    The radius widening stops early when the first (narrowest, cheapest) pass
    finds nothing *and* the coordinate turns out to be in a country the theme
    does not carry at all: 5 km cannot conjure data that does not exist in the
    dataset, so the answer is already known and the two wider remote scans are
    pure latency. Covered countries still widen exactly as before.

    Raises overture.SchemaDegraded if the dataset lacks bbox or street, and
    overture.UpstreamUnavailable if the remote scan fails; server.py maps
    both to structured errors like every other tool.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    glob = _upstream_glob()
    missing = set(_check_schema(glob))
    columns = ", ".join(_column_expr(c, missing) for c in _SELECT_COLUMNS)

    rows: list[tuple] = []
    country: Country | None = None
    for radius_m in SEARCH_RADII_M:
        rows = _scan_addresses(columns, lat, lon, radius_m, limit)
        if len(rows) >= limit:
            break
        if not rows and country is None:
            country = _country_at(lat, lon)
            if country.status == RESOLVED and not _is_covered(country.code):
                break

    payload: dict = {"results": [_row_to_result(r) for r in rows]}
    if not rows:
        if country is None:  # only reachable if SEARCH_RADII_M is ever emptied
            country = _country_at(lat, lon)
        payload["note"] = _coverage_note(country)
    degraded = degraded_fields()
    if degraded:
        payload["degraded_fields"] = degraded
    return payload
