"""Hydrology near a point, from Overture's base theme (issue #200).

The third PlaceRoot tool over theme=base and infrastructure.py's closest
sibling: type=water carries oceans, seas, bays, lakes, ponds, reservoirs,
rivers, streams, canals, ditches, springs and swimming pools. The
questions it answers — "is this parcel waterfront", "how far to the
nearest canal", "what river runs past here" — are radius questions, so
this is infrastructure.py's shape (bbox prefilter around a radius, exact
haversine predicate, ST_ClosestPoint distance to the *actual* geometry,
nearest-first, capped LIMIT, true in-range count) pointed at a fourth
base-theme type, with one structural addition described below.

Distance is measured to the closest point *on* each feature
(ST_ClosestPoint), not to a centroid or a bbox corner: rows here are
points, linestrings and polygons, and the centroid of a 3 km canal says
nothing about how far the canal is from you — you can be standing on its
bank. The closest point is taken in a cos(latitude)-scaled space rather
than raw degrees (geo.closest_point_sql), because degree-space nearness
is not ground nearness away from the equator: uncorrected, a diagonal
feature reads ~25% too far at 60°N and ~63% at 70°N, which corrupts both
the nearest-first ordering and the radius cutoff. The final metre is
still a great circle (geo.haversine_sql, shared with every other theme).
The antimeridian limitation is exactly infrastructure.py's; see that
module's docstring.

Cache theme key: "base_water", following land_use.py's composite
"base_<type>" convention (underscore, not ':', because cache.tile_path
uses the string verbatim as a directory component and ':' is illegal in a
Windows path component).

The structural addition: generalized, tile-cut water bodies
-----------------------------------------------------------
Overture does not ship one ocean polygon. It ships the ocean cut into
1-degree tiles, and those tiles are generalized: their landward edge is a
coarse approximation of the coastline that *swallows coastal land*. Live
against release 2026-07-22.0, the San Francisco Ferry Building
(37.7955, -122.3937) — a building, on land — is ST_Contains-inside the
subtype='ocean' tile covering it. Two things follow, and both are wrong
answers if this module ignores them:

1. ST_ClosestPoint of a polygon that contains the query point *is* the
   query point, so the ocean would come back as a 0.0 m "nearest water
   feature" for any coastal point, ahead of the actual pond across the
   street. A 0 m row that is really "somewhere within the generalized
   coastal blob" is worse than no row: it reads as "you are in the
   water".
2. Falling back to the polygon *boundary* to recover a shoreline distance
   does not help, because the boundary of a tile includes the phantom
   straight edges where the 1-degree grid cut the ocean apart. Those cuts
   run through open water, near land, and are indistinguishable from real
   coastline in the geometry: six San Francisco Bay rows all measured
   0.0 m to their boundary from the same query point. So this module
   never derives a shoreline distance from these rows, and does not
   pretend to answer "how far to the coast" — it answers the question the
   data can actually support.

So a generalized row is reported by *containment*, not by distance:
on_water=True plus water_body (the body's name, else its class, else its
subtype), and the row is excluded from the distance list entirely.

Detecting "generalized" (the part worth stating precisely, because two
obvious rules are both wrong). Rule one, a subtype allowlist, is wrong on
its own: subtype='physical' holds sea (span 14.6°) and strait (4.7°)
alongside waterfall (0.00015°), cape and shoal, so excluding
subtype='physical' would silently drop waterfalls. Rule two, a bare
geometric span test over *any* polygon, is worse — it deletes the largest
real lakes and rivers from the answer. Live against release 2026-07-22.0,
Lake Michigan is a single subtype='lake' polygon spanning 3.29° × 4.49°,
Lago di Como 0.322° × 0.357°; neither is tile-cut, and neither has a
generalized coastline. Under the span-only rule a query 371 m from Lake
Como returned a 376 m creek and no lake at all, and a query 992 m off
Lake Michigan with water_class='lake' returned nothing plus an "arid,
remote or unmapped" note.

The rule that actually holds is: the tile-cut/generalized problem is a
*marine* one. It applies to subtype='ocean' — the 1-degree grid, live
maximum span 1.0014°, unconditionally, so a small corner sliver of a tile
is still recognized as ocean — and to the handful of enormous
class IN ('sea','strait') polygons, whose open ends are arbitrary cuts
across water, gated by the same span test so a small strait keeps its
distance. Everything else — every lake, river, canal, stream, pond, bay,
waterfall, however large — keeps a real ST_ClosestPoint distance.

A non-generalized polygon that contains the point (you are standing in a
lake) is also reported as on_water rather than as a 0.0 m row, for the
same reason it is more informative: "you are on Sloterplas" beats "there
is water 0.0 m away".

Density (infrastructure.py's street-furniture lesson, again): a 0.1°
box over Amsterdam holds 2044 water rows, 1084 of them canals. So the
query returns the true in-range count alongside the clipped rows, and
subtype/class filters exist, so a caller can never mistake "the 5
nearest" for "all there is".

Design rule: answers, not data — compact rows ({name, subtype, class,
distance_m} plus is_salt/is_intermittent only when true), no geometry, no
source_tags. Empty results are a first-class answer: base/water coverage
is OSM-derived, and "no water within 500 m" is a real finding about an
arid or unmapped place, not an error.
"""

