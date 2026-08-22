"""Self-contained HTML map / one-pager renderer (issues #15, #34, #310).

Turns find_places / summarize_area JSON — or caller-supplied GeoJSON — into
ONE standalone HTML file: inline CSS, inline JS, vector data embedded, no
CDN, no tile server, no API key, zero network requests when opened. The
viewer is a small hand-written SVG pan/zoom map (equirectangular projection,
cos(lat) corrected so both axes share one meters-per-unit scale) with marker
dots, polygon/line shapes, click-to-open popups, a scale bar, and an
attribution line — not an embedded copy of Leaflet/MapLibre.

Issue #310 extends the artifact from a map-only viewer into a shareable
one-pager: the interactive map, a composed verdict, a per-stop detail
list, and the required data attribution — a local file the user can send
to a spouse, co-founder, or landlord. export_report() is the named entry
point; render_map / write_artifact produce the same document.

Beyond Point features, the renderer understands Polygon, MultiPolygon,
LineString and MultiLineString geometry (from caller GeoJSON, and from
routing.isochrone()'s {"polygon": <geojson>, "stats": {...}, ...} result
shape). Polygons with holes are rendered as one SVG path with fill-rule
evenodd — the ring winding order Overture/GeoJSON producers use doesn't
matter, only that holes are listed as additional rings.

The MCP tool boundary (server.render_map) returns only a tiny
{"path", "bytes", "features_rendered", "skipped_features"} envelope (plus
"truncated": True when applicable) so the (potentially large) HTML never
counts against the token budget in CONTRIBUTING.md's "answers, not data"
rule — the file on disk is the artifact.

Vertex cap (#74, defense in depth): render_map's cost is linear in feature/
vertex count, so a caller-supplied GeoJSON with no inherent size limit could
still produce an unbounded HTML file. write_artifact truncates points+shapes
to MAX_RENDER_VERTICES total vertices (_cap_vertices), folding any dropped
items into skipped_features and setting "truncated": True — see
MAX_RENDER_VERTICES's docstring.
"""

import html
import json
import math
import os
import re
import secrets
import time
from pathlib import Path

from placeroot import cache

# Overture's divisions/transportation/base themes are substantially
# OSM-derived (ODbL), so a rendered map is a "produced work" and needs the
# OSM credit alongside Overture's — see docs/DATA-LICENSE.md.
ATTRIBUTION = "© Overture Maps Foundation · © OpenStreetMap contributors (ODbL)"

# Returning the HTML inline (inline=True) is only offered under this size —
# above it the point of keeping the tool response small is defeated.
INLINE_MAX_BYTES = 300_000

# Defense in depth (#74): render_map is linear in feature/vertex count (more
# points/rings -> a proportionally bigger HTML file and a slower browser-side
# SVG render), so this is low severity, not a fix for an observed cost — but
# caller-supplied GeoJSON has no inherent size limit the way find_places/
# summarize_area/isochrone results do. MAX_RENDER_VERTICES caps the total
# vertex count write_artifact will draw (points count as 1 each; a shape
# counts its ring/line coordinate total) before it truncates rather than
# writing an unbounded artifact. Chosen generously above any realistic tool
# output (isochrone polygons are capped to MAX_POLYGON_POINTS ~= 100 in
# routing.py; find_places results are page-sized) but finite.
MAX_RENDER_VERTICES = 50_000

# A composed verdict is meant to be read by a human on a one-pager, not a
# novel. Agents that dump raw tool JSON into summary get clipped so the
# file stays sendable.
SUMMARY_MAX_CHARS = 8_000

# Shape style roles (#368): a shape feature's props may carry "role": "shed"
# (travel-time shed) or "role": "outline" (compared-area boundary) to switch
# its SVG styling; any other value (or none) keeps today's default style.
_SHAPE_ROLES = ("shed", "outline")

# Shape "label"/"callout" props are a short name + one-line verdict rendered
# as an SVG text chip over the shape (#368) — capped like SUMMARY_MAX_CHARS
# caps the verdict, so a caller can't turn a shape annotation into a wall of
# text on the map.
SHAPE_LABEL_MAX_CHARS = 40
SHAPE_CALLOUT_MAX_CHARS = 80

_ARTIFACT_DIRNAME = "artifacts"


def artifact_dir() -> Path:
    """Directory render_map writes HTML files into.

    Overridable via PLACEROOT_ARTIFACT_DIR; defaults to a sibling of the
    tile cache directory (cache.cache_dir()) so there's one on-disk root to
    reason about rather than a second default location to document.
    """
    override = os.environ.get("PLACEROOT_ARTIFACT_DIR")
    if override:
        return Path(override)
    return cache.cache_dir().parent / _ARTIFACT_DIRNAME


def _as_float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _point_from_row(row: dict) -> dict | None:
    """A find_places-style row -> {lat, lon, name, props}, or None if unusable."""
    lat = _as_float(row.get("lat"))
    lon = _as_float(row.get("lon"))
    if lat is None or lon is None:
        return None
    props = {k: v for k, v in row.items() if k not in ("lat", "lon", "name") and v is not None}
    return {"lat": lat, "lon": lon, "name": row.get("name"), "props": props}


def _point_from_geojson_feature(feature: dict) -> dict | None:
    """A GeoJSON Feature with Point geometry -> {lat, lon, name, props}, or None."""
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "Point":
        return None
    coords = geometry.get("coordinates") or []
    if len(coords) < 2:
        return None
    lon = _as_float(coords[0])
    lat = _as_float(coords[1])
    if lat is None or lon is None:
        return None
    props = dict(feature.get("properties") or {})
    return {"lat": lat, "lon": lon, "name": props.get("name"), "props": props}


def extract_points(data) -> list[dict]:
    """Normalize a tool result or caller GeoJSON into a flat list of points.

    Recognizes, roughly in this order:
      - {"results": [rows...]}                            find_places output
      - {"type": "FeatureCollection", "features": [...]}  or {"features": [...]}
      - {"center": {"lat": ..., "lon": ...}, ...}          summarize_area output
        (rendered as one marker whose popup carries the summary fields)
      - a single row dict or a single GeoJSON Feature dict
      - a bare list of any of the above row/feature shapes

    Rows or features missing usable numeric lat/lon are silently skipped —
    render_map degrades to fewer markers rather than failing outright.
    """
    if data is None:
        return []
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            points = (_point_from_row(r) for r in data["results"] if isinstance(r, dict))
            return [p for p in points if p]
        if isinstance(data.get("features"), list):
            points = (
                _point_from_geojson_feature(f) for f in data["features"] if isinstance(f, dict)
            )
            return [p for p in points if p]
        if isinstance(data.get("center"), dict):
            lat = _as_float(data["center"].get("lat"))
            lon = _as_float(data["center"].get("lon"))
            if lat is None or lon is None:
                return []
            props = {k: v for k, v in data.items() if k != "center"}
            return [{"lat": lat, "lon": lon, "name": "Area summary", "props": props}]
        if data.get("geometry"):
            point = _point_from_geojson_feature(data)
            return [point] if point else []
        point = _point_from_row(data)
        return [point] if point else []
    if isinstance(data, list):
        points = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("geometry"):
                point = _point_from_geojson_feature(item)
            else:
                point = _point_from_row(item)
            if point:
                points.append(point)
        return points
    return []


# --- shape (Polygon/MultiPolygon/LineString/MultiLineString) extraction ----

_SHAPE_GEOMETRY_TYPES = ("Polygon", "MultiPolygon", "LineString", "MultiLineString")


def _validate_ring(coords) -> list[list[float]] | None:
    """A GeoJSON linear ring -> [[lon, lat], ...], or None if malformed.

    Requires at least 4 positions (3 distinct + closing point, per the
    GeoJSON spec) each with numeric, finite lon/lat.
    """
    if not isinstance(coords, list) or len(coords) < 4:
        return None
    ring = []
    for c in coords:
        if not isinstance(c, (list, tuple)) or len(c) < 2:
            return None
        lon = _as_float(c[0])
        lat = _as_float(c[1])
        if lon is None or lat is None:
            return None
        ring.append([lon, lat])
    return ring


def _validate_polygon_rings(coords) -> list[list[list[float]]] | None:
    """A Polygon's `coordinates` (list of rings) -> validated rings, or None."""
    if not isinstance(coords, list) or not coords:
        return None
    rings = []
    for ring_coords in coords:
        ring = _validate_ring(ring_coords)
        if ring is None:
            return None
        rings.append(ring)
    return rings


