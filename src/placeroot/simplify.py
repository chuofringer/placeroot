"""Geometry simplification targeting a token budget, not a tolerance (#14).

The category's publicly-named unsolved problem is large geospatial payloads:
a real building footprint or admin boundary can carry thousands of vertices,
far more than an agent's context window can afford. Rather than ask the
caller to guess a Douglas-Peucker tolerance in degrees (meaningless without
knowing the local scale, and disconnected from what actually matters — token
cost), simplify_geometry binary-searches the tolerance until the simplified
GeoJSON fits max_tokens, then reports what was lost.

Pure Python, no shapely (not a project dependency, and not needed — RDP over
plain coordinate lists is a few dozen lines). Coordinates are projected to a
local tangent-plane approximation in meters (equirectangular, scaled by
cos(reference latitude)) so both the epsilon search and the reported
max_deviation_m are in real distance units, not degrees.
"""

import math

from placeroot import budget

METERS_PER_DEGREE_LAT = 111_320.0

SUPPORTED_TYPES = {
    "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon",
}

# Points/degenerate lines pass through unsimplified; there's nothing to drop.
_PASSTHROUGH_TYPES = {"Point", "MultiPoint"}

MAX_EXPAND_ITERS = 40
MAX_BISECT_ITERS = 40


class InvalidGeometry(Exception):
    """geojson isn't a recognized/well-formed Point/Line/Polygon geometry."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _perp_dist_m(pt, start, end) -> float:
    """Perpendicular distance from pt to the line through start/end (projected meter coords)."""
    (x, y), (x1, y1), (x2, y2) = pt, start, end
    if (x1, y1) == (x2, y2):
        return math.hypot(x - x1, y - y1)
    num = abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1)
    den = math.hypot(y2 - y1, x2 - x1)
    return num / den


def _rdp_keep_indices(points_m: list[tuple[float, float]], epsilon_m: float) -> list[int]:
    """Ramer-Douglas-Peucker over projected points; keeps indices, always including endpoints."""
    n = len(points_m)
    if n < 3:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        start_i, end_i = stack.pop()
        if end_i - start_i < 2:
            continue
        start, end = points_m[start_i], points_m[end_i]
        dmax, idx = -1.0, -1
        for i in range(start_i + 1, end_i):
            d = _perp_dist_m(points_m[i], start, end)
            if d > dmax:
                dmax, idx = d, i
        if dmax > epsilon_m:
            keep[idx] = True
            stack.append((start_i, idx))
            stack.append((idx, end_i))
    return [i for i in range(n) if keep[i]]


def _max_deviation_m(points_m: list[tuple[float, float]], kept: list[int]) -> float:
    """Max perpendicular distance of any dropped point to its enclosing kept segment."""
    if len(kept) < 2:
        return 0.0
    worst = 0.0
    for a, b in zip(kept, kept[1:]):
        start, end = points_m[a], points_m[b]
        for i in range(a + 1, b):
            d = _perp_dist_m(points_m[i], start, end)
            worst = max(worst, d)
    return worst


def _project(coord, mpd_lon: float, mpd_lat: float) -> tuple[float, float]:
    return coord[0] * mpd_lon, coord[1] * mpd_lat


def _simplify_line(coords: list, epsilon_m: float, mpd_lon: float, mpd_lat: float):
    """Simplify one coordinate list (a LineString or a polygon ring).

    Returns (new_coords, max_deviation_m). Ring closure is preserved for
    free: index 0 and index -1 are always kept by _rdp_keep_indices.
    """
    if len(coords) < 3:
        return list(coords), 0.0
    points_m = [_project(c, mpd_lon, mpd_lat) for c in coords]
    kept = _rdp_keep_indices(points_m, epsilon_m)
    dev = _max_deviation_m(points_m, kept)
    return [coords[i] for i in kept], dev


def _walk(gtype: str, coords, epsilon_m: float, mpd_lon: float, mpd_lat: float):
    """Simplify coords for gtype -> (new_coords, original_points, kept_points, max_deviation_m)."""
    if gtype == "LineString":
        new_line, dev = _simplify_line(coords, epsilon_m, mpd_lon, mpd_lat)
        return new_line, len(coords), len(new_line), dev
    if gtype == "MultiLineString":
        results = [_simplify_line(line, epsilon_m, mpd_lon, mpd_lat) for line in coords]
        new_coords = [r[0] for r in results]
        orig = sum(len(line) for line in coords)
        kept = sum(len(r[0]) for r in results)
        dev = max((r[1] for r in results), default=0.0)
        return new_coords, orig, kept, dev
    if gtype == "Polygon":
        results = [_simplify_line(ring, epsilon_m, mpd_lon, mpd_lat) for ring in coords]
        new_coords = [r[0] for r in results]
        orig = sum(len(ring) for ring in coords)
        kept = sum(len(r[0]) for r in results)
        dev = max((r[1] for r in results), default=0.0)
        return new_coords, orig, kept, dev
    if gtype == "MultiPolygon":
        new_polys, orig, kept, dev = [], 0, 0, 0.0
        for poly in coords:
            results = [_simplify_line(ring, epsilon_m, mpd_lon, mpd_lat) for ring in poly]
            new_polys.append([r[0] for r in results])
            orig += sum(len(ring) for ring in poly)
            kept += sum(len(r[0]) for r in results)
            dev = max(dev, max((r[1] for r in results), default=0.0))
        return new_polys, orig, kept, dev
    raise InvalidGeometry(f"unsupported geometry type: {gtype}")


def _count_points(gtype: str, coords) -> int:
    if gtype == "Point":
        return 1
    if gtype == "MultiPoint":
        return len(coords)
    if gtype == "LineString":
        return len(coords)
    if gtype == "MultiLineString":
        return sum(len(line) for line in coords)
    if gtype == "Polygon":
        return sum(len(ring) for ring in coords)
    if gtype == "MultiPolygon":
        return sum(len(ring) for poly in coords for ring in poly)
    raise InvalidGeometry(f"unsupported geometry type: {gtype}")


def _all_ordinates(gtype: str, coords, axis: int) -> list:
    """All lon (axis=0) or lat (axis=1) values in coords, flattened by nesting depth."""
    if gtype == "Point":
        return [coords[axis]]
    if gtype == "MultiPoint":
        return [c[axis] for c in coords]
    if gtype == "LineString":
        return [c[axis] for c in coords]
    if gtype == "MultiLineString":
        return [c[axis] for line in coords for c in line]
    if gtype == "Polygon":
        return [c[axis] for ring in coords for c in ring]
    if gtype == "MultiPolygon":
        return [c[axis] for poly in coords for ring in poly for c in ring]
    raise InvalidGeometry(f"unsupported geometry type: {gtype}")


def _all_latitudes(gtype: str, coords) -> list[float]:
    return _all_ordinates(gtype, coords, 1)


def _validate(geojson) -> tuple[str, object]:
    if not isinstance(geojson, dict):
        raise InvalidGeometry("geometry must be a GeoJSON object")
    gtype = geojson.get("type")
    if gtype not in SUPPORTED_TYPES:
        raise InvalidGeometry(f"unsupported or missing 'type': {gtype!r}")
    coords = geojson.get("coordinates")
    if coords is None or not isinstance(coords, list):
        raise InvalidGeometry("missing or malformed 'coordinates'")
    try:
        n = _count_points(gtype, coords)
        lons = _all_ordinates(gtype, coords, 0)
        lats = _all_ordinates(gtype, coords, 1)
    except (TypeError, IndexError, KeyError) as e:
        raise InvalidGeometry(f"malformed coordinates for {gtype}: {e}") from e
    if n == 0 or not lats:
        raise InvalidGeometry("geometry has no coordinates")

    def _is_number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if not all(_is_number(v) for v in lons + lats):
        raise InvalidGeometry("coordinates must be numeric [lon, lat] pairs")
    return gtype, coords


def simplify_geometry(geojson: dict, max_tokens: int = 500) -> dict:
    """Simplify geojson's geometry to fit max_tokens (budget.estimate_tokens), reporting the loss.

    Returns {"geometry": <simplified>, "max_deviation_m": ..., "original_points": N,
    "kept_points": M}. Points/MultiPoints pass through unchanged (nothing to
    simplify). Raises InvalidGeometry for malformed input — callers (the MCP
    tool) turn that into a structured error.

    max_tokens budgets the geometry payload itself, not the whole tool
    response envelope; the envelope (counts, deviation) adds only a handful
    of tokens on top.
    """
    gtype, coords = _validate(geojson)

    if gtype in _PASSTHROUGH_TYPES:
        n = _count_points(gtype, coords)
        return {"geometry": geojson, "max_deviation_m": 0.0, "original_points": n, "kept_points": n}

    lats = _all_latitudes(gtype, coords)
    ref_lat = sum(lats) / len(lats)
    mpd_lat = METERS_PER_DEGREE_LAT
    mpd_lon = METERS_PER_DEGREE_LAT * max(math.cos(math.radians(ref_lat)), 1e-6)

    def build(epsilon_m: float):
        new_coords, orig, kept, dev = _walk(gtype, coords, epsilon_m, mpd_lon, mpd_lat)
        geom = {"type": gtype, "coordinates": new_coords}
        return geom, orig, kept, dev

    def fits(geom) -> bool:
        return budget.estimate_tokens({"geometry": geom}) <= max_tokens

    # Already fits unsimplified (epsilon=0 still drops exact duplicate/colinear
    # points, which is a strict improvement, never a loss).
    geom0, orig0, kept0, dev0 = build(0.0)
    if fits(geom0):
        return {
            "geometry": geom0, "max_deviation_m": dev0,
            "original_points": orig0, "kept_points": kept0,
        }

    # Expand epsilon until the result fits, then binary-search down to the
    # smallest epsilon that still fits — token count is (near-)monotonic in
    # epsilon since larger epsilon never keeps more points.
    lo, hi = 0.0, 1.0
    best = None
    for _ in range(MAX_EXPAND_ITERS):
        candidate = build(hi)
        if fits(candidate[0]):
            best = candidate
            break
        hi *= 2.0
    if best is None:
        # Even collapsed to bare endpoints it doesn't fit (pathological — a
        # MultiPolygon with huge part counts). Use the most-simplified result
        # we have; the caller's budget just can't be met for this shape.
        best = candidate

    for _ in range(MAX_BISECT_ITERS):
        mid = (lo + hi) / 2.0
        candidate = build(mid)
        if fits(candidate[0]):
            hi = mid
            best = candidate
        else:
            lo = mid

    geom, orig, kept, dev = best
    return {"geometry": geom, "max_deviation_m": dev, "original_points": orig, "kept_points": kept}
