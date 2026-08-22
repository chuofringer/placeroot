"""Offline geometry computations behind the geometry_op MCP tool (issue #361).

Mapbox MCP ships ~18 separate keyless tools for this kind of point/geometry
math. PlaceRoot deliberately bundles them behind one tool, `geometry_op(op,
...)`, to keep schema-token surface small (each extra registered tool costs
schema tokens in every conversation whether or not it's ever called — see
tool_profiles.py's module docstring). This module holds the pure computation;
server.py's geometry_op() is a thin dispatch-and-validate wrapper.

Op catalog (op name -> required params, from the params geometry_op() takes:
point, point2, points, geometry, bearing_deg, distance_m, radius_m):

  distance(point, point2)              -> meters (haversine; same formula/
                                           radius as geo.haversine_m and
                                           distance_matrix)
  bearing(point, point2)               -> initial compass bearing, degrees
  destination(point, bearing_deg,
              distance_m)              -> point reached from point along
                                           bearing_deg for distance_m
  midpoint(point, point2)              -> great-circle midpoint
  area(geometry)                       -> Polygon/MultiPolygon area, m2+km2
  length(geometry)                     -> LineString/MultiLineString length, m
  bbox(geometry)                       -> [xmin, ymin, xmax, ymax]
  centroid(geometry)                   -> a representative point
  buffer(point, radius_m)              -> Polygon, an n-gon approximation of
                                           the radius_m circle around point
  convex_hull(points)                  -> Polygon, the hull of points
  point_in_polygon(points, geometry)   -> per-point booleans (Polygon or
                                           MultiPolygon, holes honored)
  nearest_point(point, points)         -> nearest of points to point
  nearest_point_on_line(point,
                         geometry)     -> snapped point on a LineString

Accuracy notes (worth reading before trusting a large-scale answer):

- Point math (distance/bearing/destination/midpoint) uses exact spherical
  trigonometry (R=6371000, matching geo.haversine_m) — no approximation
  beyond treating Earth as a sphere.
- area and centroid project the geometry to a local tangent-plane
  approximation in meters (equirectangular, longitude scaled by
  cos(reference latitude) — the same projection simplify.py uses for RDP),
  then do planar shoelace math. One reference latitude is used for the whole
  geometry, so accuracy degrades for a polygon spanning many degrees of
  latitude; it's a good approximation at city/regional scale, the scale
  every other tool in this server operates at.
- centroid is vertex-weighted for Point/MultiPoint/LineString/
  MultiLineString (the arithmetic mean of vertices — not length-weighted)
  and area-weighted for Polygon/MultiPolygon (the standard signed-area
  centroid, holes subtracted). Adequate at city scale; not a substitute for
  a real geodesic centroid on a large/irregular shape.
- buffer approximates the radius_m circle as a 32-vertex polygon built from
  32 destination() calls at descending bearings (counterclockwise exterior
  ring, per RFC 7946). It is a polygon *inscribed* in the true circle
  (slightly smaller near the midpoints between vertices), not the circle
  itself. radius_m is capped at MAX_BUFFER_RADIUS_M, and a circle that
  would cross the antimeridian is rejected rather than emitted as a
  self-intersecting ring.
- convex_hull runs the monotone-chain algorithm directly in lon/lat degree
  space. Fine at city scale; a hull spanning a large latitude range is not a
  true geodesic hull (a straight line in lon/lat is not a great-circle arc).
- point_in_polygon uses an even-odd ray-casting test in lon/lat degree
  space, ring by ring (every ring after the first treated as a hole). Same
  small-extent caveat as convex_hull.
- nearest_point_on_line projects the query point and each segment into the
  same local tangent-plane meters used by area/centroid, finds the closest
  point on the closest segment there, then converts back to lon/lat --
  matching geo.closest_point_sql's approach and its away-from-the-equator
  correction.

Geometry inputs get structural validation only: GeoJSON type must be one
this module supports for that op, and 'coordinates' must be a non-empty,
well-shaped list of numeric [lon, lat] pairs. There is no deep per-coordinate
range check (no "-190 is not a valid longitude" here) -- that's deliberately
out of scope for a caller-supplied geometry that's about to be measured, not
looked up; point-like inputs (point, point2, the entries of `points`) get the
real range check via server._invalid_coord instead, since those feed
trig functions where an out-of-range value produces a silently wrong number
rather than an exception.

No new dependencies -- pure Python + math, same posture as simplify.py.
"""