import logging

import duckdb

from placeroot import cache, db, geo, overture

logger = logging.getLogger(__name__)

THEME = "base"
TYPE_ = "water"

# geometry/bbox are essential (no distance and no containment test can be
# made without them); everything else degrades to None/absent in its field
# rather than failing the call — see degraded_fields().
REQUIRED_COLUMNS = [
    "geometry", "bbox", "subtype", "class", "names", "is_salt", "is_intermittent",
]
ESSENTIAL_COLUMNS = {"geometry", "bbox"}

DEFAULT_RADIUS_M = 500
DEFAULT_LIMIT = 5
MAX_ROWS = overture.MAX_ROWS  # same response-size cap every other tool uses

# Same prefilter pad as infrastructure.py, for the same reason: the bbox
# box is built in degrees off geo.bbox_around's 111_320.0 m/deg while the
# exact predicate is a haversine at R=6371000 (111_194.93 m/deg), so an
# unpadded box is ~0.11% narrower than the circle it must contain and
# drops a bare point sitting at exactly radius_m. The exact predicate
# still decides membership, so the pad can only add candidates.
_BBOX_PREFILTER_PAD = 1.002

# A *marine* polygon whose bbox spans at least this many degrees (~28 km
# at the equator) is treated as a generalized water body: its interior
# swallows coastal land and its boundary carries phantom grid cuts or
# arbitrary open-water closures, so it is reported by containment
# (on_water/water_body) and never by distance. The span test applies only
# to the ocean-like classes below — applied to any polygon it deletes Lake
# Michigan (3.29° x 4.49°, one un-cut lake row) from the answer. See the
# module docstring.
_GENERALIZED_SPAN_DEG = 0.25

# Overture's `subtype` values that are gridded coastline by construction.
# Live max span of a subtype='ocean' row is 1.0014° — the 1-degree grid —
# and every one of them is generalized regardless of how small the sliver
# is, so these skip the span test entirely.
_OCEAN_SUBTYPES = ("ocean",)

# `class` values that ride subtype='physical' and describe an open marine
# body rather than a discrete feature. Only generalized once they are
# large: a 4.7° strait's ends are arbitrary cuts across water, a 200 m one
# is a real place you can be a real distance from.
_OCEAN_CLASSES = ("sea", "strait")

# Overture base/water's `subtype` vocabulary, from a live scan of a
# Europe-wide box. Used only to decide whether a caller's substring filter
# can possibly match anything that survives the ocean exclusion — see
# _ocean_only_filter. A value missing from this list simply means no
# short-circuit, never a dropped row.
_KNOWN_SUBTYPES = (
    "canal", "human_made", "lake", "ocean", "physical", "pond", "reservoir",
    "river", "spring", "stream", "wastewater", "water",
)

# How many containing polygons to pull back, smallest-area first. Only the
# first is used (the most specific body containing the point); a second is
# fetched only so the count is knowable without a second query.
_CONTAINMENT_LIMIT = 2


def _ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once."""
    try:
        db.ensure_spatial()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(f"could not load spatial extension: {e}") from e


def _geom_expr(upstream: str) -> str:
    """SQL expression yielding a GEOMETRY for the dataset's geometry column."""
    return geo.geom_expr(upstream)


def _cache_theme() -> str:
    """Composite cache theme key for this base-theme type — see module docstring."""
    return f"{THEME}_{TYPE_}"


