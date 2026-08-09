"""Point classification against Overture's base theme (issue #167).

The first PlaceRoot tool over theme=base. Two of its types answer "what is
this land used for / what covers it": type=land_use (residential,
commercial, industrial, park, ...) and type=land_cover (grass, forest,
water, ...). Both are polygon datasets read the same keyless DuckDB+S3
GeoParquet way every other theme is (overture._upstream_glob already
supports an arbitrary theme/type pair), so this needs no new infra — it's
buildings.py's structure (schema-probe/degrade, tile cache, bbox-prefilter-
then-ST_Contains) pointed at a different theme, twice (once per type).

Point-in-polygon, not radius search: land_use_at asks "what polygon
contains this exact point", the same shape as divisions.py's admin_lookup,
not buildings.py's "what's nearby". So this borrows admin_lookup's bbox
prefilter (bbox contains the point) + exact ST_Contains recipe rather than
buildings.py's bbox-around-a-radius + haversine recipe.

Overlap (Overture nests land_use polygons — a park inside a residential
parcel is normal, not a data error): every containing polygon is a
candidate, and the smallest-area one is returned as the most specific
answer, with a "note" set whenever more than one candidate existed so a
caller knows the pick was among several valid ones, not the only one.

Tile cache and a two-type theme: cache.py's tile cache is keyed by
(release, theme) plus a schema-fingerprint subdirectory it derives itself,
so passing plain THEME="base" for both land_use and land_cover would only
accidentally avoid a collision if their two schemas happen to hash
differently. Rather than depend on that coincidence, this module keys the
cache by a composite "base_land_use" / "base_land_cover" string (cache.py's
theme parameter is opaque — any string works) so the two types can never
land tile files in the same directory even if a future schema change made
their columns identical.

Correctness of the tile cache for point-in-polygon (the thing that would
silently break an answer if wrong): cache.py's per-tile materialization
query for tile T selects every row whose bbox *intersects* T's 1-degree
box (ensure_tile's WHERE clause), not rows wholly contained in T. The query
here asks for tile(s) covering a small bbox_around(lat, lon, radius) — at
POINT_QUERY_RADIUS_M that's normally exactly one 1-degree tile, the one
containing (lat, lon). Any polygon that contains (lat, lon) necessarily has
a bbox that also contains (lat, lon) (a shape's bbox always encloses the
shape), so that polygon's bbox intersects the point's tile regardless of
how large the polygon is — a country-sized forest polygon is still
selected into the single tile the query point falls in. So a large
containing polygon is never missed by the cache, no matter its extent;
what the cache tile boundary does affect is unrelated non-containing rows
near the tile's edge, which the exact ST_Contains test downstream discards
anyway.

Design rule: answers, not data — no geometry, no source_tags/wikidata in
the response. A None land_use/land_cover is a first-class answer (base-
theme coverage is OSM-derived and patchy outside cities), not an error;
mirrors divisions.admin_lookup's empty-chain stance.
"""

import logging

import duckdb

from placeroot import cache, db, geo, overture, release

logger = logging.getLogger(__name__)

THEME = "base"
TYPE_LAND_USE = "land_use"
TYPE_LAND_COVER = "land_cover"

# names is only meaningful for land_use (land_cover rows aren't named
# individually); requesting it against land_cover's schema just degrades
# to None like any other missing column, so one shared list is enough.
REQUIRED_COLUMNS = ["id", "geometry", "bbox", "subtype", "class", "names"]
ESSENTIAL_COLUMNS = {"geometry", "bbox"}

# What land_cover actually carries: it has neither class nor names upstream
# (see the note above — those degrade to None there by design), so a schema
# watch on that type must expect only these. scripts/overture_canary.py reads
# this list; without it the canary would report the by-design absences as
# drift every week.
LAND_COVER_REQUIRED_COLUMNS = ["id", "geometry", "bbox", "subtype"]

# Small bbox around the query point — this is a point-in-polygon lookup,
# not a radius search, so the box only needs to be big enough that
# geo.bbox_around's degree math and floating point don't put the point
# outside its own box; it does NOT bound which polygons can match (see the
# module docstring's tile-cache correctness note — a large containing
# polygon is still found because its bbox necessarily overlaps this tile).
POINT_QUERY_RADIUS_M = 75.0

# How many containing candidates to pull back, smallest-area first. Only
# the first is ever returned; a second row existing is enough to know the
# point was ambiguous and flag it via "note" — no need to fetch more.
_CANDIDATE_LIMIT = 2


def _ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once.

    Thin wrapper over db.ensure_spatial() (mirrors buildings.py/divisions.py).
    """
    try:
        db.ensure_spatial()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(f"could not load spatial extension: {e}") from e


def _geom_expr(upstream: str) -> str:
    """SQL expression yielding a GEOMETRY for the dataset's geometry column."""
    return geo.geom_expr(upstream)


def _cache_theme(type_: str) -> str:
    """Composite cache theme key, distinct per base-theme type — see module docstring.

    Underscore separator, not ':': cache.tile_path uses this string verbatim
    as a directory component, and ':' is illegal in a Windows path component
    (the mkdir would raise WinError 123 on the first cached query).
    """
    return f"{THEME}_{type_}"


def set_data_path(path: str | None, type_: str) -> None:
    """Point land_use/land_cover queries at path instead of live S3 (tests).

    type_ is required (unlike buildings.set_data_path's implicit single
    type) since this module queries two types under the same theme.
    """
    overture.set_data_path(path, theme=THEME, type_=type_)