import math

from placeroot import geo, simplify

EARTH_RADIUS_M = 6371000.0
METERS_PER_DEGREE_LAT = 111_320.0

# Geometry types accepted per shape family.
_LINEAR_TYPES = {"LineString", "MultiLineString"}
_AREAL_TYPES = {"Polygon", "MultiPolygon"}
_ANY_TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}

# buffer()'s circle approximation, and the batch caps for point_in_polygon /
# nearest_point -- caller-supplied lists are otherwise an unbounded amount of
# work per call.
BUFFER_VERTICES = 32
MAX_BATCH_POINTS = 100

# buffer() builds a planar ring in lon/lat space; past city/regional scale
# that stops being a meaningful circle (and near the poles or antimeridian it
# stops being a valid ring at all), so cap the radius where the local
# tangent-plane approximation the rest of this module uses still holds.
MAX_BUFFER_RADIUS_M = 1_000_000.0

# The simplify_geometry MCP tool's own default budget (server.py); reused
# here so a geometry-returning op (buffer, convex_hull) is capped the same
# way a caller simplifying that same shape by hand would be.
GEOMETRY_MAX_TOKENS = 500


class InvalidGeometryOp(Exception):
    """A geometry_op() input didn't fit what the requested op needs."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _require_point(point, label: str) -> tuple[float, float]:
    """(lat, lon) from a {"lat":..., "lon":...} dict, or raise InvalidGeometryOp.

    Structural only -- numeric and present. Range validation (server.
    _invalid_coord) is the caller's job before this runs.
    """
    if not isinstance(point, dict):
        raise InvalidGeometryOp(f"{label} must be a {{'lat': ..., 'lon': ...}} object")
    lat, lon = point.get("lat"), point.get("lon")
    if not _is_number(lat) or not _is_number(lon):
        raise InvalidGeometryOp(f"{label} needs numeric 'lat' and 'lon'")
    return float(lat), float(lon)


def _require_points_list(points, label: str) -> list[tuple[float, float]]:
    if not isinstance(points, list) or not points:
        raise InvalidGeometryOp(f"{label} must be a non-empty list of {{'lat','lon'}} points")
    if len(points) > MAX_BATCH_POINTS:
        raise InvalidGeometryOp(
            f"{label} accepts at most {MAX_BATCH_POINTS} points, got {len(points)}"
        )
    return [_require_point(p, f"{label}[{i}]") for i, p in enumerate(points)]


def _validate_geometry(geometry, allowed_types: set[str]) -> tuple[str, object]:
    """Structural check only (type + non-empty, well-shaped, numeric coordinates).

    Reuses simplify.py's coordinate-shape walker (point counting, ordinate
    extraction) so this module and simplify_geometry can't drift on what
    "well-shaped GeoJSON" means, but with its own type allowlist per op
    (simplify.SUPPORTED_TYPES is the full six; a given geometry_op op only
    accepts the subset that makes sense for it -- area doesn't take a
    LineString).
    """
    if not isinstance(geometry, dict):
        raise InvalidGeometryOp("geometry must be a GeoJSON object")
    gtype = geometry.get("type")
    if gtype not in allowed_types:
        raise InvalidGeometryOp(
            f"geometry type must be one of {sorted(allowed_types)}, got {gtype!r}"
        )
    coords = geometry.get("coordinates")
    if coords is None or not isinstance(coords, list):
        raise InvalidGeometryOp("geometry is missing or has malformed 'coordinates'")
    try:
        n = simplify._count_points(gtype, coords)
    except (TypeError, IndexError, KeyError) as e:
        raise InvalidGeometryOp(f"malformed coordinates for {gtype}: {e}") from e
    if n > simplify.MAX_INPUT_POINTS:
        raise InvalidGeometryOp(
            f"geometry has {n} points; the maximum supported is {simplify.MAX_INPUT_POINTS} "
            "(simplify or split the geometry before sending it)"
        )
    try:
        lons = simplify._all_ordinates(gtype, coords, 0)
        lats = simplify._all_ordinates(gtype, coords, 1)
    except (TypeError, IndexError, KeyError) as e:
        raise InvalidGeometryOp(f"malformed coordinates for {gtype}: {e}") from e
    if n == 0 or not lats:
        raise InvalidGeometryOp("geometry has no coordinates")
    if not all(_is_number(v) for v in lons + lats):
        raise InvalidGeometryOp("coordinates must be numeric [lon, lat] pairs")
    return gtype, coords


def _projection(ref_lat: float) -> tuple[float, float]:
    """(meters-per-degree-lon, meters-per-degree-lat) at ref_lat, for local tangent-plane math."""
    mpd_lat = METERS_PER_DEGREE_LAT
    mpd_lon = METERS_PER_DEGREE_LAT * max(math.cos(math.radians(ref_lat)), 1e-6)
    return mpd_lon, mpd_lat


def _project(coord, mpd_lon: float, mpd_lat: float) -> tuple[float, float]:
    return coord[0] * mpd_lon, coord[1] * mpd_lat


def _unproject(xy, mpd_lon: float, mpd_lat: float) -> tuple[float, float]:
    return xy[0] / mpd_lon, xy[1] / mpd_lat


# ---------------------------------------------------------------------------
# Point math
# ---------------------------------------------------------------------------


def distance(point, point2) -> dict:
    lat1, lon1 = _require_point(point, "point")
    lat2, lon2 = _require_point(point2, "point2")
    return {"distance_m": geo.haversine_m(lat1, lon1, lat2, lon2)}


def bearing(point, point2) -> dict:
    lat1, lon1 = _require_point(point, "point")
    lat2, lon2 = _require_point(point2, "point2")
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    theta = math.degrees(math.atan2(y, x))
    return {"bearing_deg": (theta + 360.0) % 360.0}


def destination(point, bearing_deg, distance_m) -> dict:
    if not _is_number(bearing_deg):
        raise InvalidGeometryOp("op=destination needs a numeric bearing_deg")
    if not _is_number(distance_m) or distance_m < 0:
        raise InvalidGeometryOp("op=destination needs a non-negative numeric distance_m")
    lat1, lon1 = _require_point(point, "point")
    phi1, lambda1 = math.radians(lat1), math.radians(lon1)
    theta = math.radians(float(bearing_deg))
    delta = float(distance_m) / EARTH_RADIUS_M
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lat2 = math.degrees(phi2)
    lon2 = (math.degrees(lambda2) + 540.0) % 360.0 - 180.0  # normalize to [-180, 180)
    return {"point": {"lat": lat2, "lon": lon2}}


def midpoint(point, point2) -> dict:
    lat1, lon1 = _require_point(point, "point")
    lat2, lon2 = _require_point(point2, "point2")
    phi1, lambda1 = math.radians(lat1), math.radians(lon1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    bx = math.cos(phi2) * math.cos(dlambda)
    by = math.cos(phi2) * math.sin(dlambda)
    phim = math.atan2(
        math.sin(phi1) + math.sin(phi2),
        math.sqrt((math.cos(phi1) + bx) ** 2 + by**2),
    )
    lambdam = lambda1 + math.atan2(by, math.cos(phi1) + bx)
    latm = math.degrees(phim)
    lonm = (math.degrees(lambdam) + 540.0) % 360.0 - 180.0
    return {"point": {"lat": latm, "lon": lonm}}


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------


def _ring_area_m2(ring, mpd_lon: float, mpd_lat: float) -> float:
    """Signed planar shoelace area of one ring, in local tangent-plane meters."""
    pts = [_project(c, mpd_lon, mpd_lat) for c in ring]
    if len(pts) < 3:
        return 0.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _polygon_area_m2(rings, mpd_lon: float, mpd_lat: float) -> float:
    """Outer ring area minus holes (rings[1:]), all taken as unsigned regardless of winding."""
    if not rings:
        return 0.0
    area = abs(_ring_area_m2(rings[0], mpd_lon, mpd_lat))
    for hole in rings[1:]:
        area -= abs(_ring_area_m2(hole, mpd_lon, mpd_lat))
    return max(area, 0.0)


def area(geometry) -> dict:
    gtype, coords = _validate_geometry(geometry, _AREAL_TYPES)
    lats = simplify._all_latitudes(gtype, coords)
    ref_lat = sum(lats) / len(lats)
    mpd_lon, mpd_lat = _projection(ref_lat)
    if gtype == "Polygon":
        area_m2 = _polygon_area_m2(coords, mpd_lon, mpd_lat)
    else:  # MultiPolygon
        area_m2 = sum(_polygon_area_m2(poly, mpd_lon, mpd_lat) for poly in coords)
    return {"area_m2": area_m2, "area_km2": area_m2 / 1_000_000.0}


def length(geometry) -> dict:
    gtype, coords = _validate_geometry(geometry, _LINEAR_TYPES)
    lines = coords if gtype == "MultiLineString" else [coords]
    total_m = 0.0
    for line in lines:
        for (lon1, lat1), (lon2, lat2) in zip(line, line[1:]):
            total_m += geo.haversine_m(lat1, lon1, lat2, lon2)
    return {"length_m": total_m}


def bbox(geometry) -> dict:
    gtype, coords = _validate_geometry(geometry, _ANY_TYPES)
    lons = simplify._all_ordinates(gtype, coords, 0)
    lats = simplify._all_ordinates(gtype, coords, 1)
    return {"bbox": [min(lons), min(lats), max(lons), max(lats)]}


def _vertex_centroid(gtype: str, coords) -> tuple[float, float]:
    lons = simplify._all_ordinates(gtype, coords, 0)
    lats = simplify._all_ordinates(gtype, coords, 1)
    return sum(lons) / len(lons), sum(lats) / len(lats)


def _ring_area_and_centroid_m(ring, mpd_lon, mpd_lat):
    """(signed_area, (cx, cy)) of one ring's area-weighted centroid, in projected meters."""
    pts = [_project(c, mpd_lon, mpd_lat) for c in ring]
    if len(pts) < 3:
        return 0.0, (pts[0] if pts else (0.0, 0.0))
    a_sum, cx, cy = 0.0, 0.0, 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        cross = x1 * y2 - x2 * y1
        a_sum += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    signed_area = a_sum / 2.0
    if signed_area == 0.0:
        # Degenerate ring (zero-area) -- fall back to the vertex mean rather
        # than dividing by zero.
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return 0.0, (sum(xs) / len(xs), sum(ys) / len(ys))
    cx /= 6.0 * signed_area
    cy /= 6.0 * signed_area
    return signed_area, (cx, cy)


