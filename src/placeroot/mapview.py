"""Self-contained HTML map artifact renderer (issue #15).

Turns find_places / summarize_area JSON — or caller-supplied GeoJSON — into
ONE standalone HTML file: inline CSS, inline JS, vector data embedded, no
CDN, no tile server, no API key, zero network requests when opened. The
viewer is a small hand-written SVG pan/zoom map (equirectangular projection,
cos(lat) corrected so both axes share one meters-per-unit scale) with marker
dots, click-to-open popups, a scale bar, and an attribution line — not an
embedded copy of Leaflet/MapLibre.

The MCP tool boundary (server.render_map) returns only a tiny
{"path", "bytes", "features_rendered"} envelope so the (potentially large)
HTML never counts against the token budget in ROADMAP.md's "answers, not
data dumps" rule — the file on disk is the artifact.
"""

import html
import json
import math
import os
import time
from pathlib import Path

from placeroot import cache

ATTRIBUTION = "© Overture Maps Foundation contributors"

# Returning the HTML inline (inline=True) is only offered under this size —
# above it the point of keeping the tool response small is defeated.
INLINE_MAX_BYTES = 300_000

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
  var svg = document.getElementById("map");
  var viewport = document.getElementById("viewport");
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
    return String(v);
  }

  if (!DATA.length) {
    emptyMsg.style.display = "flex";
    return;
  }

  var lats = DATA.map(function (p) { return p.lat; });
  var lons = DATA.map(function (p) { return p.lon; });
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

  DATA.forEach(function (p, i) {
    var xy = project(p.lat, p.lon);
    p._x = xy[0]; p._y = xy[1];
    var g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("class", "marker");
    var c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    c.setAttribute("cx", xy[0]); c.setAttribute("cy", xy[1]); c.setAttribute("r", 6);
    g.appendChild(c);
    if (p.name) {
      var t = document.createElementNS("http://www.w3.org/2000/svg", "text");
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

  var dragging = null;
  svg.addEventListener("pointerdown", function (ev) {
    dragging = { x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y };
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener("pointermove", function (ev) {
    if (!dragging) return;
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


def render_html(points: list[dict], title: str | None = None) -> str:
    """Render points (as produced by extract_points) into one standalone HTML document."""
    safe_title = html.escape(title or "PlaceRoot map", quote=True)
    count = len(points)
    count_label = f"{count} place{'s' if count != 1 else ''}"
    data_for_js = [
        {"lat": p["lat"], "lon": p["lon"], "name": p.get("name"), "props": p.get("props") or {}}
        for p in points
    ]
    # default=str: props may carry values (e.g. Decimal-ish) json can't natively
    # serialize; "</script>" guard prevents the embedded JSON from closing the tag early.
    data_json = json.dumps(data_for_js, default=str).replace("</", "<\\/")

    js = _JS.replace("__DATA_JSON__", data_json)
    doc = (
        _DOC.replace("@@CSS@@", _CSS)
        .replace("@@JS@@", js)
        .replace("@@TITLE@@", safe_title)
        .replace("@@COUNT@@", html.escape(count_label))
        .replace("@@ATTRIBUTION@@", html.escape(ATTRIBUTION))
    )
    return doc


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
    """Extract points from data, render the HTML artifact, and write it to disk.

    Returns {"path": str, "bytes": int, "features_rendered": int}, plus
    "html" when inline=True and the document is under INLINE_MAX_BYTES.
    """
    points = extract_points(data)
    doc = render_html(points, title=title)
    encoded = doc.encode("utf-8")

    directory = out_dir if out_dir is not None else artifact_dir()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"map_{int(time.time() * 1000)}_{_slug(title)}.html"
    path = directory / filename
    path.write_bytes(encoded)

    result = {
        "path": str(path),
        "bytes": len(encoded),
        "features_rendered": len(points),
    }
    if inline and len(encoded) <= INLINE_MAX_BYTES:
        result["html"] = doc
    return result