def _upstream_glob(type_: str) -> str:
    return overture._upstream_glob(THEME, type_=type_)


def _check_schema(glob: str) -> list[str]:
    """Missing REQUIRED_COLUMNS for glob, raising SchemaDegraded if any are essential."""
    missing = overture.missing_columns(glob, REQUIRED_COLUMNS)
    essential_missing = [c for c in missing if c in ESSENTIAL_COLUMNS]
    if essential_missing:
        raise overture.SchemaDegraded(essential_missing)
    return missing


def degraded_fields() -> list[str]:
    """Non-essential REQUIRED_COLUMNS missing from either active base-theme dataset."""
    missing: set[str] = set()
    for type_ in (TYPE_LAND_USE, TYPE_LAND_COVER):
        missing.update(overture.missing_columns(_upstream_glob(type_), REQUIRED_COLUMNS))
    return sorted(c for c in missing if c not in ESSENTIAL_COLUMNS)


def _from_source(bbox: tuple[float, float, float, float], type_: str) -> str:
    """SQL FROM-clause source for a base-theme query: local cache tiles, or upstream.

    Mirrors buildings.py's own _from_source, keyed by _cache_theme(type_)
    rather than a bare theme string — see the module docstring.
    """
    upstream = _upstream_glob(type_)
    if cache.enabled():
        try:
            with db.conn_lock:
                paths = cache.local_paths_for_query(
                    db.shared_conn(), release.resolve_release(), _cache_theme(type_), bbox,
                    upstream, db.new_connection,
                )
        except duckdb.Error as e:
            raise overture.UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


def _classify(lat: float, lon: float, type_: str, include_name: bool) -> tuple[dict | None, bool]:
    """Smallest-area polygon of type_ containing (lat, lon), and whether it was ambiguous.

    Returns (None, False) if no polygon contains the point — a valid
    answer, not an error. The bool is True iff more than one candidate
    polygon contained the point (real Overture data nests them), meaning
    the returned row was chosen over at least one other equally-valid
    match, smallest area first.
    """
    upstream = _upstream_glob(type_)
    missing = set(_check_schema(upstream))
    geom_expr = _geom_expr(upstream)

    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    class_expr = "NULL" if "class" in missing else "class"
    name_expr = "NULL" if (not include_name or "names" in missing) else "names.primary"

    xmin, ymin, xmax, ymax = geo.bbox_around(lat, lon, POINT_QUERY_RADIUS_M)
    # Cheap row-group prefilter (bbox containing the point) before the exact,
    # expensive polygon test — same recipe as divisions.py's admin_lookup;
    # without it this is an ST_Contains scan over every polygon of this type
    # on Earth (divisions.py measured that at 10+ minutes live).
    bbox_prefilter = (
        "bbox.xmin <= $lon AND bbox.xmax >= $lon"
        " AND bbox.ymin <= $lat AND bbox.ymax >= $lat"
    )
    sql = f"""
        SELECT
            {subtype_expr} AS subtype,
            {class_expr}   AS class,
            {name_expr}    AS name,
            ST_Area({geom_expr}) AS area
        FROM {_from_source((xmin, ymin, xmax, ymax), type_)}
        WHERE {bbox_prefilter} AND ST_Contains({geom_expr}, ST_Point($lon, $lat))
        ORDER BY area ASC
        LIMIT {_CANDIDATE_LIMIT}
    """
    try:
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, {"lat": lat, "lon": lon}).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e

    if not rows:
        return None, False

    subtype, class_, name, _area = rows[0]
    result = {"subtype": subtype, "class": class_}
    if include_name:
        result["name"] = name
    return result, len(rows) > 1


def land_use_at(lat: float, lon: float) -> dict:
    """Land use and land cover classification at a point, no geometry.

    Returns {"lat", "lon", "land_use": {"subtype", "class", "name"} or None,
    "land_cover": {"subtype", "class"} or None, "note": ... (only present
    when an overlap was resolved)}. A None value for either field means no
    polygon of that type covers the point in the active dataset — coverage
    is OSM-derived and patchy outside well-mapped cities, so this is a
    common, valid answer, not an error (mirrors admin_lookup's empty-chain
    stance).

    When more than one polygon of a type contains the point (Overture nests
    land_use polygons — e.g. a park inside a residential parcel), the
    smallest-area / most specific one is returned and "note" flags that the
    pick was made among overlapping candidates rather than a lone match.

    Raises SchemaDegraded if geometry or bbox is missing from either active
    base-theme dataset (no way to test containment at all), or
    UpstreamUnavailable if a remote scan (or the one-time spatial extension
    load) fails. Non-essential columns (subtype, class, names) missing from
    a dataset come back as None in their field rather than failing — see
    degraded_fields().
    """
    _ensure_spatial()
    land_use, lu_ambiguous = _classify(lat, lon, TYPE_LAND_USE, include_name=True)
    land_cover, lc_ambiguous = _classify(lat, lon, TYPE_LAND_COVER, include_name=False)

    result = {"lat": lat, "lon": lon, "land_use": land_use, "land_cover": land_cover}
    notes = []
    if lu_ambiguous:
        notes.append(
            "multiple overlapping land_use polygons contain this point; "
            "returning the smallest/most specific"
        )
    if lc_ambiguous:
        notes.append(
            "multiple overlapping land_cover polygons contain this point; "
            "returning the smallest/most specific"
        )
    if notes:
        result["note"] = " ".join(notes)
    return result