def _polygon_centroid_m(rings, mpd_lon, mpd_lat):
    """Area-weighted centroid of a polygon (outer minus holes), in projected meters."""
    if not rings:
        return 0.0, 0.0
    outer_area, (ocx, ocy) = _ring_area_and_centroid_m(rings[0], mpd_lon, mpd_lat)
    outer_area = abs(outer_area)
    total_area, wx, wy = outer_area, ocx * outer_area, ocy * outer_area
    for hole in rings[1:]:
        hole_area, (hcx, hcy) = _ring_area_and_centroid_m(hole, mpd_lon, mpd_lat)
        hole_area = abs(hole_area)
        total_area -= hole_area
        wx -= hcx * hole_area
        wy -= hcy * hole_area
    if total_area <= 0.0:
        return ocx, ocy
    return wx / total_area, wy / total_area


def centroid(geometry) -> dict:
    gtype, coords = _validate_geometry(geometry, _ANY_TYPES)
    if gtype in ("Point", "MultiPoint", "LineString", "MultiLineString"):
        lon, lat = _vertex_centroid(gtype, coords)
        return {"point": {"lat": lat, "lon": lon}}

    lats = simplify._all_latitudes(gtype, coords)
    ref_lat = sum(lats) / len(lats)
    mpd_lon, mpd_lat = _projection(ref_lat)

    if gtype == "Polygon":
        cx, cy = _polygon_centroid_m(coords, mpd_lon, mpd_lat)
    else:  # MultiPolygon: area-weighted average of each part's centroid
        parts = []
        for poly in coords:
            outer_area = abs(_ring_area_m2(poly[0], mpd_lon, mpd_lat)) if poly else 0.0
            hole_area = sum(abs(_ring_area_m2(h, mpd_lon, mpd_lat)) for h in poly[1:])
            part_area = max(outer_area - hole_area, 0.0)
            pcx, pcy = _polygon_centroid_m(poly, mpd_lon, mpd_lat)
            parts.append((part_area, pcx, pcy))
        total_area = sum(p[0] for p in parts)
        if total_area > 0.0:
            cx = sum(p[0] * p[1] for p in parts) / total_area
            cy = sum(p[0] * p[2] for p in parts) / total_area
        else:
            cx = sum(p[1] for p in parts) / len(parts)
            cy = sum(p[2] for p in parts) / len(parts)

    lon, lat = _unproject((cx, cy), mpd_lon, mpd_lat)
    return {"point": {"lat": lat, "lon": lon}}


