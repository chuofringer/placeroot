"""Point -> containing admin hierarchy, and polygon -> overlapping divisions,
against Overture's divisions theme (issue #11, #348).

Overture's divisions theme carries names/subtype directly on division_area
rows (they're denormalized, not just on the paired type=division rows), so
a single glob over theme=divisions/type=division_area is enough to answer
"what contains this point" without a join. Uses DuckDB's spatial extension
for point-in-polygon (ST_Contains) against a WKB geometry column.

Data-path injection follows overture.py's per-theme design
(overture.set_data_path(path, theme="divisions")) so tests can point this at
a small synthetic fixture (a handful of nested polygons) instead of a
divisions dataset large enough that point-in-polygon over it would be slow
without a spatial index.

Design choice, documented rather than hidden: this reuses db.shared_conn()
(so the spatial extension loads once, alongside httpfs) but does NOT route
through cache.py's tile cache the way places queries do. The places cache
exists because point-radius searches cluster geographically and get
replayed; divisions lookups are one-point-in-time-anywhere queries against
a comparatively tiny dataset (a few million division_area polygons vs.
places), so a whole-glob scan per call is the simpler, adequate choice for
this P1 pass. If admin_lookup call volume or upstream latency makes that a
problem later, the cache's theme-keyed design (it already takes theme as a
parameter) would let it slot in without further plumbing.
"""

import json
import logging

import duckdb

from placeroot import db, geo, manifest, overture, trace

logger = logging.getLogger(__name__)

THEME = "divisions"

# Columns admin_lookup depends on. geometry is essential — without it there
# is no point-in-polygon test to run at all, unlike places' softer columns.
# country (ISO 3166-1 alpha-2) and region (ISO 3166-2) are soft like
# name/subtype/division_id (#446): a dataset without them still answers,
# just without those two fields on each chain row.
REQUIRED_COLUMNS = [
    "id", "names", "subtype", "geometry", "bbox", "division_id", "country", "region",
]
ESSENTIAL_COLUMNS = {"geometry"}

# divisions_in_polygon filters by subtype, so unlike admin_lookup (where a
# missing subtype column just degrades a chain entry's "type" to NULL) a
# missing subtype column here means the tool cannot honor its own contract
# (a caller-chosen set of subtypes) at all — essential, not soft.
POLYGON_REQUIRED_COLUMNS = ["id", "names", "subtype", "geometry", "bbox", "division_id"]
POLYGON_ESSENTIAL_COLUMNS = {"geometry", "subtype"}

# Upper bound (degrees) on the input polygon's bbox span on either axis.
# Derived from geo.MAX_QUERY_RADIUS_M — the same abuse guard every
# point-radius tool applies — converted to a degree span (the diameter of
# the largest circle those tools honor, ~9 degrees). A continent-sized (or
# antimeridian-crossing, which shows up here as a near-360-degree longitude
# span) polygon would otherwise degenerate the bbox prefilter into a
# global scan.
MAX_POLYGON_SPAN_DEG = 2 * geo.MAX_QUERY_RADIUS_M / 111_320.0