def set_data_path(path: str | None) -> None:
    """Point water queries at path instead of live S3 (tests)."""
    overture.set_data_path(path, theme=THEME, type_=TYPE_)


def _upstream_glob() -> str:
    return overture._upstream_glob(THEME, type_=TYPE_)


def _check_schema(glob: str) -> list[str]:
    """Missing REQUIRED_COLUMNS for glob, raising SchemaDegraded if any are essential."""
    missing = overture.missing_columns(glob, REQUIRED_COLUMNS)
    essential_missing = [c for c in missing if c in ESSENTIAL_COLUMNS]
    if essential_missing:
        raise overture.SchemaDegraded(essential_missing)
    return missing


def degraded_fields() -> list[str]:
    """Non-essential REQUIRED_COLUMNS missing from the active water dataset."""
    missing = overture.missing_columns(_upstream_glob(), REQUIRED_COLUMNS)
    return [c for c in missing if c not in ESSENTIAL_COLUMNS]


def _from_source(bbox: tuple[float, float, float, float]) -> str:
    """SQL FROM-clause source for a water query: local cache tiles, or upstream."""
    upstream = _upstream_glob()
    try:
        return cache.source_sql(_cache_theme(), upstream, bbox)
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e


# Haversine distance in meters between the closest point on a row's
# geometry (nlat/nlon, computed once in the `nearest` CTE) and the query
# point — the shared expression every theme uses.
_NEAREST_DISTANCE_EXPR = geo.haversine_sql("nlat", "nlon")