def _validate_line(coords) -> list[list[float]] | None:
    """A LineString's `coordinates` -> [[lon, lat], ...], or None if malformed."""
    if not isinstance(coords, list) or len(coords) < 2:
        return None
    line = []
    for c in coords:
        if not isinstance(c, (list, tuple)) or len(c) < 2:
            return None
        lon = _as_float(c[0])
        lat = _as_float(c[1])
        if lon is None or lat is None:
            return None
        line.append([lon, lat])
    return line


def _shape_from_geometry(geometry: dict, name: str | None, props: dict) -> dict | None:
    """geometry dict (already known to be Polygon/MultiPolygon/Line*) -> a render-ready shape.

    Polygon rendering only needs a flat bag of rings (outer + holes) drawn
    with fill-rule=evenodd, so MultiPolygon parts are flattened into one
    "polygon" shape rather than kept as separate shapes/popups.
    """
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        rings = _validate_polygon_rings(coords)
        if not rings:
            return None
        return {"kind": "polygon", "rings": rings, "name": name, "props": props}
    if gtype == "MultiPolygon":
        if not isinstance(coords, list) or not coords:
            return None
        rings: list[list[list[float]]] = []
        for poly_coords in coords:
            r = _validate_polygon_rings(poly_coords)
            if r is None:
                return None
            rings.extend(r)
        if not rings:
            return None
        return {"kind": "polygon", "rings": rings, "name": name, "props": props}
    if gtype == "LineString":
        line = _validate_line(coords)
        if line is None:
            return None
        return {"kind": "line", "lines": [line], "name": name, "props": props}
    if gtype == "MultiLineString":
        if not isinstance(coords, list) or not coords:
            return None
        lines = []
        for line_coords in coords:
            line = _validate_line(line_coords)
            if line is None:
                return None
            lines.append(line)
        if not lines:
            return None
        return {"kind": "line", "lines": lines, "name": name, "props": props}
    return None


def _shape_from_geojson_feature(feature: dict) -> dict | None:
    """A GeoJSON Feature with Polygon/MultiPolygon/LineString/MultiLineString geometry."""
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return None
    props = dict(feature.get("properties") or {})
    return _shape_from_geometry(geometry, props.get("name"), props)


def _shape_from_isochrone_result(data: dict) -> dict | None:
    """routing.isochrone()'s {"polygon": <geojson>, "stats": {...}, ...} -> one shape.

    Stats and the request parameters are flattened into the shape's props so
    they show up directly in the click popup rather than as a nested object.
    """
    geometry = data.get("polygon")
    if not isinstance(geometry, dict):
        return None
    shape = _shape_from_geometry(geometry, "Isochrone", {})
    if shape is None:
        return None
    props: dict = {}
    center = data.get("center")
    if isinstance(center, dict):
        clat = _as_float(center.get("lat"))
        clon = _as_float(center.get("lon"))
        if clat is not None and clon is not None:
            props["center"] = f"{clat:.5f}, {clon:.5f}"
    for key in ("minutes", "mode", "speed_m_s", "polygon_method", "truncated"):
        if key in data:
            props[key] = data[key]
    stats = data.get("stats")
    if isinstance(stats, dict):
        props.update(stats)
    # Style/annotation hints (#368) survive the props rebuild: an agent
    # decorating routing.isochrone() output — the exact payload role/label/
    # callout were designed for — can set them at the payload's top level or
    # on the polygon's "properties" (top level wins). Validation/truncation
    # happen downstream in render_html like for any other shape.
    polygon_props = geometry.get("properties")
    sources = [data] + ([polygon_props] if isinstance(polygon_props, dict) else [])
    for key in ("role", "label", "callout"):
        for source in sources:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                props[key] = value
                break
    shape["props"] = props
    return shape


def _is_isochrone_result(data: dict) -> bool:
    geometry = data.get("polygon")
    return isinstance(geometry, dict) and geometry.get("type") in ("Polygon", "MultiPolygon")


def _handle_feature_dict(item: dict, points: list[dict], shapes: list[dict]) -> bool:
    """Route a single GeoJSON Feature dict to points or shapes.

    Returns True if the feature contributed a point or shape, False if its
    geometry was missing/unsupported/malformed (caller counts these as
    skipped).
    """
    geometry = item.get("geometry")
    if not isinstance(geometry, dict):
        return False
    gtype = geometry.get("type")
    if gtype == "Point":
        point = _point_from_geojson_feature(item)
        if point is None:
            return False
        points.append(point)
        return True
    if gtype in _SHAPE_GEOMETRY_TYPES:
        shape = _shape_from_geojson_feature(item)
        if shape is None:
            return False
        shapes.append(shape)
        return True
    return False


def _is_compare_areas_result(data: dict) -> bool:
    """compare_areas returns {areas, categories, differentiators} — no top-level coords."""
    return isinstance(data.get("areas"), list) and isinstance(data.get("differentiators"), list)


def _points_from_compare_areas(data: dict, points: list[dict]) -> None:
    """One marker per compared area, popup carrying density and category mix."""
    for i, area in enumerate(data.get("areas") or []):
        if not isinstance(area, dict):
            continue
        center = area.get("center")
        if not isinstance(center, dict):
            continue
        lat = _as_float(center.get("lat"))
        lon = _as_float(center.get("lon"))
        if lat is None or lon is None:
            continue
        props = {k: v for k, v in area.items() if k != "center" and v is not None}
        points.append({"lat": lat, "lon": lon, "name": f"Area {i + 1}", "props": props})


def extract_features(data) -> dict:
    """Normalize a tool result or caller GeoJSON into points + shapes.

    A superset of extract_points(): also recognizes Polygon, MultiPolygon,
    LineString, and MultiLineString geometry (in Features, FeatureCollections,
    or bare lists) as well as routing.isochrone()'s
    {"polygon": <geojson>, "stats": {...}, ...} result shape.

    Returns {"points": [...], "shapes": [...], "skipped_features": N} where
    each shape is {"kind": "polygon"|"line", "rings"|"lines": [...],
    "name": ..., "props": {...}}. Anything with missing/malformed geometry
    or unusable coordinates is dropped and counted in skipped_features
    rather than raising — render_map degrades to fewer features rather than
    failing outright.
    """
    points: list[dict] = []
    shapes: list[dict] = []
    skipped = 0

    if data is None:
        return {"points": points, "shapes": shapes, "skipped_features": 0}

    if isinstance(data, dict):
        if _is_isochrone_result(data):
            shape = _shape_from_isochrone_result(data)
            if shape:
                shapes.append(shape)
            else:
                skipped += 1
            return {"points": points, "shapes": shapes, "skipped_features": skipped}
        if _is_compare_areas_result(data):
            _points_from_compare_areas(data, points)
            skipped += max(0, len(data.get("areas") or []) - len(points))
            return {"points": points, "shapes": shapes, "skipped_features": skipped}
        if isinstance(data.get("results"), list):
            for r in data["results"]:
                if not isinstance(r, dict):
                    skipped += 1
                    continue
                point = _point_from_row(r)
                if point:
                    points.append(point)
                else:
                    skipped += 1
            return {"points": points, "shapes": shapes, "skipped_features": skipped}
        if isinstance(data.get("features"), list):
            for f in data["features"]:
                if not isinstance(f, dict) or not _handle_feature_dict(f, points, shapes):
                    skipped += 1
            return {"points": points, "shapes": shapes, "skipped_features": skipped}
        if isinstance(data.get("center"), dict):
            lat = _as_float(data["center"].get("lat"))
            lon = _as_float(data["center"].get("lon"))
            if lat is None or lon is None:
                return {"points": points, "shapes": shapes, "skipped_features": skipped}
            props = {k: v for k, v in data.items() if k != "center"}
            points.append({"lat": lat, "lon": lon, "name": "Area summary", "props": props})
            return {"points": points, "shapes": shapes, "skipped_features": skipped}
        if data.get("geometry"):
            if not _handle_feature_dict(data, points, shapes):
                skipped += 1
            return {"points": points, "shapes": shapes, "skipped_features": skipped}
        point = _point_from_row(data)
        if point:
            points.append(point)
        return {"points": points, "shapes": shapes, "skipped_features": skipped}

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                skipped += 1
                continue
            if item.get("geometry"):
                if not _handle_feature_dict(item, points, shapes):
                    skipped += 1
            else:
                point = _point_from_row(item)
                if point:
                    points.append(point)
                else:
                    skipped += 1
        return {"points": points, "shapes": shapes, "skipped_features": skipped}

    return {"points": points, "shapes": shapes, "skipped_features": skipped}


