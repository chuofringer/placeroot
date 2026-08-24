"""union/intersect/difference for the geometry_op MCP tool (issue #404).

geometry_ops.py is deliberately connection-free -- its module docstring says
so explicitly ("No new dependencies -- pure Python + math, same posture as
simplify.py") and every op in it is exact spherical trig or planar shoelace
math with no upstream/database dependency. General polygon clipping
(union/intersection/difference of arbitrary, possibly multi-part,
possibly-holed rings) is exactly the kind of thing that pure-Python
implementation gets wrong at the edges -- degenerate rings, touching-but-not-
overlapping inputs, multi-part results -- so this module takes the route
issue #404 and area_suggest.py's intersect_sheds already established: reuse
the DuckDB spatial extension (`ST_Union`/`ST_Intersection`/`ST_Difference`)
that's already loaded and already trusted for exactly this kind of op
(divisions.py's overlap queries, area_suggest.intersect_sheds), rather than
hand-rolling a second (and inevitably buggier) clipping algorithm in Python.

This mirrors area_suggest.py's own split: the pure computation that needs a
live connection lives in its own small module; server.py's geometry_op()
stays the thin dispatch-and-validate wrapper, now importing from here for
the three set ops instead of from geometry_ops for those three.

Structural geometry validation, the vertex-budget simplification pass, and
the InvalidGeometryOp error type are all reused from geometry_ops.py rather
than duplicated -- this module only adds the DuckDB round trip.
"""

from __future__ import annotations

import json

import duckdb

from placeroot import db, geometry_ops

InvalidGeometryOp = geometry_ops.InvalidGeometryOp

_OPS = {
    "union": "ST_Union",
    "intersect": "ST_Intersection",
    "difference": "ST_Difference",
}

# Empty-result explanations, keyed the same way.
_EMPTY_NOTE = {
    "intersect": "geometry and geometry2 do not overlap",
    "difference": "geometry is fully covered by geometry2",
}


def _set_op(op: str, geometry, geometry2) -> dict:
    """Shared implementation for union/intersect/difference.

    Both inputs get geometry_ops' own structural validation (Polygon/
    MultiPolygon only), then a single DuckDB round trip computes the
    result. area_km2 is computed with geometry_ops.area() on the
    *unsimplified* result so the reported figure matches what op=area would
    say about the same shape, not a decimated approximation of it; only the
    returned `geometry` is passed through the budget-simplify pass ops like
    buffer/convex_hull already use, so a caller never gets an unbounded
    coordinate dump.
    """
    geometry_ops._validate_geometry(geometry, geometry_ops._AREAL_TYPES)
    geometry_ops._validate_geometry(geometry2, geometry_ops._AREAL_TYPES)

    db.ensure_spatial()
    sql_op = _OPS[op]
    sql = (
        f"SELECT ST_AsGeoJSON({sql_op}(ST_GeomFromGeoJSON($g1), ST_GeomFromGeoJSON($g2))) "
        "AS result"
    )
    params = {"g1": json.dumps(geometry), "g2": json.dumps(geometry2)}
    try:
        with db.conn_lock:
            row = db.shared_conn().execute(sql, params).fetchone()
    except duckdb.Error as e:
        raise InvalidGeometryOp(
            f"op={op} could not process this geometry pair (invalid or "
            f"self-intersecting polygon?): {e}"
        ) from e

    geojson_str = row[0] if row else None
    if not geojson_str:
        return {"empty": True, "note": _EMPTY_NOTE.get(op, "the result geometry is empty")}
    result_geom = json.loads(geojson_str)
    rtype = result_geom.get("type")

    if rtype not in ("Polygon", "MultiPolygon") or not result_geom.get("coordinates"):
        # GeometryCollection (DuckDB's empty-result shape), LineString, Point,
        # or a Polygon/MultiPolygon with an empty coordinates list -- a
        # sliver or a genuinely empty result (disjoint intersect, a
        # difference that consumes the whole input).
        return {"empty": True, "note": _EMPTY_NOTE.get(op, "the result geometry is empty")}

    area_km2 = geometry_ops.area(result_geom)["area_km2"]
    if area_km2 <= 0:
        return {"empty": True, "note": _EMPTY_NOTE.get(op, "the result geometry is empty")}

    out = geometry_ops._budget_simplify(result_geom)
    out["area_km2"] = area_km2
    return out


def union(geometry, geometry2) -> dict:
    """Union of two Polygon/MultiPolygon geometries -> {"geometry", "area_km2"}.

    Always non-empty for two well-formed inputs (a union of non-empty shapes
    is never empty). Two disjoint inputs come back as a MultiPolygon.
    """
    return _set_op("union", geometry, geometry2)


def intersect(geometry, geometry2) -> dict:
    """Intersection of two Polygon/MultiPolygon geometries.

    -> {"geometry", "area_km2"}, or {"empty": True, "note": ...} when the
    two inputs don't overlap at all -- a valid, honest answer, not an error.
    """
    return _set_op("intersect", geometry, geometry2)


def difference(geometry, geometry2) -> dict:
    """geometry minus geometry2 -> {"geometry", "area_km2"}.

    -> {"empty": True, "note": ...} when geometry2 fully covers geometry.
    When the two inputs are disjoint the result is geometry unchanged
    (subject to the same budget-simplify pass every geometry-returning op
    gets).
    """
    return _set_op("difference", geometry, geometry2)