# ---------------------------------------------------------------------------
# Derive (geometry-returning)
# ---------------------------------------------------------------------------


def _budget_simplify(geom: dict) -> dict:
    """Run geom through simplify_geometry's token-fit search (issue #361's
    "respect the existing simplify machinery" requirement) and fold the
    result into the op's response shape."""
    fitted = simplify.simplify_geometry(geom, GEOMETRY_MAX_TOKENS)
    out = {"geometry": fitted["geometry"]}
    if fitted["kept_points"] < fitted["original_points"]:
        out["max_deviation_m"] = fitted["max_deviation_m"]
        out["original_points"] = fitted["original_points"]
        out["kept_points"] = fitted["kept_points"]
    return out


def buffer(point, radius_m) -> dict:
    if not _is_number(radius_m) or radius_m <= 0:
        raise InvalidGeometryOp("op=buffer needs a positive numeric radius_m")
    if radius_m > MAX_BUFFER_RADIUS_M:
        raise InvalidGeometryOp(
            f"op=buffer radius_m must be at most {MAX_BUFFER_RADIUS_M:.0f} m; "
            "beyond that scale a planar lon/lat ring is not a meaningful circle"
        )
    lat, lon = _require_point(point, "point")
    ring = []
    for i in range(BUFFER_VERTICES):
        # Descending bearings so the exterior ring winds counterclockwise
        # (RFC 7946 exterior-ring winding, matching convex_hull's output).
        b = (360.0 * (BUFFER_VERTICES - i) / BUFFER_VERTICES) % 360.0
        d = destination({"lat": lat, "lon": lon}, b, float(radius_m))["point"]
        ring.append([d["lon"], d["lat"]])
    ring.append(ring[0])
    # destination() normalizes longitudes to [-180, 180), so a circle that
    # crosses the antimeridian shows up as an adjacent-vertex lon jump of
    # more than 180 degrees -- a self-intersecting ring that every planar op
    # in this module (area, point_in_polygon, ...) would silently mismeasure.
    # Reject it rather than return garbage.
    for (lon1, _), (lon2, _) in zip(ring, ring[1:]):
        if abs(lon2 - lon1) > 180.0:
            raise InvalidGeometryOp(
                "buffer crosses the antimeridian; this planar ring approximation "
                "cannot represent it -- use a center farther from lon ±180 or a "
                "smaller radius_m"
            )
    return _budget_simplify({"type": "Polygon", "coordinates": [ring]})


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _monotone_chain_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Convex hull (lon, lat) via the monotone chain algorithm; input already deduped+sorted."""
    if len(pts) <= 2:
        return pts

    def half(seq):
        hull: list[tuple[float, float]] = []
        for p in seq:
            while len(hull) >= 2 and _cross(hull[-2], hull[-1], p) <= 0:
                hull.pop()
            hull.append(p)
        return hull

    lower = half(pts)
    upper = half(list(reversed(pts)))
    return lower[:-1] + upper[:-1]


def convex_hull(points) -> dict:
    pts = _require_points_list(points, "points")
    lonlat = sorted({(lon, lat) for lat, lon in pts})
    if len(lonlat) < 3:
        raise InvalidGeometryOp(
            f"op=convex_hull needs at least 3 distinct points, got {len(lonlat)}"
        )
    hull = _monotone_chain_hull(lonlat)
    if len(hull) < 3:
        raise InvalidGeometryOp("op=convex_hull points are collinear; no polygon hull exists")
    ring = [[lon, lat] for lon, lat in hull]
    ring.append(ring[0])
    return _budget_simplify({"type": "Polygon", "coordinates": [ring]})


# ---------------------------------------------------------------------------
# Predicates / selection
# ---------------------------------------------------------------------------


def _ring_contains(lon: float, lat: float, ring) -> bool:
    """Even-odd ray casting for one ring, in lon/lat degree space."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat):
            x_at_lat = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_at_lat:
                inside = not inside
    return inside


