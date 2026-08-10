"""Self-contained HTML map artifact renderer (issue #15, extended by #34).

Turns find_places / summarize_area JSON — or caller-supplied GeoJSON — into
ONE standalone HTML file: inline CSS, inline JS, vector data embedded, no
CDN, no tile server, no API key, zero network requests when opened. The
viewer is a small hand-written SVG pan/zoom map (equirectangular projection,
cos(lat) corrected so both axes share one meters-per-unit scale) with marker
dots, polygon/line shapes, click-to-open popups, a scale bar, and an
attribution line — not an embedded copy of Leaflet/MapLibre.

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
  margin: 0; padding: 0; height: 100%;
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
#app { display: flex; flex-direction: column; height: 100%; }
header {
  display: flex; align-items: baseline; gap: 0.6em;
  padding: 0.7em 1em; border-bottom: 1px solid var(--border);
  background: var(--panel-bg);
}
header h1 { font-size: 1.05rem; margin: 0; font-weight: 600; }
header .count { color: var(--muted); font-size: 0.85rem; }
#map-container { position: relative; flex: 1; min-height: 0; }
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
  var SVGNS = "http://www.w3.org/2000/svg";
  var svg = document.getElementById("map");
  var viewport = document.getElementById("viewport");
  var shapesGroup = document.getElementById("shapes");
  var markersGroup = document.getElementById("markers");
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

  SHAPES.forEach(function (s) {
    if (s.kind === "polygon" && s.rings && s.rings.length) {
      var d = "";
      s.rings.forEach(function (ring) { d += pathFromRing(ring); });
      var path = document.createElementNS(SVGNS, "path");
      path.setAttribute("d", d.trim());
      path.setAttribute("fill-rule", "evenodd");
      path.setAttribute("class", "shape-polygon");
      var c0 = centroidOf(s.rings[0]);
      var entity = { name: s.name, props: s.props || {}, _x: c0[0], _y: c0[1] };
      path.addEventListener("click", function (ev) {
        ev.stopPropagation();
        showPopup(entity);
      });
      shapesGroup.appendChild(path);
    } else if (s.kind === "line" && s.lines && s.lines.length) {
      s.lines.forEach(function (line) {
        var parts = line.map(function (c, idx) {
          var xy = project(c[1], c[0]);
          return (idx === 0 ? "M" : "L") + xy[0].toFixed(2) + "," + xy[1].toFixed(2);
        });
        var path = document.createElementNS(SVGNS, "path");
        path.setAttribute("d", parts.join(" "));
        path.setAttribute("class", "shape-line");
        var c0 = centroidOf(line);
        var entity = { name: s.name, props: s.props || {}, _x: c0[0], _y: c0[1] };
        path.addEventListener("click", function (ev) {
          ev.stopPropagation();
          showPopup(entity);
        });
        shapesGroup.appendChild(path);
      });
    }
  });

  POINTS.forEach(function (p, i) {
    var xy = project(p.lat, p.lon);
    p._x = xy[0]; p._y = xy[1];
    var g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "marker");
    var c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", xy[0]); c.setAttribute("cy", xy[1]); c.setAttribute("r", 6);
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
  <div id="map-container">
    <svg id="map" viewBox="0 0 1000 640" preserveAspectRatio="xMidYMid meet">
      <g id="viewport">
        <g id="shapes"></g>
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
    <div class="attribution">@@ATTRIBUTION@@</div>
    <div class="popup" id="popup" style="display:none"></div>
  </div>
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


def render_html(
    points: list[dict], title: str | None = None, shapes: list[dict] | None = None
) -> str:
    """Render points + shapes (as produced by extract_points/extract_features) into one HTML doc.

    `shapes` is optional and defaults to none, so callers that only ever
    dealt with points (extract_points()) keep working unchanged.
    """
    shapes = shapes or []
    safe_title = html.escape(title or "PlaceRoot map", quote=True)
    count = len(points)
    shape_count = len(shapes)
    label_parts = []
    if count or not shape_count:
        label_parts.append(f"{count} place{'s' if count != 1 else ''}")
    if shape_count:
        label_parts.append(f"{shape_count} shape{'s' if shape_count != 1 else ''}")
    count_label = " · ".join(label_parts)

    points_for_js = [
        {"lat": p["lat"], "lon": p["lon"], "name": p.get("name"), "props": p.get("props") or {}}
        for p in points
    ]
    shapes_for_js = []
    for s in shapes:
        entry = {"kind": s.get("kind"), "name": s.get("name"), "props": s.get("props") or {}}
        if s.get("kind") == "polygon":
            entry["rings"] = s.get("rings") or []
        else:
            entry["lines"] = s.get("lines") or []
        shapes_for_js.append(entry)

    # Antimeridian handling (#137): unwrap longitudes onto a continuous
    # range in place before they're serialized, so the JS projection's plain
    # min/max over raw longitudes below doesn't blow up on dateline-crossing
    # data. No-op for the common (non-dateline) case.
    _unwrap_longitudes(points_for_js, shapes_for_js)

    # default=str: props may carry values (e.g. Decimal-ish) json can't natively
    # serialize; "</script>" guard prevents the embedded JSON from closing the tag early.
    data_json = json.dumps(
        {"points": points_for_js, "shapes": shapes_for_js}, default=str
    ).replace("</", "<\\/")

    js = _JS.replace("__DATA_JSON__", data_json)
    doc = (
        _DOC.replace("@@CSS@@", _CSS)
        .replace("@@JS@@", js)
        .replace("@@TITLE@@", safe_title)
        .replace("@@COUNT@@", html.escape(count_label))
        .replace("@@ATTRIBUTION@@", html.escape(ATTRIBUTION))
    )
    return doc


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
) -> dict:
    """Extract points/shapes from data, render the HTML artifact, and write it to disk.

    Returns {"path": str, "bytes": int, "features_rendered": int,
    "skipped_features": int}, plus "html" when inline=True and the document
    is under INLINE_MAX_BYTES, and "truncated": True when the input exceeded
    MAX_RENDER_VERTICES. skipped_features counts rows/features present in
    `data` that couldn't be rendered — either malformed/unsupported geometry
    (extract_features) or dropped for exceeding MAX_RENDER_VERTICES
    (_cap_vertices) — render_map degrades rather than failing or writing an
    unbounded artifact.
    """
    extracted = extract_features(data)
    points, shapes, dropped = _cap_vertices(
        extracted["points"], extracted["shapes"], MAX_RENDER_VERTICES
    )
    doc = render_html(points, title=title, shapes=shapes)
    encoded = doc.encode("utf-8")

    directory = out_dir if out_dir is not None else artifact_dir()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"map_{int(time.time() * 1000)}_{_slug(title)}.html"
    path = directory / filename
    path.write_bytes(encoded)

    result = {
        "path": str(path),
        "bytes": len(encoded),
        "features_rendered": len(points) + len(shapes),
        "skipped_features": extracted["skipped_features"] + dropped,
    }
    if dropped:
        result["truncated"] = True
    if inline and len(encoded) <= INLINE_MAX_BYTES:
        result["html"] = doc
    return result