def _ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once.

    Thin wrapper over db.ensure_spatial() (issue #40) that keeps this
    module's original UpstreamUnavailable-on-failure contract.
    """
    try:
        db.ensure_spatial()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(f"could not load spatial extension: {e}") from e


def _geom_expr(upstream: str) -> str:
    """SQL expression yielding a GEOMETRY for the dataset's geometry column.

    Thin wrapper over geo.geom_expr(), kept as a private name for this
    module's call sites.
    """
    return geo.geom_expr(upstream)


def admin_lookup(lat: float, lon: float, con=None) -> dict:
    """Containing admin hierarchy for a point, smallest division first.

    Returns {"chain": [{"name", "type", "id", "country"?, "region"?}, ...]}
    ordered neighborhood -> locality -> county -> region -> country
    (whichever levels actually contain the point; an empty chain means no
    division in the active dataset contains it, which is a valid answer,
    not an error). "country" (ISO 3166-1 alpha-2) and "region" (ISO 3166-2)
    are omitted from a row, not null-filled, when the active dataset lacks
    the column or the row itself carries no value (#446).

    Raises SchemaDegraded if geometry is missing from the active divisions
    dataset (no way to test containment at all), or UpstreamUnavailable if
    the remote scan (or the one-time spatial extension load) fails.
    """
    _ensure_spatial()
    upstream = overture._upstream_glob(THEME, type_="division_area")
    missing = set(overture.missing_columns(upstream, REQUIRED_COLUMNS))
    essential_missing = [c for c in missing if c in ESSENTIAL_COLUMNS]
    if essential_missing:
        raise overture.SchemaDegraded(essential_missing)

    name_expr = "NULL" if "names" in missing else "names.primary"
    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    country_expr = "NULL" if "country" in missing else "country"
    region_expr = "NULL" if "region" in missing else "region"
    # Prefer the parent division's GERS id: division_area rows are polygon
    # variants (land/territorial) with their own row ids but a shared
    # division_id, which is the stable reference agents should hold.
    if "division_id" not in missing:
        id_expr = "coalesce(division_id, id)"
    elif "id" not in missing:
        id_expr = "id"
    else:
        id_expr = "NULL"
    geom_expr = _geom_expr(upstream)
    # Bundled-manifest file pruning first (a point query touches a handful
    # of the theme's files; the footer pass on the rest is pure waste),
    # then the bbox column prunes remote row groups before the (expensive)
    # exact containment test — without that this is an ST_Contains over
    # every division polygon on Earth (measured 10+ minutes live).
    src = (
        manifest.pruned_source_sql(upstream, geo.bbox_around(lat, lon, 100.0))
        or f"read_parquet('{upstream}', hive_partitioning=1)"
    )
    bbox_prefilter = (
        "bbox.xmin <= $lon AND bbox.xmax >= $lon AND bbox.ymin <= $lat AND bbox.ymax >= $lat AND "
        if "bbox" not in missing
        else ""
    )

    sql = f"""
        SELECT
            {id_expr}   AS id,
            {name_expr} AS name,
            {subtype_expr} AS type,
            {country_expr} AS country,
            {region_expr} AS region,
            ST_Area({geom_expr}) AS area
        FROM {src}
        WHERE {bbox_prefilter}ST_Contains({geom_expr}, ST_Point($lon, $lat))
    """
    try:
        # Bounded: a point, with the bbox prefilter and manifest pruning above.
        with trace.scan("admin containment", bounded=True, source=src):
            if con is not None:
                # Caller-supplied cursor of the shared instance: safe without
                # conn_lock (cursors are DuckDB's multithreading unit) and
                # shares the warm metadata cache — gers_lookup runs this join
                # concurrently with its building join on two of these.
                rows = con.execute(sql, {"lat": lat, "lon": lon}).fetchall()
            else:
                with db.conn_lock:
                    rows = db.shared_conn().execute(sql, {"lat": lat, "lon": lon}).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    # Smallest-first ranking happens here, not in the SQL: an ORDER BY on a
    # sort key computed from geometry forces the parquet scan to produce
    # geometry eagerly for every row group, defeating late materialization
    # (land_use.py measured that at 18.5s vs 1.6s cold). The containing
    # rows are one per admin level — sorting them client-side is free.
    rows.sort(key=lambda r: r[5])
    # division_area carries multiple polygon rows per division (e.g. land and
    # maritime variants) — keep the first (smallest-area) row per division.
    chain, seen = [], set()
    for id_, name, type_, country, region, _area in rows:
        key = id_ if id_ is not None else (name, type_)
        if key in seen:
            continue
        seen.add(key)
        entry = {"name": name, "type": type_, "id": id_}
        # Omitted, not null-filled, when absent — same convention address_at
        # uses for its optional columns (#446).
        if country is not None:
            entry["country"] = country
        if region is not None:
            entry["region"] = region
        chain.append(entry)
    return {"chain": chain}


def _polygon_bbox(coordinates: list) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) over every ring's vertices of a GeoJSON Polygon.

    Holes are included too — they're a subset of the exterior ring's extent,
    so folding them in is harmless and saves distinguishing ring 0 from the
    rest.
    """
    xs = [pt[0] for ring in coordinates for pt in ring]
    ys = [pt[1] for ring in coordinates for pt in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _validate_rings(coordinates) -> None:
    """Raise a clean ValueError unless coordinates is a list of valid rings.

    A valid ring is a non-empty sequence of [lon, lat] pairs (numbers,
    length >= 2). Without this, a malformed polygon (e.g. ring nesting
    omitted: coordinates=[[-73.9, 40.7], ...]) surfaces as a TypeError or
    an empty-sequence ValueError from _polygon_bbox instead of the
    documented clean ValueError.
    """
    if not isinstance(coordinates, (list, tuple)):
        raise ValueError("polygon_geojson coordinates must be a list of rings")
    for ring in coordinates:
        if not isinstance(ring, (list, tuple)) or not ring:
            raise ValueError(
                "polygon_geojson rings must be non-empty lists of [lon, lat] pairs"
            )
        for pt in ring:
            if (
                not isinstance(pt, (list, tuple))
                or len(pt) < 2
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in pt[:2])
            ):
                raise ValueError(
                    "polygon_geojson ring vertices must be [lon, lat] numeric pairs"
                )


def divisions_in_polygon(
    polygon_geojson: dict,
    subtypes: tuple[str, ...] = ("neighborhood", "locality"),
    limit: int = 20,
    con=None,
) -> dict:
    """Divisions of the given subtypes intersecting a GeoJSON polygon, by overlap.

    Returns {"results": [{"id", "name", "subtype", "overlap_fraction"}, ...]},
    ranked by overlap_fraction descending — the fraction of the DIVISION's
    own area (not the input polygon's) that lies inside polygon_geojson,
    rounded to 3 decimals (except that a truly positive overlap too small
    to survive the rounding stays at its unrounded value, so a real
    overlap is never reported as exactly 0.0). An empty results list
    (e.g. a polygon entirely over open ocean) is a valid answer, not an
    error. limit is clamped to overture.MAX_ROWS like every other tool.

    Raises ValueError for a malformed polygon_geojson (wrong type, bad
    ring structure, or a bbox wider than MAX_POLYGON_SPAN_DEG on either
    axis) or limit < 1;
    SchemaDegraded if geometry or subtype is missing from the active
    divisions dataset (mirrors admin_lookup for geometry; subtype is
    additionally essential here because this tool's whole contract is
    filtering by it); UpstreamUnavailable if the remote scan (or the
    one-time spatial extension load) fails.
    """
    if (
        not isinstance(polygon_geojson, dict)
        or polygon_geojson.get("type") != "Polygon"
        or not polygon_geojson.get("coordinates")
    ):
        raise ValueError("polygon_geojson must be a GeoJSON Polygon with coordinates")
    _validate_rings(polygon_geojson["coordinates"])
    if limit < 1:
        raise ValueError("limit must be >= 1")
    limit = min(int(limit), overture.MAX_ROWS)  # same response-size cap every other tool uses
    poly_xmin, poly_ymin, poly_xmax, poly_ymax = _polygon_bbox(polygon_geojson["coordinates"])
    lon_span = poly_xmax - poly_xmin
    lat_span = poly_ymax - poly_ymin
    if lon_span > MAX_POLYGON_SPAN_DEG or lat_span > MAX_POLYGON_SPAN_DEG:
        hint = (
            " (a longitude span over 180 degrees usually means the polygon crosses"
            " the antimeridian; split it into two polygons instead)"
            if lon_span > 180.0
            else ""
        )
        raise ValueError(
            f"polygon_geojson bbox spans {lon_span:.1f} x {lat_span:.1f} degrees; "
            f"the maximum supported extent is {MAX_POLYGON_SPAN_DEG:.1f} degrees "
            f"on either axis{hint}"
        )
    if not subtypes:
        return {"results": []}

    _ensure_spatial()
    upstream = overture._upstream_glob(THEME, type_="division_area")
    missing = set(overture.missing_columns(upstream, POLYGON_REQUIRED_COLUMNS))
    essential_missing = [c for c in missing if c in POLYGON_ESSENTIAL_COLUMNS]
    if essential_missing:
        raise overture.SchemaDegraded(essential_missing)

    name_expr = "NULL" if "names" in missing else "names.primary"
    if "division_id" not in missing:
        id_expr = "coalesce(division_id, id)"
    elif "id" not in missing:
        id_expr = "id"
    else:
        id_expr = "NULL"
    geom_expr = _geom_expr(upstream)

    src = (
        manifest.pruned_source_sql(upstream, (poly_xmin, poly_ymin, poly_xmax, poly_ymax))
        or f"read_parquet('{upstream}', hive_partitioning=1)"
    )
    bbox_prefilter = (
        "bbox.xmin <= $poly_xmax AND bbox.xmax >= $poly_xmin"
        " AND bbox.ymin <= $poly_ymax AND bbox.ymax >= $poly_ymin AND "
        if "bbox" not in missing
        else ""
    )

    sql = f"""
        SELECT
            {id_expr}   AS id,
            {name_expr} AS name,
            subtype,
            ST_Area(ST_Intersection({geom_expr}, ST_GeomFromGeoJSON($poly))) AS overlap_area,
            ST_Area({geom_expr}) AS division_area
        FROM {src}
        WHERE {bbox_prefilter}subtype IN ({",".join(f"$subtype{i}" for i in range(len(subtypes)))})
            AND ST_Intersects({geom_expr}, ST_GeomFromGeoJSON($poly))
    """
    params = {"poly": json.dumps(polygon_geojson)}
    if bbox_prefilter:
        # Only when the prefilter is in the SQL — DuckDB rejects named
        # parameters the statement doesn't reference, so passing these on a
        # bbox-less schema would turn every query into a bogus
        # UpstreamUnavailable.
        params.update(
            {
                "poly_xmin": poly_xmin,
                "poly_ymin": poly_ymin,
                "poly_xmax": poly_xmax,
                "poly_ymax": poly_ymax,
            }
        )
    params.update({f"subtype{i}": s for i, s in enumerate(subtypes)})
    try:
        # Bounded: bbox prefilter + manifest pruning above keep this to the
        # handful of division polygons near the input, not a global scan.
        with trace.scan("divisions in polygon", bounded=True, source=src):
            if con is not None:
                rows = con.execute(sql, params).fetchall()
            else:
                with db.conn_lock:
                    rows = db.shared_conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e

    # Overlap fraction, and the descending sort, happen here rather than in
    # SQL for the same reason admin_lookup sorts client-side: an ORDER BY on
    # a geometry-derived expression forces eager materialization of every
    # row group's geometry, defeating late materialization.
    by_id: dict = {}
    for id_, name, subtype, overlap_area, division_area in rows:
        raw = overlap_area / division_area if division_area else 0.0
        fraction = round(raw, 3)
        if fraction == 0.0 and raw > 0.0:
            # A truly positive overlap must never print as 0.0 — keep the
            # unrounded fraction for slivers below the 3-decimal resolution.
            fraction = raw
        key = id_ if id_ is not None else (name, subtype)
        # division_area carries multiple polygon rows per division (land and
        # maritime variants) — keep the row with the larger overlap fraction.
        existing = by_id.get(key)
        if existing is None or fraction > existing["overlap_fraction"]:
            by_id[key] = {
                "id": id_,
                "name": name,
                "subtype": subtype,
                "overlap_fraction": fraction,
            }
    results = sorted(by_id.values(), key=lambda r: r["overlap_fraction"], reverse=True)
    return {"results": results[:limit]}
