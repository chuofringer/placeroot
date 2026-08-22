"""Pure/testable helpers for suggest_areas (issue #350) — the composed tool
that turns anchor points + travel budgets + amenity requirements into a
ranked shortlist of candidate neighborhoods.

suggest_areas is the user-facing hero feature #305 breaks down into three
sub-issues: #348 (divisions.divisions_in_polygon — polygon -> candidate
localities), #349 (area_score.score_locality — locality -> per-requirement
scores), and this one (#350), which composes them:

    1. Per anchor, routing.isochrone gives the reachable shed.
    2. Multiple anchors' sheds are intersected (this module,
       intersect_sheds) — a candidate must be reachable within EVERY
       anchor's budget, not just one of them.
    3. divisions.divisions_in_polygon partitions the (intersected) shed into
       candidate localities.
    4. area_score.score_locality scores each candidate against the
       requirements.
    5. build_reason (this module) turns one candidate's scored requirements
       into a one-line, honest summary.

This module holds only the geometry (intersect_sheds) and text-summary
(build_reason) pieces that don't need a live upstream connection to test in
isolation; server.py's suggest_areas owns the orchestration (confirm gate,
per-anchor isochrone calls, per-candidate scoring/routing fan-out, error
mapping) the same way meeting.py holds meeting_point's pure geometry while
server.py owns its orchestration.
"""

from __future__ import annotations

import json

import duckdb

from placeroot import db

# suggest_areas anchors: at least 1 (a single origin's shed), at most this
# many. Each anchor costs its own isochrone graph build/extraction (unlike
# meeting_point, where one graph often serves several origin-destination
# pairs) — a small, documented cap keeps a cold multi-anchor call bounded
# even after the caller has agreed to wait via confirm=true.
MAX_ANCHORS = 3

# Ranked shortlist size: default 5 (issue #350's proposed shape), clamped to
# this range. The candidate fetch from divisions_in_polygon is itself capped
# a bit above `limit` (see server.py) so there is something to rank, but
# never past overture.MAX_ROWS.
MIN_LIMIT = 1
MAX_LIMIT = 10

# intersect_sheds' worst case (a multiply-disconnected intersection, e.g.
# two anchors' sheds crossing a river at several bridges) queries
# divisions_in_polygon once per part. Capped so that pathological geometry
# can't turn one suggest_areas call into an unbounded fan-out of scans; in
# the ordinary case (overlapping, mostly-convex sheds) there is exactly one
# part.
MAX_SHED_PARTS = 5


def intersect_sheds(polygons: list[dict]) -> list[dict]:
    """Geometric intersection of 1+ GeoJSON Polygon sheds, as a list of parts.

    A single polygon is returned unchanged, wrapped in a one-element list.
    Two or more are intersected with DuckDB's spatial extension
    (ST_Intersection) the same way divisions.py already computes overlap —
    reusing the one already-loaded spatial connection rather than pulling in
    a second geometry library. An intersection that spans multiple
    disconnected pieces (e.g. two anchors' sheds overlapping on both sides
    of a river) comes back as a MultiPolygon; this returns each part as its
    own Polygon geojson dict so the caller (divisions_in_polygon, which only
    accepts a Polygon) can query each part and merge results, rather than
    silently widening the shape to a hull that would let in areas reachable
    from only one anchor.

    Returns an empty list when the sheds do not overlap at all (a genuinely
    empty intersection) — a valid answer, not an error; the caller turns
    this into an empty results list with an explanatory note.
    """
    if not polygons:
        return []
    if len(polygons) == 1:
        return [polygons[0]]

    db.ensure_spatial()
    expr = "ST_GeomFromGeoJSON($p0)"
    params: dict = {"p0": json.dumps(polygons[0])}
    for i, poly in enumerate(polygons[1:], start=1):
        expr = f"ST_Intersection({expr}, ST_GeomFromGeoJSON($p{i}))"
        params[f"p{i}"] = json.dumps(poly)
    sql = f"SELECT ST_Area({expr}), ST_AsGeoJSON({expr})"
    try:
        with db.conn_lock:
            area, geojson_str = db.shared_conn().execute(sql, params).fetchone()
    except duckdb.Error:
        # A malformed or degenerate shed polygon (e.g. from an isochrone
        # fallback shape with < 3 distinct points) can't be intersected —
        # treat it the same as "no overlap" rather than raising: the caller
        # already has a perfectly good per-anchor isochrone answer, this is
        # only the composition step.
        return []
    if not area or area <= 0 or geojson_str is None:
        return []
    geom = json.loads(geojson_str)
    if geom.get("type") == "Polygon":
        return [geom]
    if geom.get("type") == "MultiPolygon":
        parts = [{"type": "Polygon", "coordinates": c} for c in geom["coordinates"]]
        return parts[:MAX_SHED_PARTS]
    # GeometryCollection, LineString, Point (a sliver that degenerated under
    # intersection) — no area worth partitioning into localities.
    return []


def build_reason(scored_requirements: list[dict]) -> str:
    """One-line, honest summary of a candidate's scored requirements.

    Ranks the measurable requirements by score and names the strongest and
    (if different) weakest, so "why this candidate" is legible without
    reading the full requirements array. A candidate with no measurable
    requirement at all (every requirement was subjective or unmatched) gets
    a note saying so rather than a fabricated verdict — same honesty rule
    area_score.score_locality itself follows.
    """
    measurable = [r for r in scored_requirements if r.get("measurable")]
    if not measurable:
        return (
            "No requirement here was measurable from open data "
            "(subjective, or no matching category) — see requirements for detail."
        )
    ranked = sorted(measurable, key=lambda r: r["score"], reverse=True)
    if len(ranked) == 1 or ranked[0]["score"] == ranked[-1]["score"]:
        best = ranked[0]
        return f"{best['label']}: score {best['score']} within the search radius."
    best, worst = ranked[0], ranked[-1]
    return (
        f"Strong on {best['label']} (score {best['score']}); "
        f"weaker on {worst['label']} (score {worst['score']})."
    )