# --- HTML/CSS/JS template pieces --------------------------------------------
# Assembled with plain str.replace (never str.format/f-string) against the
# whole document: CSS and JS are full of literal { } braces that .format
# would misparse as fields.

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --panel-bg: #ffffff;
  --text: #1c2126;
  --muted: #5b6470;
  --border: #d8dce1;
  --accent: #2563eb;
  --marker: #e0562b;
  --marker-stroke: #ffffff;
  --shape-fill: rgba(37, 99, 235, 0.16);
  --shape-stroke: #2563eb;
  --line-stroke: #2563eb;
  --shadow: 0 2px 10px rgba(20, 24, 30, 0.12);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c;
    --panel-bg: #1d2127;
    --text: #e7eaee;
    --muted: #9aa4b1;
    --border: #333a44;
    --accent: #6ea8fe;
    --marker: #ff8a5b;
    --marker-stroke: #1d2127;
    --shape-fill: rgba(110, 168, 254, 0.22);
    --shape-stroke: #6ea8fe;
    --line-stroke: #6ea8fe;
    --shadow: 0 2px 14px rgba(0, 0, 0, 0.45);
  }
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; min-height: 100%;
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
#app { display: flex; flex-direction: column; min-height: 100vh; }
header {
  display: flex; align-items: baseline; gap: 0.6em;
  padding: 0.7em 1em; border-bottom: 1px solid var(--border);
  background: var(--panel-bg);
}
header h1 { font-size: 1.05rem; margin: 0; font-weight: 600; }
header .count { color: var(--muted); font-size: 0.85rem; }
#verdict {
  padding: 0.85em 1.1em 1em;
  background: var(--panel-bg);
  border-bottom: 1px solid var(--border);
}
.section-label {
  font-size: 0.7rem; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--muted); margin: 0 0 0.45em;
}
.verdict-text { line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
#body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(220px, 28rem);
  flex: 1;
  min-height: 0;
}
#map-container { position: relative; min-height: 320px; height: min(64vh, 640px); }
#stops {
  padding: 0.85em 1.1em 1.2em;
  background: var(--panel-bg);
  border-left: 1px solid var(--border);
  overflow: auto;
}
.stop-list { list-style: none; margin: 0; padding: 0; }
.stop {
  padding: 0.65em 0.55em;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
}
.stop:last-child { border-bottom: none; }
.stop:hover, .stop:focus { background: var(--bg); outline: none; }
.stop-name { font-weight: 600; font-size: 0.92rem; }
.stop-meta { color: var(--muted); font-size: 0.8rem; margin-top: 0.2em; }
.stop-meta .k { margin-right: 0.15em; }
.stops-empty { color: var(--muted); font-size: 0.9rem; margin: 0; }
#credit {
  display: flex; flex-wrap: wrap; gap: 0.4em 1.2em;
  align-items: baseline; justify-content: space-between;
  padding: 0.6em 1.1em;
  border-top: 1px solid var(--border);
  background: var(--panel-bg);
  font-size: 11px; color: var(--muted);
}
@media (max-width: 800px) {
  #body { grid-template-columns: 1fr; }
  #map-container { height: min(50vh, 420px); }
  #stops { border-left: none; border-top: 1px solid var(--border); }
}
@media print {
  #map-container { height: 360px; break-inside: avoid; }
  .panel { display: none; }
  #stops { overflow: visible; }
}
#map {
  display: block; width: 100%; height: 100%;
  background: var(--bg); touch-action: none; cursor: grab;
}
#map:active { cursor: grabbing; }
.marker circle {
  fill: var(--marker); stroke: var(--marker-stroke); stroke-width: 1.5;
  cursor: pointer;
}
.marker text {
  font-size: 11px; fill: var(--text); pointer-events: none;
  paint-order: stroke; stroke: var(--panel-bg); stroke-width: 3px;
}
.shape-polygon {
  fill: var(--shape-fill); stroke: var(--shape-stroke); stroke-width: 2;
  cursor: pointer;
}
.shape-polygon:hover { fill-opacity: 0.85; }
.shape-line {
  fill: none; stroke: var(--line-stroke); stroke-width: 2.5;
  stroke-linejoin: round; stroke-linecap: round; cursor: pointer;
}
/* Shape style roles (#368): "shed" = travel-time shed, a soft translucent
   fill with a dashed edge so several can overlap and stay readable. */
.shape-polygon.role-shed {
  fill-opacity: 0.15;
  stroke-dasharray: 8 5;
}
/* Same specificity as the base rule above, so declared after it: sheds keep
   hover feedback (softer than the generic 0.85 so overlapping sheds stay
   readable while hovered). */
.shape-polygon.role-shed:hover { fill-opacity: 0.4; }
.shape-line.role-shed { stroke-dasharray: 8 5; }
/* "outline" = a compared-area boundary: no fill (so it never hides what's
   under it), a strong 2px edge. */