# bbox of the row contains the query point. Cheap row-group prefilter in
# front of the exact (expensive) ST_Contains test — divisions.py's and
# land_use.py's recipe.
_BBOX_CONTAINS_POINT = (
    "bbox.xmin <= $lon AND bbox.xmax >= $lon AND bbox.ymin <= $lat AND bbox.ymax >= $lat"
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _generalized_expr(missing: set[str], geom_expr: str) -> str:
    """SQL predicate: is this row a generalized, tile-cut marine body?

    Two halves, both narrow on purpose (module docstring for the live
    numbers). subtype='ocean' is unconditional: those rows *are* the
    1-degree grid, and a corner sliver of a tile is as untrustworthy as a
    whole one. class IN ('sea','strait') is gated on a
    _GENERALIZED_SPAN_DEG polygon span, because 'physical' holds the
    Mediterranean and individual waterfalls alike and only the enormous
    ones have arbitrary boundaries.

    What is deliberately *not* here is a span test over polygons in
    general: Lake Michigan is one 3.29° x 4.49° subtype='lake' polygon
    with a real, un-generalized shoreline, and a generic span test deletes
    it — and Lake Geneva, and Lago di Como, and the Mississippi — from
    every distance answer.

    COALESCE, not a bare comparison: subtype/class are nullable upstream,
    and `NULL = 'ocean'` is NULL, so the caller's `NOT (...)` would be NULL
    too and silently drop every row with an unlabelled subtype.
    """
    parts = []
    if "subtype" not in missing:
        parts.append(f"COALESCE(subtype, '') IN ({_sql_list(_OCEAN_SUBTYPES)})")
    if "class" not in missing:
        span = (
            f"(bbox.xmax - bbox.xmin >= {_GENERALIZED_SPAN_DEG}"
            f" OR bbox.ymax - bbox.ymin >= {_GENERALIZED_SPAN_DEG})"
        )
        parts.append(
            f"(COALESCE(class, '') IN ({_sql_list(_OCEAN_CLASSES)})"
            f" AND {span} AND ST_Dimension({geom_expr}) = 2)"
        )
    if not parts:
        # Neither label is available, so ocean cannot be told from lake.
        # Treating nothing as generalized is the safe degrade: containment
        # still answers "you are inside X", and no real body is deleted.
        return "FALSE"
    return f"({' OR '.join(parts)})"


def _ocean_only_filter(subtype: str | None, water_class: str | None) -> bool:
    """Would these filters match *only* rows this module never distances?

    subtype='ocean' rows are always excluded from the distance list, so
    `water_near(..., subtype='ocean')` is structurally unsatisfiable: it
    returns [] and, without this check, the empty-result note claims the
    place is "arid, remote or unmapped" — which is a confidently wrong
    answer to give someone standing on a pier.

    The filter is a case-insensitive substring, so the test is: does it
    match at least one known subtype, and is every subtype it matches an
    ocean one? 'ocean', 'OCEAN' and 'cea' short-circuit; 'o' does not (it
    also matches 'pond' and 'reservoir') and is left to the query. A
    water_class filter is not considered here: class='ocean' rows are
    excluded, but 'sea'/'strait' rows below the span threshold are not, so
    a class filter can legitimately return something.
    """
    if not subtype:
        return False
    needle = subtype.lower()
    matched = [s for s in _KNOWN_SUBTYPES if needle in s]
    return bool(matched) and all(s in _OCEAN_SUBTYPES for s in matched)


def _attribute_filters(
    missing: set[str], subtype: str | None, water_class: str | None, params: dict
) -> list[str]:
    """subtype/class filter clauses (params added in place).

    Case-insensitive substring match on a *bound* parameter with the
    caller's LIKE metacharacters escaped, so Overture's snake_case values
    (salt_pond, hot_spring) match literally rather than turning '_' into a
    single-character wildcard — identical semantics to
    infrastructure._attribute_filters. A filter whose column is absent from
    the active dataset is a no-op rather than an error.
    """
    filters = []
    if subtype and "subtype" not in missing:
        filters.append("subtype ILIKE $subtype ESCAPE '\\'")
        params["subtype"] = f"%{overture._like_escape(subtype)}%"
    if water_class and "class" not in missing:
        filters.append("class ILIKE $water_class ESCAPE '\\'")
        params["water_class"] = f"%{overture._like_escape(water_class)}%"
    return filters


def _containing_body(
    lat: float, lon: float, missing: set[str], geom_expr: str, bbox: tuple
) -> dict | None:
    """The most specific water body containing (lat, lon), or None.

    Returns {"water_body": str|None, "generalized": bool} for the
    smallest-area containing polygon. None — no polygon contains the point
    — is the common case and a perfectly good answer.

    This is what replaces a bogus 0.0 m distance row for both the
    generalized ocean tiles (which contain dry coastal land) and for an
    honest "you are standing in this lake": either way "you are on X" is
    the answer, and a 0.0 m row in the nearest-first list is not.
    """
    name_expr = "NULL" if "names" in missing else "names.primary"
    class_expr = "NULL" if "class" in missing else "class"
    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    sql = f"""
        SELECT
            {name_expr}    AS name,
            {class_expr}   AS class,
            {subtype_expr} AS subtype,
            {_generalized_expr(missing, geom_expr)} AS generalized,
            ST_Area({geom_expr}) AS area
        FROM {_from_source(bbox)}
        WHERE {_BBOX_CONTAINS_POINT}
          AND ST_Contains({geom_expr}, ST_Point($lon, $lat))
        ORDER BY area ASC
        LIMIT {_CONTAINMENT_LIMIT}
    """
    try:
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, {"lat": lat, "lon": lon}).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    if not rows:
        return None
    name, class_, subtype, generalized, _area = rows[0]
    return {"water_body": name or class_ or subtype, "generalized": bool(generalized)}