def _polygon_contains(lon: float, lat: float, rings) -> bool:
    if not rings or not _ring_contains(lon, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if _ring_contains(lon, lat, hole):
            return False
    return True


def point_in_polygon(points, geometry) -> dict:
    pts = _require_points_list(points, "points")
    gtype, coords = _validate_geometry(geometry, _AREAL_TYPES)
    polys = coords if gtype == "MultiPolygon" else [coords]
    results = []
    for lat, lon in pts:
        results.append(any(_polygon_contains(lon, lat, poly) for poly in polys))
    return {"results": results}


def nearest_point(point, points) -> dict:
    lat, lon = _require_point(point, "point")
    pts = _require_points_list(points, "points")
    best_idx, best_dist = -1, math.inf
    for i, (plat, plon) in enumerate(pts):
        d = geo.haversine_m(lat, lon, plat, plon)
        if d < best_dist:
            best_idx, best_dist = i, d
    return {"index": best_idx, "distance_m": best_dist}


def nearest_point_on_line(point, geometry) -> dict:
    lat, lon = _require_point(point, "point")
    gtype, coords = _validate_geometry(geometry, {"LineString"})
    if len(coords) < 2:
        raise InvalidGeometryOp("op=nearest_point_on_line needs a LineString with >=2 points")

    mpd_lon, mpd_lat = _projection(lat)
    qx, qy = lon * mpd_lon, lat * mpd_lat
    proj = [_project(c, mpd_lon, mpd_lat) for c in coords]

    seg_lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(proj, proj[1:])]
    total_length = sum(seg_lengths)

    best = None  # (dist_m, snapped_xy, cumulative_length_at_snap)
    cumulative = 0.0
    for i, ((x1, y1), (x2, y2)) in enumerate(zip(proj, proj[1:])):
        seg_len = seg_lengths[i]
        if seg_len == 0.0:
            t = 0.0
        else:
            t = ((qx - x1) * (x2 - x1) + (qy - y1) * (y2 - y1)) / (seg_len**2)
            t = max(0.0, min(1.0, t))
        sx, sy = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
        dist = math.hypot(qx - sx, qy - sy)
        if best is None or dist < best[0]:
            best = (dist, (sx, sy), cumulative + t * seg_len)
        cumulative += seg_len

    dist_m, (sx, sy), length_at_snap = best
    snapped_lon, snapped_lat = _unproject((sx, sy), mpd_lon, mpd_lat)
    fraction = length_at_snap / total_length if total_length > 0.0 else 0.0
    return {
        "point": {"lat": snapped_lat, "lon": snapped_lon},
        "distance_m": dist_m,
        "fraction": fraction,
    }