.shape-polygon.role-outline { fill: none; stroke-width: 2; }
.shape-line.role-outline { stroke-width: 3; }
.shape-label-chip {
  fill: var(--panel-bg); stroke: var(--border); stroke-width: 1; rx: 5;
  pointer-events: none;
}
.shape-label-name, .shape-label-callout {
  text-anchor: middle; pointer-events: none; fill: var(--text);
}
.shape-label-name { font-size: 11px; font-weight: 700; }
.shape-label-callout { font-size: 10.5px; fill: var(--muted); }
.panel {
  position: absolute; top: 12px; right: 12px;
  display: flex; flex-direction: column; gap: 4px;
  background: var(--panel-bg); border: 1px solid var(--border);
  border-radius: 8px; box-shadow: var(--shadow); overflow: hidden;
}
.panel button {
  width: 32px; height: 32px; border: none; background: transparent;
  color: var(--text); font-size: 16px; cursor: pointer; line-height: 1;
}
.panel button:hover { background: var(--bg); }
.panel button + button { border-top: 1px solid var(--border); }
.scalebar {
  position: absolute; left: 12px; bottom: 12px;
  background: var(--panel-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 10px; box-shadow: var(--shadow);
  font-size: 11px; color: var(--muted);
}
.scalebar svg { display: block; }
.scalebar rect { fill: var(--text); }
.attribution {
  position: absolute; right: 12px; bottom: 12px;
  background: var(--panel-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 10px; box-shadow: var(--shadow);
  font-size: 11px; color: var(--muted);
}
.legend {
  position: absolute; left: 12px; top: 12px;
  display: flex; flex-direction: column; gap: 4px;
  background: var(--panel-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 10px; box-shadow: var(--shadow);
  font-size: 11px; color: var(--text); max-width: 200px;
}
.legend-row { display: flex; align-items: center; gap: 6px; }
.legend-swatch {
  width: 10px; height: 10px; flex: none; border-radius: 50%;
  border: 1px solid var(--marker-stroke);
}
.legend-label { word-break: break-word; }
.popup {
  position: absolute; transform: translate(-50%, calc(-100% - 14px));
  background: var(--panel-bg); border: 1px solid var(--border);
  border-radius: 8px; box-shadow: var(--shadow);
  padding: 8px 10px; max-width: 260px; font-size: 12.5px;
  pointer-events: none;
}
.popup-title { font-weight: 600; margin-bottom: 4px; }
.popup-row { display: flex; gap: 6px; color: var(--muted); }
.popup-row .k { min-width: 90px; }
.popup-row .v { color: var(--text); word-break: break-word; }
#empty-msg {
  position: absolute; inset: 0; display: none;
  align-items: center; justify-content: center;
  color: var(--muted); font-size: 0.95rem;
}
"""

_JS = """
(function () {
  "use strict";
  var DATA = __DATA_JSON__;
  var POINTS = DATA.points || [];
  var SHAPES = DATA.shapes || [];
  var LEGEND = DATA.legend || {};
  var SVGNS = "http://www.w3.org/2000/svg";
  var svg = document.getElementById("map");
  var viewport = document.getElementById("viewport");
  var shapesGroup = document.getElementById("shapes");
  var markersGroup = document.getElementById("markers");
  var labelsGroup = document.getElementById("labels");
  var popup = document.getElementById("popup");
  var scaleFill = document.getElementById("scale-fill");
  var scaleLabel = document.getElementById("scale-label");
  var emptyMsg = document.getElementById("empty-msg");

  var W = 1000, H = 640, PAD = 56;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtVal(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "number") return String(Math.round(v * 100) / 100);
    if (typeof v === "boolean") return v ? "true" : "false";
    return String(v);
  }

  if (!POINTS.length && !SHAPES.length) {
    emptyMsg.style.display = "flex";
    return;
  }

  var lats = POINTS.map(function (p) { return p.lat; });
  var lons = POINTS.map(function (p) { return p.lon; });
  SHAPES.forEach(function (s) {
    var vertexLists = s.kind === "polygon" ? s.rings : s.lines;
    (vertexLists || []).forEach(function (vl) {
      vl.forEach(function (c) { lons.push(c[0]); lats.push(c[1]); });
    });
  });

  var minLat = Math.min.apply(null, lats), maxLat = Math.max.apply(null, lats);
  var minLon = Math.min.apply(null, lons), maxLon = Math.max.apply(null, lons);
  var latPad = Math.max((maxLat - minLat) * 0.12, 0.003);
  var lonPad = Math.max((maxLon - minLon) * 0.12, 0.003);
  minLat -= latPad; maxLat += latPad; minLon -= lonPad; maxLon += lonPad;

  var lat0 = (minLat + maxLat) / 2;
  var cosLat0 = Math.max(Math.cos((lat0 * Math.PI) / 180), 0.05);

  var xDegMin = minLon * cosLat0, xDegMax = maxLon * cosLat0;
  var yDegMin = minLat, yDegMax = maxLat;
  var spanX = Math.max(xDegMax - xDegMin, 1e-9);
  var spanY = Math.max(yDegMax - yDegMin, 1e-9);

  var innerW = W - PAD * 2, innerH = H - PAD * 2;
  var S = Math.min(innerW / spanX, innerH / spanY);
  var usedW = spanX * S, usedH = spanY * S;
  var offX = PAD + (innerW - usedW) / 2;
  var offY = PAD + (innerH - usedH) / 2;

  function project(lat, lon) {
    var xd = lon * cosLat0;
    var x = (xd - xDegMin) * S + offX;
    var y = (yDegMax - lat) * S + offY; // north up
    return [x, y];
  }

  var METERS_PER_DEG = 111320;

  function pathFromRing(ring) {
    var d = "";
    ring.forEach(function (c, idx) {
      var xy = project(c[1], c[0]);
      d += (idx === 0 ? "M" : "L") + xy[0].toFixed(2) + "," + xy[1].toFixed(2) + " ";
    });
    return d + "Z ";
  }

  function centroidOf(vertexList) {
    var cx = 0, cy = 0, n = vertexList.length;
    vertexList.forEach(function (c) {
      var xy = project(c[1], c[0]);
      cx += xy[0]; cy += xy[1];
    });
    return [cx / n, cy / n];
  }

  // bbox (in projected px) of every vertex a shape draws, used to anchor its
  // label/callout chip at the shape's center-top (#368).
  function bboxTopCenter(vertexLists) {
    var minX = Infinity, maxX = -Infinity, minY = Infinity;
    vertexLists.forEach(function (vl) {
      vl.forEach(function (c) {
        var xy = project(c[1], c[0]);
        if (xy[0] < minX) minX = xy[0];
        if (xy[0] > maxX) maxX = xy[0];
        if (xy[1] < minY) minY = xy[1];
      });
    });
    return [(minX + maxX) / 2, minY];
  }

  var LABEL_LINE_H = 14, LABEL_PAD = 6;
  // A callout can run to SHAPE_CALLOUT_MAX_CHARS (~80) but the chip caps at
  // 260px wide (~43 chars at the 5.6px/char estimate below), so wrap it
  // into chip-width lines — growing boxH — instead of overflowing the rect.
  var CALLOUT_WRAP_CHARS = 43;
  function wrapCallout(text) {
    var words = String(text).split(/\\s+/).filter(Boolean);
    var lines = [], cur = "";
    words.forEach(function (w) {
      while (w.length > CALLOUT_WRAP_CHARS) { // unbreakable run: hard-split
        if (cur) { lines.push(cur); cur = ""; }
        lines.push(w.slice(0, CALLOUT_WRAP_CHARS));
        w = w.slice(CALLOUT_WRAP_CHARS);
      }
      if (!cur) cur = w;
      else if (cur.length + 1 + w.length <= CALLOUT_WRAP_CHARS) cur += " " + w;
      else { lines.push(cur); cur = w; }
    });
    if (cur) lines.push(cur);
    return lines;
  }
  function addShapeLabel(s, vertexLists) {
    if (!s.label && !s.callout) return;
    var lines = [];
    if (s.label) lines.push({ text: s.label, cls: "shape-label-name" });
    if (s.callout) {
      wrapCallout(s.callout).forEach(function (line) {
        lines.push({ text: line, cls: "shape-label-callout" });
      });
    }
    if (!lines.length) return;
    var top = bboxTopCenter(vertexLists);
    var cx = top[0], topY = top[1];
    // Width is estimated from character count (no live text measurement
    // before layout) rather than measured precisely — good enough for a
    // legibility chip, not meant to hug the glyphs exactly.
    var maxLen = Math.max.apply(null, lines.map(function (l) { return l.text.length; }));
    var boxW = Math.min(Math.max(maxLen * 5.6 + 16, 40), 260);
    var boxH = lines.length * LABEL_LINE_H + LABEL_PAD * 2 - 2;
    var boxY = topY - boxH - 8;
    var g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "shape-label");
    var rect = document.createElementNS(SVGNS, "rect");
    rect.setAttribute("x", cx - boxW / 2);
    rect.setAttribute("y", boxY);
    rect.setAttribute("width", boxW);
    rect.setAttribute("height", boxH);
    rect.setAttribute("rx", 5);
    rect.setAttribute("class", "shape-label-chip");
    g.appendChild(rect);
    lines.forEach(function (l, i) {
      var t = document.createElementNS(SVGNS, "text");
      t.setAttribute("x", cx);
      t.setAttribute("y", boxY + LABEL_PAD + LABEL_LINE_H * (i + 1) - 4);
      t.setAttribute("class", l.cls);
      t.textContent = l.text;
      g.appendChild(t);
    });
    labelsGroup.appendChild(g);
  }

  function renderShape(s) {
    if (s.kind === "polygon" && s.rings && s.rings.length) {
      var d = "";
      s.rings.forEach(function (ring) { d += pathFromRing(ring); });
      var path = document.createElementNS(SVGNS, "path");
      path.setAttribute("d", d.trim());
      path.setAttribute("fill-rule", "evenodd");
      path.setAttribute("class", "shape-polygon" + (s.role ? " role-" + s.role : ""));
      var c0 = centroidOf(s.rings[0]);
      var entity = { name: s.name, props: s.props || {}, _x: c0[0], _y: c0[1] };
      path.addEventListener("click", function (ev) {
        ev.stopPropagation();
        showPopup(entity);
      });
      shapesGroup.appendChild(path);
      addShapeLabel(s, s.rings);
    } else if (s.kind === "line" && s.lines && s.lines.length) {
      s.lines.forEach(function (line) {
        var parts = line.map(function (c, idx) {
          var xy = project(c[1], c[0]);
          return (idx === 0 ? "M" : "L") + xy[0].toFixed(2) + "," + xy[1].toFixed(2);
        });
        var path = document.createElementNS(SVGNS, "path");
        path.setAttribute("d", parts.join(" "));
        path.setAttribute("class", "shape-line" + (s.role ? " role-" + s.role : ""));
        var c0 = centroidOf(line);
        var entity = { name: s.name, props: s.props || {}, _x: c0[0], _y: c0[1] };
        path.addEventListener("click", function (ev) {
          ev.stopPropagation();
          showPopup(entity);
        });
        shapesGroup.appendChild(path);
      });
      addShapeLabel(s, s.lines);
    }
  }

  // Stacking (#368): sheds (soft translucent fill) render first, then
  // shapes with no/unknown role (today's default styling), then outlines
  // (no fill, so order relative to sheds barely matters visually) — all of
  // it under the labels group (chips only need to sit above shapes) which
  // in turn sits under the markers group; markers stay last in the DOM so
  // neither a shape fill nor an opaque label chip ever covers a marker
  // dot. A shed/outline-free SHAPES array (the common case)
  // collapses to a single "other" bucket in original order, so undecorated
  // input renders in exactly its original order.
  var shedShapes = [], outlineShapes = [], otherShapes = [];
  SHAPES.forEach(function (s) {
    if (s.role === "shed") shedShapes.push(s);
    else if (s.role === "outline") outlineShapes.push(s);
    else otherShapes.push(s);
  });
  shedShapes.concat(otherShapes, outlineShapes).forEach(renderShape);

  POINTS.forEach(function (p, i) {
    var xy = project(p.lat, p.lon);
    p._x = xy[0]; p._y = xy[1];
    var g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "marker");
    var c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", xy[0]); c.setAttribute("cy", xy[1]); c.setAttribute("r", 6);
    if (p.cls && LEGEND[p.cls] && LEGEND[p.cls].color) {
      // Inline style, not the "fill" presentation attribute: the stylesheet's
      // `.marker circle { fill: var(--marker); }` rule outranks presentation
      // attributes, so an attribute-set class color would never render.
      c.style.fill = LEGEND[p.cls].color;
    }
    g.appendChild(c);
    if (p.name) {
      var t = document.createElementNS(SVGNS, "text");
      t.setAttribute("x", xy[0] + 9); t.setAttribute("y", xy[1] + 4);
      t.textContent = p.name;
      g.appendChild(t);
    }
    g.addEventListener("click", function (ev) {
      ev.stopPropagation();
      showPopup(p);
    });
    markersGroup.appendChild(g);
  });

  function showPopup(p) {
    var rows = "";
    if (p.name) rows += '<div class="popup-title">' + escapeHtml(p.name) + "</div>";
    var props = p.props || {};
    Object.keys(props).forEach(function (k) {
      rows +=
        '<div class="popup-row"><span class="k">' + escapeHtml(k) + '</span>' +
        '<span class="v">' + escapeHtml(fmtVal(props[k])) + "</span></div>";
    });
    if (!rows) rows = '<div class="popup-title">' + escapeHtml("(no details)") + "</div>";
    popup.innerHTML = rows;
    popup.style.display = "block";
    positionPopup(p);
  }

  function positionPopup(p) {
    var rect = svg.getBoundingClientRect();
    var scrX = ((p._x * view.k) + view.x) / W * rect.width;
    var scrY = ((p._y * view.k) + view.y) / H * rect.height;
    popup.style.left = scrX + "px";
    popup.style.top = scrY + "px";
  }

  var view = { x: 0, y: 0, k: 1 };
  var activePopupPoint = null;

  function applyTransform() {
    viewport.setAttribute(
      "transform",
      "translate(" + view.x + "," + view.y + ") scale(" + view.k + ")"
    );
    updateScaleBar();
    if (activePopupPoint) positionPopup(activePopupPoint);
  }

  var NICE = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000,
              10000, 20000, 50000, 100000, 200000, 500000, 1000000, 2000000];
  function pickNice(target) {
    for (var i = 0; i < NICE.length; i++) { if (NICE[i] >= target) return NICE[i]; }
    return NICE[NICE.length - 1];
  }

  function updateScaleBar() {
    var metersPerUnit = METERS_PER_DEG / (S * view.k);
    var niceMeters = pickNice(metersPerUnit * 120);
    var barPx = niceMeters / metersPerUnit;
    scaleFill.setAttribute("width", Math.max(barPx, 1));
    scaleLabel.textContent = niceMeters >= 1000 ? niceMeters / 1000 + " km" : niceMeters + " m";
  }

  function zoomAt(factor, cx, cy) {
    var nk = Math.min(Math.max(view.k * factor, 0.4), 60);
    factor = nk / view.k;
    view.x = cx - (cx - view.x) * factor;
    view.y = cy - (cy - view.y) * factor;
    view.k = nk;
    applyTransform();
  }

  svg.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var rect = svg.getBoundingClientRect();
    var cx = ((ev.clientX - rect.left) / rect.width) * W;
    var cy = ((ev.clientY - rect.top) / rect.height) * H;
    zoomAt(ev.deltaY < 0 ? 1.2 : 1 / 1.2, cx, cy);
  }, { passive: false });

  // Pointer capture is only engaged once the pointer has actually moved past
  // a small threshold. Capturing unconditionally on every pointerdown (the
  // pre-#34 behavior) makes Chromium retarget the *click* event that follows
  // a same-spot mousedown/mouseup to the <svg> element itself instead of the
  // marker/shape actually under the cursor — silently breaking click-to-open
  // popups for real mouse users (caught by scripts/browser_smoke.py, #33).
  var DRAG_THRESHOLD = 3;
  var dragging = null;
  svg.addEventListener("pointerdown", function (ev) {
    dragging = { x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y,
                 pointerId: ev.pointerId, captured: false };
  });
  svg.addEventListener("pointermove", function (ev) {
    if (!dragging) return;
    if (!dragging.captured) {
      var dx0 = ev.clientX - dragging.x, dy0 = ev.clientY - dragging.y;
      if (Math.abs(dx0) < DRAG_THRESHOLD && Math.abs(dy0) < DRAG_THRESHOLD) return;
      svg.setPointerCapture(dragging.pointerId);
      dragging.captured = true;
    }
    var rect = svg.getBoundingClientRect();
    var sx = W / rect.width, sy = H / rect.height;
    view.x = dragging.vx + (ev.clientX - dragging.x) * sx;
    view.y = dragging.vy + (ev.clientY - dragging.y) * sy;
    applyTransform();
  });
  svg.addEventListener("pointerup", function () { dragging = null; });
  svg.addEventListener("pointerleave", function () { dragging = null; });
  svg.addEventListener("click", function () {
    activePopupPoint = null;
    popup.style.display = "none";
  });

  document.getElementById("zoom-in").addEventListener("click", function (ev) {
    ev.stopPropagation(); zoomAt(1.3, W / 2, H / 2);
  });
  document.getElementById("zoom-out").addEventListener("click", function (ev) {
    ev.stopPropagation(); zoomAt(1 / 1.3, W / 2, H / 2);
  });
  document.getElementById("zoom-reset").addEventListener("click", function (ev) {
    ev.stopPropagation(); view = { x: 0, y: 0, k: 1 }; applyTransform();
  });

  var origShowPopup = showPopup;
  showPopup = function (p) { activePopupPoint = p; origShowPopup(p); };

  function bindStop(el) {
    var idx = parseInt(el.getAttribute("data-idx"), 10);
    function open() {
      if (POINTS[idx]) showPopup(POINTS[idx]);
    }
    el.addEventListener("click", function (ev) { ev.stopPropagation(); open(); });
    el.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
    });
  }
  var stopNodes = document.querySelectorAll(".stop[data-idx]");
  for (var si = 0; si < stopNodes.length; si++) bindStop(stopNodes[si]);

  applyTransform();
})();
"""

_DOC = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<style>@@CSS@@</style>
</head>
<body>
<div id="app">
  <header>
    <h1>@@TITLE@@</h1>
    <span class="count">@@COUNT@@</span>
  </header>
  <section id="verdict">
    <div class="section-label">Verdict</div>
    <div class="verdict-text">@@SUMMARY@@</div>
  </section>
  <div id="body">
  <div id="map-container">
    <svg id="map" viewBox="0 0 1000 640" preserveAspectRatio="xMidYMid meet">
      <g id="viewport">
        <g id="shapes"></g>
        <g id="labels"></g>
        <g id="markers"></g>
      </g>
    </svg>
    <div id="empty-msg">No places to show.</div>
    <div class="panel">
      <button id="zoom-in" title="Zoom in" aria-label="Zoom in">+</button>
      <button id="zoom-out" title="Zoom out" aria-label="Zoom out">&minus;</button>
      <button id="zoom-reset" title="Reset view" aria-label="Reset view">&#8634;</button>
    </div>
    <div class="scalebar">
      <svg width="130" height="14">
        <rect id="scale-fill" x="0" y="5" width="80" height="4"></rect>
      </svg>
      <span id="scale-label">&nbsp;</span>
    </div>
    <div class="attribution">@@ATTRIBUTION@@</div>@@LEGEND@@
    <div class="popup" id="popup" style="display:none"></div>
  </div>
  <aside id="stops">
    <div class="section-label">@@STOPS_LABEL@@</div>
    @@STOPS@@
  </aside>
  </div>
  <footer id="credit">
    <span>@@ATTRIBUTION@@</span>
    <span class="credit-note">PlaceRoot one-pager · local file · nothing uploaded</span>
  </footer>
</div>
<script>@@JS@@</script>
</body>
</html>
"""


