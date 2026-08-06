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

Design choice, documented rather than hidden: this reuses overture._conn()
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
from functools import lru_cache

import duckdb

from placeroot import overture

logger = logging.getLogger(__name__)

THEME = "divisions"

# Columns admin_lookup depends on. geometry is essential — without it there
# is no point-in-polygon test to run at all, unlike places' softer columns.
REQUIRED_COLUMNS = ["id", "names", "subtype", "geometry"]
ESSENTIAL_COLUMNS = {"geometry"}

_spatial_loaded = False


def _ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once."""
    global _spatial_loaded
    if _spatial_loaded:
        return
    con = overture._conn()
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(f"could not load spatial extension: {e}") from e
    _spatial_loaded = True


@lru_cache(maxsize=8)
def _geom_expr(upstream: str) -> str:
    """SQL expression yielding a GEOMETRY for the dataset's geometry column.

    Real Overture GeoParquet carries geo metadata, so DuckDB spatial reads
    `geometry` as a native GEOMETRY; plain-parquet fixtures store raw WKB
    BLOBs, which need ST_GeomFromWKB. Probe the column type once per glob.
    """
    try:
        with overture._conn_lock:
            (_, type_name) = overture._conn().execute(
                f"DESCRIBE SELECT geometry FROM read_parquet('{upstream}') LIMIT 0"
            ).fetchone()[:2]
    except duckdb.Error:
        # Probe failure means upstream itself is unreachable — let the real
        # query hit the same problem and surface it as UpstreamUnavailable.
        return "ST_GeomFromWKB(geometry)"
    return "geometry" if type_name.upper().startswith("GEOMETRY") else "ST_GeomFromWKB(geometry)"


def admin_lookup(lat: float, lon: float) -> dict:
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
    id_expr = "NULL" if "id" in missing else "id"
    geom_expr = _geom_expr(upstream)

    sql = f"""
        SELECT
            {id_expr}   AS id,
            {name_expr} AS name,
            {subtype_expr} AS type,
            ST_Area({geom_expr}) AS area
        FROM read_parquet('{upstream}', hive_partitioning=1)
        WHERE ST_Contains({geom_expr}, ST_Point($lon, $lat))
        ORDER BY area ASC
    """
    try:
        with overture._conn_lock:
            rows = overture._conn().execute(sql, {"lat": lat, "lon": lon}).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    chain = [{"name": name, "type": type_, "id": id_} for id_, name, type_, _area in rows]
    return {"chain": chain}