def water_near(
    lat: float,
    lon: float,
    radius_m: float = DEFAULT_RADIUS_M,
    limit: int = DEFAULT_LIMIT,
    subtype: str | None = None,
    water_class: str | None = None,
) -> tuple[list[dict], float, int, dict | None]:
    """Nearest water features to a point, nearest first, plus the body it sits in.

    Returns (rows, effective_radius_m, in_range_count, containing) where
    each row is {"subtype", "class", "distance_m"} plus "name" when the
    feature is named and "is_salt"/"is_intermittent" only when true (an
    absent flag means false-or-unknown; carrying explicit nulls would cost
    tokens to say nothing). No geometry — answers, not data.

    distance_m is measured to the closest point on the feature, so a canal
    bank you are standing on reads ~0 m rather than "distance to the middle
    of the canal".

    in_range_count is how many features matched the radius and filters
    *before* the LIMIT, so "the 5 nearest of 812 in a canal district" can
    never be read as "there are 5".

    containing is None, or {"water_body", "generalized"} for the most
    specific polygon containing the point. Rows for containing polygons and
    for generalized marine bodies (subtype='ocean', plus huge sea/strait
    polygons) are deliberately excluded from the distance list: the former
    would all be 0.0 m, and the latter's geometry lies about the shoreline
    in both directions (its interior covers dry coastal land, its boundary
    carries phantom grid cuts). Large *lakes and rivers* are not excluded —
    Lake Michigan and the Mississippi have real shorelines and appear with
    real edge distances. See the module docstring.

    subtype/water_class narrow the search by Overture's subtype and class
    columns (case-insensitive substring, wildcards escaped). water_class is
    the `class` column under a non-reserved name.

    An empty list is a valid answer, not an error. The returned radius is
    the *effective* one (geo.clamp_radius_m may have lowered it).

    Raises SchemaDegraded if geometry or bbox is missing from the active
    dataset, or UpstreamUnavailable if the remote scan (or the one-time
    spatial extension load) fails.
    """
    _ensure_spatial()
    # Floor of 1, not 0 — infrastructure.py's reasoning: in_range_count
    # rides a window function over the returned rows, so LIMIT 0 would
    # report zero rows *and* zero in range with water all around.
    limit = max(1, min(int(limit), MAX_ROWS))
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    geom_expr = _geom_expr(upstream)

    radius_m = geo.clamp_radius_m(radius_m)
    bbox = geo.bbox_around(lat, lon, radius_m * _BBOX_PREFILTER_PAD)
    xmin, ymin, xmax, ymax = bbox
    bbox_filter, params = geo.bbox_filter_sql(xmin, ymin, xmax, ymax)
    params = {**params, "lat": lat, "lon": lon, "radius_m": radius_m}

    containing = _containing_body(lat, lon, missing, geom_expr, bbox)

    # subtype='ocean' can never appear in the distance list, so a filter
    # that matches nothing else has an answer already — and running the
    # scan would only produce an empty list that reads as "no water here".
    if _ocean_only_filter(subtype, water_class) and "subtype" not in missing:
        return [], radius_m, 0, containing

    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    class_expr = "NULL" if "class" in missing else "class"
    name_expr = "NULL" if "names" in missing else "names.primary"
    salt_expr = "NULL" if "is_salt" in missing else "is_salt"
    intermittent_expr = "NULL" if "is_intermittent" in missing else "is_intermittent"

    filters = [
        bbox_filter,
        # Generalized bodies never contribute a distance (module docstring).
        f"NOT {_generalized_expr(missing, geom_expr)}",
        # A polygon containing the point is answered by `containing`, not by
        # a 0.0 m row. The cheap bbox test short-circuits the ST_Contains
        # for the overwhelming majority of rows.
        f"NOT ({_BBOX_CONTAINS_POINT} AND ST_Contains({geom_expr}, ST_Point($lon, $lat)))",
        *_attribute_filters(missing, subtype, water_class, params),
    ]

    # Latitude-corrected: raw ST_ClosestPoint picks the nearest point in
    # degree space, which is the wrong point (and a distance up to ~63% too
    # large at 70 deg N) anywhere but the equator. See geo.closest_point_sql.
    nlon_expr, nlat_expr = geo.closest_point_sql(geom_expr)

    sql = f"""
        WITH nearest AS (
            SELECT
                {name_expr}         AS name,
                {subtype_expr}      AS subtype,
                {class_expr}        AS class,
                {salt_expr}         AS is_salt,
                {intermittent_expr} AS is_intermittent,
                {nlon_expr} AS nlon,
                {nlat_expr} AS nlat
            FROM {_from_source(bbox)}
            WHERE {' AND '.join(filters)}
        ),
        in_range AS (
            SELECT name, subtype, class, is_salt, is_intermittent,
                   {_NEAREST_DISTANCE_EXPR} AS distance_m
            FROM nearest
            WHERE {_NEAREST_DISTANCE_EXPR} <= $radius_m
        )
        SELECT name, subtype, class, is_salt, is_intermittent, distance_m,
               COUNT(*) OVER () AS in_range_count
        FROM in_range
        ORDER BY distance_m
        LIMIT {limit}
    """
    try:
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e

    results = []
    for name, row_subtype, class_, is_salt, is_intermittent, distance_m, _count in rows:
        row = {"subtype": row_subtype, "class": class_, "distance_m": round(distance_m, 1)}
        if name:
            row = {"name": name, **row}
        if is_salt:
            row["is_salt"] = True
        if is_intermittent:
            row["is_intermittent"] = True
        results.append(row)
    in_range_count = rows[0][-1] if rows else 0
    return results, radius_m, in_range_count, containing
