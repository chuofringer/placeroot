"""Antimeridian (dateline) handling for render_map's projection (#137).

_unwrap_longitudes shifts longitudes onto a continuous range in Python,
before they're serialized into the HTML artifact's embedded DATA JSON, so
the JS renderer's local-tangent-plane projection (plain Math.min/Math.max
over raw longitudes) doesn't mistake a small dateline-crossing gap for a
~360-degree span. See mapview._unwrap_longitudes's docstring for the
algorithm and its heuristic.
"""

import json

from placeroot import mapview


def test_unwrap_shrinks_dateline_crossing_point_span():
    points = [
        {"lat": 0.0, "lon": 179.9, "name": "a", "props": {}},
        {"lat": 0.0, "lon": -179.9, "name": "b", "props": {}},
    ]
    mapview._unwrap_longitudes(points, [])
    lons = [p["lon"] for p in points]
    assert max(lons) - min(lons) < 1.0
    # unaffected point keeps its value, the negative one gets +360
    assert 179.9 in lons
    assert 180.1 in lons


def test_unwrap_shrinks_dateline_crossing_shape_span():
    shapes = [
        {
            "kind": "line",
            "lines": [[[179.5, 10.0], [-179.5, 10.0], [-179.8, 10.0]]],
            "name": "route",
            "props": {},
        }
    ]
    mapview._unwrap_longitudes([], shapes)
    lons = [c[0] for c in shapes[0]["lines"][0]]
    assert max(lons) - min(lons) < 1.5
    # negatives were shifted into the continuous range above 180
    assert all(lon >= 179.0 for lon in lons)


def test_unwrap_mixed_points_and_shapes_consistent():
    points = [{"lat": 0.0, "lon": 179.95, "name": "a", "props": {}}]
    shapes = [
        {
            "kind": "polygon",
            "rings": [[[179.0, 1.0], [-179.0, 1.0], [-179.0, -1.0], [179.0, -1.0], [179.0, 1.0]]],
            "name": "poly",
            "props": {},
        }
    ]
    mapview._unwrap_longitudes(points, shapes)
    all_lons = [points[0]["lon"]] + [c[0] for c in shapes[0]["rings"][0]]
    assert max(all_lons) - min(all_lons) < 3.0


def test_unwrap_leaves_normal_data_untouched():
    """Regression guard: ordinary non-dateline data (span <= 180) must not
    be shifted at all, even where a point happens to have a negative
    longitude — the fix must be a no-op outside the antimeridian case."""
    points = [
        {"lat": 40.7, "lon": -73.9, "name": "a", "props": {}},
        {"lat": 40.71, "lon": -74.0, "name": "b", "props": {}},
        {"lat": 40.69, "lon": -73.8, "name": "c", "props": {}},
    ]
    original = [dict(p) for p in points]
    mapview._unwrap_longitudes(points, [])
    assert points == original


def test_unwrap_empty_is_noop():
    points: list = []
    shapes: list = []
    mapview._unwrap_longitudes(points, shapes)  # must not raise
    assert points == []
    assert shapes == []


def test_render_html_dateline_points_have_small_lon_span_in_data_json():
    points = [
        {"lat": 10.0, "lon": 179.9, "name": "a", "props": {}},
        {"lat": 10.0, "lon": -179.9, "name": "b", "props": {}},
    ]
    doc = mapview.render_html(points, title="Dateline")
    start = doc.index("var DATA = ") + len("var DATA = ")
    end = doc.index(";", start)
    data = json.loads(doc[start:end])
    lons = [p["lon"] for p in data["points"]]
    assert max(lons) - min(lons) < 1.0


def test_write_artifact_dateline_straddling_input_does_not_crash(tmp_path):
    payload = {
        "results": [
            {"name": "Fiji East", "lat": -17.7, "lon": 179.9},
            {"name": "Fiji West", "lat": -17.8, "lon": -179.9},
        ]
    }
    result = mapview.write_artifact(payload, title="Dateline Area", out_dir=tmp_path)
    assert result["features_rendered"] == 2
    from pathlib import Path

    written = Path(result["path"])
    assert written.exists()
    text = written.read_text()
    start = text.index("var DATA = ") + len("var DATA = ")
    end = text.index(";", start)
    data = json.loads(text[start:end])
    lons = [p["lon"] for p in data["points"]]
    assert max(lons) - min(lons) < 1.0