def _unwrap_longitudes(points: list[dict], shapes: list[dict]) -> None:
    """Shift longitudes onto a continuous range when a feature set straddles
    the antimeridian, so the JS renderer's local-tangent-plane projection
    (plain Math.min/Math.max over raw longitudes, see _JS) doesn't mistake a
    small dateline-crossing gap for a ~360-degree span (#137).

    Only RELATIVE longitude positions matter for that projection — it is not
    a global map, just a local view — so a single global shift (every
    longitude < 0 gets +360) preserves every point's position relative to
    every other point while turning e.g. [179.9, -179.9] into
    [179.9, 180.1] (a ~0.2 degree span instead of ~359.8).

    Heuristic: a >180 degree raw span is treated as antimeridian-crossing
    (true for the local-area views render_map is for). A genuinely
    globe-spanning feature set would also trigger this and get shifted
    pointlessly, but such input isn't a realistic render_map use case, and
    the shift is harmless (still just a relabeling of longitude) even then.
    Mutates points/shapes in place; a <=180 degree span (the common case) is
    left untouched entirely, so ordinary non-dateline data is unaffected.
    """
    lons: list[float] = [p["lon"] for p in points]
    for s in shapes:
        coord_lists = s.get("rings") if s.get("kind") == "polygon" else s.get("lines")
        for coords in coord_lists or []:
            lons.extend(c[0] for c in coords)

    if not lons:
        return
    if max(lons) - min(lons) <= 180:
        return

    for p in points:
        if p["lon"] < 0:
            p["lon"] += 360
    for s in shapes:
        coord_lists = s.get("rings") if s.get("kind") == "polygon" else s.get("lines")
        for coords in coord_lists or []:
            for c in coords:
                if c[0] < 0:
                    c[0] += 360



