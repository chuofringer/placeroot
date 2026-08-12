"""Point -> containing admin hierarchy against Overture's divisions theme (issue #11).

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

import logging

import duckdb

from placeroot import db, geo, overture

logger = logging.getLogger(__name__)

THEME = "divisions"

# Columns admin_lookup depends on. geometry is essential — without it there
# is no point-in-polygon test to run at all, unlike places' softer columns.
REQUIRED_COLUMNS = ["id", "names", "subtype", "geometry", "bbox", "division_id"]
ESSENTIAL_COLUMNS = {"geometry"}


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

    Returns {"chain": [{"name", "type", "id"}, ...]} ordered
    neighborhood -> locality -> county -> region -> country (whichever
    levels actually contain the point; an empty chain means no division in
    the active dataset contains it, which is a valid answer, not an error).

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
    # The bbox column prunes remote row groups before the (expensive) exact
    # containment test — without it this is an ST_Contains over every
    # division polygon on Earth (measured 10+ minutes live).
    bbox_prefilter = (
        "bbox.xmin <= $lon AND bbox.xmax >= $lon"
        " AND bbox.ymin <= $lat AND bbox.ymax >= $lat AND "
        if "bbox" not in missing
        else ""
    )

    sql = f"""
        SELECT
            {id_expr}   AS id,
            {name_expr} AS name,
            {subtype_expr} AS type,
            ST_Area({geom_expr}) AS area
        FROM read_parquet('{upstream}', hive_partitioning=1)
        WHERE {bbox_prefilter}ST_Contains({geom_expr}, ST_Point($lon, $lat))
        ORDER BY area ASC
    """
    try:
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
    # division_area carries multiple polygon rows per division (e.g. land and
    # maritime variants) — keep the first (smallest-area) row per division.
    chain, seen = [], set()
    for id_, name, type_, _area in rows:
        key = id_ if id_ is not None else (name, type_)
        if key in seen:
            continue
        seen.add(key)
        chain.append({"name": name, "type": type_, "id": id_})
    return {"chain": chain}
