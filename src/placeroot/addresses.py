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

Not routed through cache.py's tile cache — same data path _nearest_address
uses today. That is issue #189's question, deliberately not answered here.
"""

import logging

import duckdb

from placeroot import overture

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

# Radii for the containing-country lookup (divisions theme), only ever run
# when the address query came back empty and the answer needs a coverage
# explanation. Wider than the address radii because divisions are sparse.
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


def _upstream_glob() -> str:
    return overture._upstream_glob(THEME, type_=TYPE_)


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


def _query_addresses(lat: float, lon: float, limit: int) -> list[dict]:
    """The `limit` nearest address points, nearest first; [] if none are near.

    Raises overture.SchemaDegraded if the dataset can't answer at all, or
    overture.UpstreamUnavailable if the scan fails.
    """
    glob = _upstream_glob()
    missing = set(_check_schema(glob))
    columns = ", ".join(_column_expr(c, missing) for c in _SELECT_COLUMNS)
    rows: list[tuple] = []
    for radius_m in SEARCH_RADII_M:
        bbox_filter, distance_filter, params, _bbox, _radius = overture.area_geometry(
            lat, lon, radius_m
        )
        sql = f"""
            SELECT {columns},
                   round({overture.DISTANCE_EXPR}, 1) AS distance_m
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND {distance_filter}
            ORDER BY distance_m, street NULLS LAST, number NULLS LAST
            LIMIT {limit}
        """
        try:
            with overture._conn_lock:
                rows = overture.conn().execute(sql, params).fetchall()
        except duckdb.Error as e:
            raise overture.UpstreamUnavailable(str(e)) from e
        if len(rows) >= limit:
            break
    return [_row_to_result(r) for r in rows]


def _country_at(lat: float, lon: float) -> tuple[str | None, str | None]:
    """(ISO 3166-1 alpha-2 code, country name) containing the point, or (None, None).

    Read off the nearest divisions row — the same theme and the same
    expanding-radius shape geocode._nearest_division uses — rather than a
    point-in-polygon test against division_area: this only ever runs to
    explain an *empty* address answer, where a nearest-division approximation
    (wrong only for a point sitting within metres of a national border) is
    worth far more than a second polygon scan. Degrades to (None, None) on
    any schema or query trouble; the caller then says so rather than guessing.
    """
    glob = overture.upstream_glob(theme="divisions", type_="division")
    missing = set(overture.missing_columns(glob, ["country", "hierarchies"]))
    if "country" in missing:
        return None, None
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
            logger.warning("country lookup for address coverage note failed: %s", e)
            return None, None
        if row:
            return row[0], row[1]
    return None, None


def _coverage_note(lat: float, lon: float) -> str:
    """Why an address query came back empty — never left to an unexplained []."""
    code, name = _country_at(lat, lon)
    widest = SEARCH_RADII_M[-1]
    if code is None:
        return (
            f"no address point within {widest} m, and no division in the active dataset "
            "identifies this coordinate's country, so its address coverage could not be "
            "checked. Overture's addresses theme is alpha and covers "
            f"{len(COVERED_COUNTRIES)} countries, so an empty result may mean no data "
            "rather than no addresses."
        )
    label = f"{name} ({code})" if name else code
    if code not in COVERED_COUNTRIES:
        return (
            f"no Overture address coverage for {label}. The addresses theme is alpha and "
            f"carries data for {len(COVERED_COUNTRIES)} countries; this is not one of "
            "them, so this empty result means no data, not no addresses. Try "
            "reverse_geocode, which falls back to the containing admin areas."
        )
    return (
        f"no address point within {widest} m of this coordinate. {label} is covered by "
        "Overture's addresses theme, but coverage inside a covered country is partial, "
        "so this may be a gap in the data rather than an unaddressed location."
    )


def address_at(lat: float, lon: float, limit: int = DEFAULT_LIMIT) -> dict:
    """The nearest Overture address points to a coordinate, nearest first.

    Returns {"results": [{number, street, unit?, postcode?, postal_city?,
    address_levels?, country, distance_m}, ...]} and, whenever `results` is
    empty, a "note" saying why — see _coverage_note. `degraded_fields` is
    added when the active dataset is missing non-essential columns.

    Raises overture.SchemaDegraded if the dataset lacks bbox or street, and
    overture.UpstreamUnavailable if the remote scan fails; server.py maps
    both to structured errors like every other tool.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    results = _query_addresses(lat, lon, limit)
    payload: dict = {"results": results}
    if not results:
        payload["note"] = _coverage_note(lat, lon)
    degraded = degraded_fields()
    if degraded:
        payload["degraded_fields"] = degraded
    return payload