def _fmt_distance(meters) -> str:
    try:
        m = float(meters)
    except (TypeError, ValueError):
        return str(meters)
    if m >= 1000:
        return f"{m / 1000:.1f} km"
    return f"{int(round(m))} m"


def _fmt_duration(seconds) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if s >= 3600:
        hours = int(s // 3600)
        mins = int((s % 3600) // 60)
        return f"{hours} h {mins} min" if mins else f"{hours} h"
    if s >= 60:
        return f"{int(round(s / 60))} min"
    return f"{int(round(s))} s"


def _clip_summary(text: str | None) -> str:
    clipped = (text or "").strip()
    if len(clipped) <= SUMMARY_MAX_CHARS:
        return clipped
    return clipped[:SUMMARY_MAX_CHARS].rstrip() + "…"


def _truncate(text: str, max_chars: int) -> str:
    clipped = text.strip()
    if len(clipped) <= max_chars:
        return clipped
    return clipped[:max_chars].rstrip() + "…"


def _shape_role(props: dict) -> str | None:
    """props["role"] if it's a recognized style role, else None (default style).

    Anything else — absent, or a value we don't recognize — falls through to
    today's default shape styling silently rather than erroring; render_map
    degrades gracefully the same way malformed geometry does.
    """
    role = props.get("role")
    return role if role in _SHAPE_ROLES else None


# Props worth putting on a stop row. Nested objects (addresses lists,
# category_counts dicts) stay in the map popup, not the scannable list.
_STOP_DETAIL_KEYS = (
    "category",
    "basic_category",
    "distance_m",
    "operating_status",
    "brand",
    "confidence",
    "address",
    "minutes",
    "mode",
    "total_places",
    "density_per_km2",
    "duration_s",
    "detour_m",
    "area_km2",
    "max_radius_m",
)


def _fmt_stop_value(key: str, value) -> str:
    if key == "distance_m":
        return _fmt_distance(value)
    if key == "duration_s":
        return _fmt_duration(value)
    if key == "density_per_km2":
        try:
            return f"{float(value):g}/km²"
        except (TypeError, ValueError):
            return str(value)
    if key == "confidence":
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _stop_meta(props: dict) -> str:
    bits = []
    for key in _STOP_DETAIL_KEYS:
        if key not in props or props[key] is None:
            continue
        value = props[key]
        if isinstance(value, (list, dict)):
            continue
        bits.append(_fmt_stop_value(key, value))
    return " · ".join(bits)


def _stops_html(points: list[dict], shapes: list[dict]) -> tuple[str, str]:
    """Label + inner HTML for the one-pager stop list (server-rendered, no JS)."""
    items: list[str] = []
    for i, point in enumerate(points):
        name = html.escape(str(point.get("name") or f"Stop {i + 1}"))
        meta = html.escape(_stop_meta(point.get("props") or {}))
        meta_html = f'<div class="stop-meta">{meta}</div>' if meta else ""
        items.append(
            f'<li class="stop" data-idx="{i}" tabindex="0">'
            f'<div class="stop-name">{name}</div>{meta_html}</li>'
        )
    for shape in shapes:
        name = html.escape(str(shape.get("name") or shape.get("kind") or "Shape"))
        meta = html.escape(_stop_meta(shape.get("props") or {}))
        meta_html = f'<div class="stop-meta">{meta}</div>' if meta else ""
        items.append(
            f'<li class="stop">'
            f'<div class="stop-name">{name}</div>{meta_html}</li>'
        )
    n = len(items)
    if n == 0:
        return "Stops", '<p class="stops-empty">No stop details.</p>'
    noun = "stop" if n == 1 else "stops"
    return f"{n} {noun}", f'<ol class="stop-list">{"".join(items)}</ol>'


def compose_summary(
    data, points: list[dict] | None = None, shapes: list[dict] | None = None
) -> str:
    """A short fallback verdict when the caller did not write one.

    Reads the well-known tool result shapes (find_places, summarize_area,
    compare_areas, isochrone, optimize_route) and otherwise falls back to a
    count of extracted points/shapes. Never raises — a one-pager without a
    verdict is worse than a generic sentence.
    """
    points = points or []
    shapes = shapes or []

    if isinstance(data, dict):
        if _is_compare_areas_result(data):
            parts = []
            for i, area in enumerate(data.get("areas") or []):
                if not isinstance(area, dict):
                    continue
                total = area.get("total_places")
                dens = area.get("density_per_km2")
                bit = f"Area {i + 1}"
                if total is not None:
                    bit += f": {total} places"
                if dens is not None:
                    bit += f" ({_fmt_stop_value('density_per_km2', dens)})"
                parts.append(bit)
            diffs = data.get("differentiators") or []
            if diffs and isinstance(diffs[0], dict) and diffs[0].get("category"):
                parts.append(f"Biggest difference: {diffs[0]['category']}.")
            if parts:
                return " ".join(parts)

        if "total_places" in data and isinstance(data.get("center"), dict):
            n = data["total_places"]
            try:
                n_int = int(n)
                line = f"{n_int} place{'s' if n_int != 1 else ''}"
            except (TypeError, ValueError):
                line = f"{n} places"
            radius = data.get("radius_m")
            if radius is not None:
                line += f" within {_fmt_distance(radius)}"
            line += "."
            tops = data.get("top_categories") or []
            cats = []
            for row in tops[:5]:
                if isinstance(row, dict) and row.get("category") is not None:
                    cats.append(f"{row['category']} ({row.get('count', '?')})")
            if cats:
                line += f" Top: {', '.join(cats)}."
            return line

        if _is_isochrone_result(data):
            minutes = data.get("minutes")
            mode = data.get("mode") or "travel"
            line = f"{minutes}-minute {mode} reachability."
            stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
            area = stats.get("area_km2")
            if area is not None:
                line += f" About {area} km²."
            return line

        if isinstance(data.get("order"), list) and "total_distance_m" in data:
            n = len(data["order"])
            line = f"{n}-stop run"
            dist = data.get("total_distance_m")
            if dist is not None:
                line += f", {_fmt_distance(dist)} total"
            dur = data.get("total_duration_s")
            if dur is not None:
                line += f" (~{_fmt_duration(dur)})"
            return line + "."

        if isinstance(data.get("results"), list):
            rows = [r for r in data["results"] if isinstance(r, dict)]
            n = len(rows)
            if n == 0:
                return "No places in this result."
            first = rows[0].get("name") or "Unnamed"
            line = f"{n} place{'s' if n != 1 else ''}."
            dist = rows[0].get("distance_m")
            if dist is not None:
                line += f" Nearest: {first} ({_fmt_distance(dist)})."
            else:
                line += f" First: {first}."
            return line

    n_pts, n_shp = len(points), len(shapes)
    parts = []
    if n_pts:
        parts.append(f"{n_pts} place{'s' if n_pts != 1 else ''}")
    if n_shp:
        parts.append(f"{n_shp} shape{'s' if n_shp != 1 else ''}")
    return (" · ".join(parts) + ".") if parts else "No places to show."


def _unique_slot(*blobs: str) -> str:
    """A substitution token that does not appear in any of the given strings."""
    for _ in range(16):
        token = f"@@PR_{secrets.token_hex(16)}@@"
        if all(token not in blob for blob in blobs):
            return token
    raise RuntimeError("could not allocate a collision-proof template token")


# --- pin classes + legend (#367) --------------------------------------------
# A feature (caller GeoJSON properties, or a find_places/summarize_area row)
# may carry props["class"] — a caller-chosen name, not an Overture field.
# `legend` maps that name to a label + optional color so markers of the same
# class get a contrasting fill and a legend box explains what's what.

# Okabe-Ito, minus black (a black dot would read as "no class" against the
# default marker color): deterministic, color-blind-safe fills assigned to
# any legend entry that didn't specify its own color.
_LEGEND_PALETTE = (
    "#e69f00",
    "#56b4e9",
    "#009e73",
    "#f0e442",
    "#0072b2",
    "#d55e00",
    "#cc79a7",
    "#999999",
)

# A legend color lands directly inside an SVG fill attribute and an HTML
# style attribute, so it is restricted to plain #rgb/#rrggbb hex — never
# trusted verbatim, which would otherwise be a CSS/attribute injection point.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _valid_hex_color(value) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR_RE.fullmatch(value))


def _expand_hex(color: str) -> str:
    """Canonical form for comparing hex colors: lowercase #rrggbb."""
    color = color.lower()
    if len(color) == 4:  # "#abc" -> "#aabbcc"
        color = "#" + "".join(ch * 2 for ch in color[1:])
    return color


def _feature_class(item: dict) -> str | None:
    """A point/shape's props["class"], or None if absent/not a string."""
    props = item.get("props") or {}
    cls = props.get("class")
    return cls if isinstance(cls, str) and cls else None


def _normalize_legend(legend: dict | None) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Validate/complete a caller-supplied legend into {class: {label, color}}.

    - A missing/blank label falls back to the class name itself.
    - A missing color is assigned from _LEGEND_PALETTE, walked in
      sorted-class-name order, skipping palette entries already claimed by
      an explicit color (compared case-insensitively with #rgb expanded to
      #rrggbb) so an auto-colored class never collides with an explicit
      one. Deterministic for a given legend regardless of dict insertion
      order.
    - An invalid color (anything but #rgb/#rrggbb hex) is dropped rather
      than trusted into the SVG/HTML output; a palette color is assigned
      instead and the class name comes back in `notes` (never the raw
      invalid string, so a hostile color value never reaches the page).

    legend=None or {} (or any non-dict) resolves to no legend at all.
    """
    if not isinstance(legend, dict) or not legend:
        return {}, []
    class_names = sorted(k for k in legend if isinstance(k, str))
    resolved: dict[str, dict[str, str]] = {}
    invalid_color_classes: list[str] = []
    needs_palette: list[str] = []
    claimed: set[str] = set()
    for cls in class_names:
        entry = legend.get(cls)
        label = cls
        color = None
        if isinstance(entry, dict):
            raw_label = entry.get("label")
            if isinstance(raw_label, str) and raw_label.strip():
                label = raw_label
            raw_color = entry.get("color")
            if raw_color is not None:
                if _valid_hex_color(raw_color):
                    color = raw_color
                    claimed.add(_expand_hex(color))
                else:
                    invalid_color_classes.append(cls)
        resolved[cls] = {"label": label, "color": color}
        if color is None:
            needs_palette.append(cls)

    # Auto-color pass: palette entries already claimed by an explicit color
    # are skipped so two classes never share a fill (until the free palette
    # itself runs out and has to wrap).
    available = [c for c in _LEGEND_PALETTE if _expand_hex(c) not in claimed]
    if not available:
        available = list(_LEGEND_PALETTE)
    for palette_i, cls in enumerate(needs_palette):
        resolved[cls]["color"] = available[palette_i % len(available)]

    notes = []
    if invalid_color_classes:
        noun = "class" if len(invalid_color_classes) == 1 else "classes"
        notes.append(
            f"legend color for {noun} {', '.join(invalid_color_classes)} was not a valid "
            "#rgb/#rrggbb hex string and was replaced with a palette color."
        )
    return resolved, notes


def _classify_features(
    points: list[dict], resolved_legend: dict[str, dict[str, str]]
) -> tuple[dict[str, dict[str, str]], int]:
    """Match each point's props["class"] against a resolved legend.

    Only points are classified — the legend controls marker dot fill
    (#367); shapes keep their existing fill/stroke regardless of any
    "class" in their props.

    Returns (legend_present, unknown_count): legend_present holds only the
    classes actually carried by a point, in sorted-class-name order (so
    the on-page legend box lists classes deterministically); unknown_count
    is how many points carried a class that isn't in the legend — they
    render with the default marker dot instead.
    """
    present: set[str] = set()
    unknown = 0
    for p in points:
        cls = _feature_class(p)
        if cls is None:
            continue
        if cls in resolved_legend:
            present.add(cls)
        else:
            unknown += 1
    return {cls: resolved_legend[cls] for cls in sorted(present)}, unknown


def _legend_note(unknown_count: int, color_notes: list[str]) -> str | None:
    parts = list(color_notes)
    if unknown_count:
        noun = "feature" if unknown_count == 1 else "features"
        parts.append(
            f"{unknown_count} {noun} carried a class not present in the legend and "
            "rendered with the default marker."
        )
    return " ".join(parts) if parts else None


def _legend_html(legend_present: dict[str, dict[str, str]]) -> str:
    """The on-page legend box: one swatch + label per class actually present.

    Empty input renders to "" (no box at all) rather than an empty
    container, so classless documents are unaffected.
    """
    if not legend_present:
        return ""
    rows = []
    for entry in legend_present.values():
        label = html.escape(str(entry["label"]))
        color = entry["color"]  # _normalize_legend already restricted this to hex
        rows.append(
            '<div class="legend-row"><span class="legend-swatch" '
            f'style="background:{color}"></span>'
            f'<span class="legend-label">{label}</span></div>'
        )
    return '<div class="legend">' + "".join(rows) + "</div>"


def render_html(
    points: list[dict],
    title: str | None = None,
    shapes: list[dict] | None = None,
    summary: str | None = None,
    legend: dict | None = None,
) -> str:
    """Render points + shapes (as produced by extract_points/extract_features) into one HTML doc.

    `shapes` is optional and defaults to none, so callers that only ever
    dealt with points (extract_points()) keep working unchanged. `summary`
    is the composed verdict on the one-pager; when omitted a short fallback
    is composed from the extracted features (#310).

    `legend` optionally maps a point's props["class"] to
    {"label": str, "color": str?} (#367): classed points get a
    contrasting marker fill and a legend box is drawn listing label +
    swatch for each class actually present. A missing color is assigned
    from a fixed color-blind-safe palette; an invalid one (not #rgb/
    #rrggbb hex) is dropped rather than trusted into the SVG. A point
    whose class isn't a legend key keeps the default marker dot. legend
    input without any classed points, or omitted entirely, renders exactly
    as it always has — see mapview._normalize_legend/_classify_features.
    """
    shapes = shapes or []
    if summary is None:
        summary = compose_summary(None, points, shapes)
    safe_title = html.escape(title or "PlaceRoot map", quote=True)
    count = len(points)
    shape_count = len(shapes)
    label_parts = []
    if count or not shape_count:
        label_parts.append(f"{count} place{'s' if count != 1 else ''}")
    if shape_count:
        label_parts.append(f"{shape_count} shape{'s' if shape_count != 1 else ''}")
    count_label = " · ".join(label_parts)

    resolved_legend, _color_notes = _normalize_legend(legend)
    legend_present, _unknown_class_count = _classify_features(points, resolved_legend)

    points_for_js = []
    for p in points:
        entry = {
            "lat": p["lat"],
            "lon": p["lon"],
            "name": p.get("name"),
            "props": p.get("props") or {},
        }
        cls = _feature_class(p)
        if cls in legend_present:
            entry["cls"] = cls
        points_for_js.append(entry)
    shapes_for_js = []
    for s in shapes:
        entry = {"kind": s.get("kind"), "name": s.get("name")}
        if s.get("kind") == "polygon":
            entry["rings"] = s.get("rings") or []
        else:
            entry["lines"] = s.get("lines") or []
        # Role/label/callout are optional style/annotation hints (#368) — only
        # set the key when there's something to say, so undecorated input
        # (the overwhelming common case) serializes to exactly the same JSON
        # as before this feature existed. label/callout are truncated in
        # props too (not just the render entry) so the click popup — which
        # lists every prop verbatim — can't be used to smuggle the untruncated
        # text back onto the page.
        props = dict(s.get("props") or {})
        role = _shape_role(props)
        if role:
            entry["role"] = role
        label = props.get("label")
        if isinstance(label, str) and label.strip():
            label = _truncate(label, SHAPE_LABEL_MAX_CHARS)
            entry["label"] = label
            props["label"] = label
        callout = props.get("callout")
        if isinstance(callout, str) and callout.strip():
            callout = _truncate(callout, SHAPE_CALLOUT_MAX_CHARS)
            entry["callout"] = callout
            props["callout"] = callout
        entry["props"] = props
        shapes_for_js.append(entry)

    # Antimeridian handling (#137): unwrap longitudes onto a continuous
    # range in place before they're serialized, so the JS projection's plain
    # min/max over raw longitudes below doesn't blow up on dateline-crossing
    # data. No-op for the common (non-dateline) case.
    _unwrap_longitudes(points_for_js, shapes_for_js)

    # default=str: props may carry values (e.g. Decimal-ish) json can't natively
    # serialize; "</script>" guard prevents the embedded JSON from closing the tag early.
    data_payload = {"points": points_for_js, "shapes": shapes_for_js}
    if legend_present:
        # Only added when a class actually resolved, so classless/legend-less
        # input keeps producing exactly the DATA payload it always has.
        data_payload["legend"] = legend_present
    data_json = json.dumps(data_payload, default=str).replace("</", "<\\/")

    stops_label, stops_html = _stops_html(points, shapes)
    # Swap every @@TOKEN@@ for a unique slot first so a place named
    # "@@STOPS@@" (or a title/summary containing one) cannot rewrite
    # another substitution. Embed the script — and its user JSON — last.
    replacements = {
        "CSS": _CSS,
        "TITLE": safe_title,
        "COUNT": html.escape(count_label),
        "SUMMARY": html.escape(_clip_summary(summary)),
        "STOPS_LABEL": html.escape(stops_label),
        "STOPS": stops_html,
        "ATTRIBUTION": html.escape(ATTRIBUTION),
        "LEGEND": _legend_html(legend_present),
    }
    known = [data_json, _JS, _DOC, *replacements.values()]
    slots: dict[str, str] = {}
    doc = _DOC
    for name in (*replacements, "JS"):
        token = _unique_slot(doc, *known, *slots.values())
        doc = doc.replace(f"@@{name}@@", token)
        slots[name] = token
        known.append(token)
    for name, value in replacements.items():
        doc = doc.replace(slots[name], value)
    js = _JS.replace("__DATA_JSON__", data_json)
    return doc.replace(slots["JS"], js)


def _shape_vertex_count(shape: dict) -> int:
    """Coordinate count of a shape as produced by extract_features: the sum
    of ring lengths for a polygon, or line lengths for a line."""
    if shape.get("kind") == "polygon":
        return sum(len(ring) for ring in shape.get("rings") or [])
    return sum(len(line) for line in shape.get("lines") or [])


def _cap_vertices(
    points: list[dict], shapes: list[dict], max_vertices: int
) -> tuple[list[dict], list[dict], int]:
    """Truncate points+shapes to a total vertex budget (#74, defense in depth).

    Points count as 1 vertex each; a shape counts its ring/line coordinate
    total (_shape_vertex_count). Points are kept before shapes, both in
    their original order, up to the first item that would push the running
    total over max_vertices — everything from that item onward (including
    the item itself) is dropped, so the result is always a clean prefix of
    the input rather than a scattered subset. Returns
    (kept_points, kept_shapes, dropped_count).
    """
    total = 0
    kept_points: list[dict] = []
    for i, p in enumerate(points):
        if total + 1 > max_vertices:
            return kept_points, [], (len(points) - i) + len(shapes)
        kept_points.append(p)
        total += 1

    kept_shapes: list[dict] = []
    for i, s in enumerate(shapes):
        vertex_count = _shape_vertex_count(s)
        if total + vertex_count > max_vertices:
            return kept_points, kept_shapes, len(shapes) - i
        kept_shapes.append(s)
        total += vertex_count

    return kept_points, kept_shapes, 0


def _slug(title: str | None) -> str:
    base = (title or "map").strip().lower()
    out = "".join(c if c.isalnum() else "-" for c in base)
    out = "-".join(filter(None, out.split("-")))
    return out[:40] or "map"


def write_artifact(
    data,
    title: str | None = None,
    inline: bool = False,
    out_dir: Path | None = None,
    summary: str | None = None,
    legend: dict | None = None,
) -> dict:
    """Extract points/shapes from data, render the HTML artifact, and write it to disk.

    Returns {"path": str, "bytes": int, "features_rendered": int,
    "skipped_features": int}, plus "html" when inline=True and the document
    is under INLINE_MAX_BYTES, "truncated": True when the input exceeded
    MAX_RENDER_VERTICES, and "note" when `legend` triggered something worth
    flagging (see below). skipped_features counts rows/features present in
    `data` that couldn't be rendered — either malformed/unsupported geometry
    (extract_features) or dropped for exceeding MAX_RENDER_VERTICES
    (_cap_vertices) — render_map degrades rather than failing or writing an
    unbounded artifact.

    `summary` is the composed verdict on the one-pager (#310). When omitted,
    compose_summary() writes a short fallback from the payload itself.

    `legend` optionally maps a point's props["class"] to
    {"label": str, "color": str?} (#367) — see render_html's docstring for
    the full contract. "note" in the response reports anything the legend
    resolution had to paper over: an invalid color string (replaced with a
    palette color) or points whose class wasn't a legend key (rendered with
    the default marker dot) — never present when `legend` is omitted and no
    point carries a "class".
    """
    extracted = extract_features(data)
    points, shapes, dropped = _cap_vertices(
        extracted["points"], extracted["shapes"], MAX_RENDER_VERTICES
    )
    if summary is None:
        summary = compose_summary(data, points, shapes)
    doc = render_html(points, title=title, shapes=shapes, summary=summary, legend=legend)
    encoded = doc.encode("utf-8")

    directory = out_dir if out_dir is not None else artifact_dir()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"map_{int(time.time() * 1000)}_{_slug(title)}.html"
    path = directory / filename
    path.write_bytes(encoded)

    resolved_legend, color_notes = _normalize_legend(legend)
    if resolved_legend:
        _legend_present, unknown_class_count = _classify_features(points, resolved_legend)
    else:
        # No legend supplied: props["class"] is inert (renders exactly as
        # before #367), so it must not be reported as an "unknown class".
        unknown_class_count = 0
    note = _legend_note(unknown_class_count, color_notes)

    result = {
        "path": str(path),
        "bytes": len(encoded),
        "features_rendered": len(points) + len(shapes),
        "skipped_features": extracted["skipped_features"] + dropped,
    }
    if dropped:
        result["truncated"] = True
    if note:
        result["note"] = note
    if inline and len(encoded) <= INLINE_MAX_BYTES:
        result["html"] = doc
    return result


def export_report(
    data,
    title: str | None = None,
    summary: str | None = None,
    inline: bool = False,
    out_dir: Path | None = None,
    legend: dict | None = None,
) -> dict:
    """Write a shareable one-pager: map + verdict + stop list + attribution.

    Same envelope as write_artifact. `summary` is the verdict you would tell
    a spouse, co-founder, or landlord; when omitted a short fallback is
    composed from the payload. The file is dependency-free and opens at
    file:// with no network. `legend` is passed through to write_artifact
    unchanged — see its docstring.
    """
    return write_artifact(
        data, title=title, inline=inline, out_dir=out_dir, summary=summary, legend=legend
    )
