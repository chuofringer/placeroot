import re
from html.parser import HTMLParser
from pathlib import Path

from placeroot import mapview

CENTER_LAT = 40.700000
CENTER_LON = -73.900000

# http(s):// appearing as an actual src="..."/href="..." attribute value —
# i.e. a reference the browser would fetch. Deliberately does not flag bare
# "https://" text sitting inside comments or plain attribution copy.
_EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.IGNORECASE)


def _rows(n):
    return [
        {
            "name": f"Place {i}",
            "category": "coffee_shop",
            "basic_category": "coffee_shop",
            "operating_status": "open",
            "confidence": 0.9,
            "lat": CENTER_LAT + i * 0.001,
            "lon": CENTER_LON + i * 0.001,
            "distance_m": i * 10,
        }
        for i in range(n)
    ]


class _StructureCheckingParser(HTMLParser):
    """Just confirms the document parses as a sane HTML skeleton."""

    def __init__(self):
        super().__init__()
        self.tags_seen = []

    def handle_starttag(self, tag, attrs):
        self.tags_seen.append(tag)


def _parse_ok(doc: str) -> _StructureCheckingParser:
    parser = _StructureCheckingParser()
    parser.feed(doc)
    parser.close()
    return parser


# --- extract_points -----------------------------------------------------


def test_extract_points_from_find_places_results():
    payload = {"results": _rows(5)}
    points = mapview.extract_points(payload)
    assert len(points) == 5
    assert points[0]["name"] == "Place 0"
    assert points[0]["lat"] == CENTER_LAT


def test_extract_points_skips_rows_missing_lat_lon():
    payload = {"results": _rows(3) + [{"name": "no coords"}]}
    points = mapview.extract_points(payload)
    assert len(points) == 3


def test_extract_points_from_summarize_area():
    payload = {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "radius_m": 1000,
        "total_places": 42,
        "top_categories": [{"category": "coffee_shop", "count": 10}],
    }
    points = mapview.extract_points(payload)
    assert len(points) == 1
    assert points[0]["lat"] == CENTER_LAT
    assert points[0]["lon"] == CENTER_LON
    assert points[0]["props"]["total_places"] == 42


def test_extract_points_from_geojson_feature_collection():
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [CENTER_LON, CENTER_LAT]},
                "properties": {"name": "Custom Spot", "kind": "landmark"},
            }
        ],
    }
    points = mapview.extract_points(payload)
    assert len(points) == 1
    assert points[0]["name"] == "Custom Spot"
    assert points[0]["lon"] == CENTER_LON
    assert points[0]["lat"] == CENTER_LAT


def test_extract_points_from_bare_list_of_rows():
    points = mapview.extract_points(_rows(4))
    assert len(points) == 4


def test_extract_points_ignores_non_point_geometry():
    payload = {
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[]]},
                "properties": {"name": "Area"},
            }
        ]
    }
    assert mapview.extract_points(payload) == []


def test_extract_points_handles_none_and_empty():
    assert mapview.extract_points(None) == []
    assert mapview.extract_points({}) == []
    assert mapview.extract_points([]) == []
    assert mapview.extract_points({"results": []}) == []


def test_extract_points_rejects_non_finite_coords():
    payload = {"results": [{"name": "x", "lat": float("nan"), "lon": 1.0}]}
    assert mapview.extract_points(payload) == []


# --- render_html ---------------------------------------------------------


def test_render_html_has_no_external_references():
    points = mapview.extract_points({"results": _rows(6)})
    doc = mapview.render_html(points, title="Test Map")
    assert not _EXTERNAL_REF_RE.search(doc)
    # Belt-and-suspenders: no CDN-style script/link tags at all.
    assert "cdn." not in doc.lower()
    assert "<link" not in doc.lower() or "stylesheet" not in doc.lower()


def test_render_html_is_well_formed():
    points = mapview.extract_points({"results": _rows(3)})
    doc = mapview.render_html(points, title="Test Map")
    parser = _parse_ok(doc)
    assert "html" in parser.tags_seen
    assert "head" in parser.tags_seen
    assert "body" in parser.tags_seen
    assert "svg" in parser.tags_seen
    assert "script" in parser.tags_seen
    assert doc.strip().lower().startswith("<!doctype html>")


def test_render_html_embeds_data_and_labels():
    points = mapview.extract_points({"results": _rows(3)})
    doc = mapview.render_html(points, title="Test Map")
    assert "Place 0" in doc
    assert "Place 1" in doc
    assert "Place 2" in doc
    assert "popup" in doc  # click-to-open popup element/logic present


def test_render_html_has_scale_bar_and_attribution():
    points = mapview.extract_points({"results": _rows(3)})
    doc = mapview.render_html(points, title="Test Map")
    assert "scale" in doc.lower()
    assert "Overture Maps Foundation" in doc


def test_render_html_escapes_title():
    points = mapview.extract_points({"results": _rows(1)})
    doc = mapview.render_html(points, title="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in doc
    assert "&lt;script&gt;" in doc


def test_render_html_empty_result_still_valid():
    doc = mapview.render_html([], title="Empty")
    _parse_ok(doc)  # must not raise
    assert "No places to show" in doc
    assert "NaN" not in doc


def test_render_html_size_scales_reasonably_with_n():
    small = mapview.render_html(mapview.extract_points({"results": _rows(5)}), "Small")
    large = mapview.render_html(mapview.extract_points({"results": _rows(25)}), "Large")
    assert len(large.encode("utf-8")) > len(small.encode("utf-8"))
    # 25 modest rows should stay well under a sane ceiling for one HTML file.
    assert len(large.encode("utf-8")) < 200_000


def test_render_html_json_payload_cannot_break_out_of_script_tag():
    rows = _rows(1)
    rows[0]["name"] = "</script><script>alert(1)</script>"
    doc = mapview.render_html(mapview.extract_points({"results": rows}), "Danger")
    assert "</script><script>alert(1)</script>" not in doc


# --- write_artifact --------------------------------------------------------


def test_write_artifact_writes_file_and_returns_small_envelope(tmp_path):
    payload = {"results": _rows(8)}
    result = mapview.write_artifact(payload, title="My Area", out_dir=tmp_path)
    assert set(result) == {"path", "bytes", "features_rendered", "skipped_features"}
    assert result["features_rendered"] == 8
    assert result["skipped_features"] == 0
    from pathlib import Path

    written = Path(result["path"])
    assert written.exists()
    assert written.stat().st_size == result["bytes"]
    assert written.parent == tmp_path


def test_write_artifact_inline_returns_html_when_small(tmp_path):
    payload = {"results": _rows(3)}
    result = mapview.write_artifact(payload, title="Inline", inline=True, out_dir=tmp_path)
    assert "html" in result
    assert "<!doctype html>" in result["html"].lower()


def test_write_artifact_no_inline_by_default(tmp_path):
    payload = {"results": _rows(3)}
    result = mapview.write_artifact(payload, title="No Inline", out_dir=tmp_path)
    assert "html" not in result


def test_write_artifact_empty_result(tmp_path):
    result = mapview.write_artifact({"results": []}, title="Nothing Here", out_dir=tmp_path)
    assert result["features_rendered"] == 0
    from pathlib import Path

    assert Path(result["path"]).exists()


def test_artifact_dir_default_sits_alongside_cache_dir(monkeypatch):
    monkeypatch.delenv("PLACEROOT_ARTIFACT_DIR", raising=False)
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", "/tmp/placeroot-cache-test/cache")
    d = mapview.artifact_dir()
    assert d.name == "artifacts"
    assert d.parent.name == "placeroot-cache-test"


def test_artifact_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PLACEROOT_ARTIFACT_DIR", str(tmp_path / "custom"))
    assert mapview.artifact_dir() == tmp_path / "custom"


# --- extract_features: shapes (#34) -----------------------------------------

_OUTER_RING = [
    [CENTER_LON - 0.01, CENTER_LAT - 0.01],
    [CENTER_LON + 0.01, CENTER_LAT - 0.01],
    [CENTER_LON + 0.01, CENTER_LAT + 0.01],
    [CENTER_LON - 0.01, CENTER_LAT + 0.01],
    [CENTER_LON - 0.01, CENTER_LAT - 0.01],
]
_HOLE_RING = [
    [CENTER_LON - 0.003, CENTER_LAT - 0.003],
    [CENTER_LON + 0.003, CENTER_LAT - 0.003],
    [CENTER_LON + 0.003, CENTER_LAT + 0.003],
    [CENTER_LON - 0.003, CENTER_LAT + 0.003],
    [CENTER_LON - 0.003, CENTER_LAT - 0.003],
]


def test_extract_features_polygon_with_hole_keeps_both_rings():
    payload = {
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [_OUTER_RING, _HOLE_RING]},
                "properties": {"name": "Park with pond"},
            }
        ]
    }
    result = mapview.extract_features(payload)
    assert result["skipped_features"] == 0
    assert len(result["shapes"]) == 1
    shape = result["shapes"][0]
    assert shape["kind"] == "polygon"
    assert len(shape["rings"]) == 2
    assert shape["rings"][0] == _OUTER_RING
    assert shape["rings"][1] == _HOLE_RING


def test_extract_features_multipolygon_flattens_rings():
    payload = {
        "type": "Feature",
        "geometry": {"type": "MultiPolygon", "coordinates": [[_OUTER_RING], [_HOLE_RING]]},
        "properties": {"name": "Two blocks"},
    }
    result = mapview.extract_features(payload)
    assert len(result["shapes"]) == 1
    assert len(result["shapes"][0]["rings"]) == 2


def test_extract_features_linestring_and_multilinestring():
    line_a = [[CENTER_LON, CENTER_LAT], [CENTER_LON + 0.01, CENTER_LAT + 0.01]]
    line_b = [[CENTER_LON + 0.02, CENTER_LAT], [CENTER_LON + 0.03, CENTER_LAT]]
    payload = {
        "features": [
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": line_a},
             "properties": {"name": "Trail"}},
            {"type": "Feature", "geometry": {"type": "MultiLineString",
             "coordinates": [line_a, line_b]}, "properties": {"name": "Trail network"}},
        ]
    }
    result = mapview.extract_features(payload)
    assert result["skipped_features"] == 0
    kinds = [s["kind"] for s in result["shapes"]]
    assert kinds == ["line", "line"]
    assert result["shapes"][0]["lines"] == [line_a]
    assert result["shapes"][1]["lines"] == [line_a, line_b]


def test_extract_features_isochrone_result_shape():
    isochrone_result = {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "minutes": 15,
        "mode": "walk",
        "speed_m_s": 1.4,
        "polygon": {"type": "Polygon", "coordinates": [_OUTER_RING]},
        "polygon_method": "convex_hull",
        "stats": {"reachable_nodes": 42, "max_radius_m": 950.3, "area_km2": 0.51},
    }
    result = mapview.extract_features(isochrone_result)
    assert result["skipped_features"] == 0
    assert len(result["points"]) == 0
    assert len(result["shapes"]) == 1
    shape = result["shapes"][0]
    assert shape["kind"] == "polygon"
    assert shape["rings"] == [_OUTER_RING]
    assert shape["props"]["reachable_nodes"] == 42
    assert shape["props"]["max_radius_m"] == 950.3
    assert shape["props"]["minutes"] == 15
    assert "CENTER_LAT" not in str(shape["props"]["center"])  # sanity: it's formatted, not repr'd


def test_write_artifact_isochrone_result_renders_one_polygon_and_stats_popup(tmp_path):
    isochrone_result = {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "minutes": 15,
        "mode": "walk",
        "polygon": {"type": "Polygon", "coordinates": [_OUTER_RING]},
        "stats": {"reachable_nodes": 42, "max_radius_m": 950.3, "area_km2": 0.51},
    }
    result = mapview.write_artifact(isochrone_result, title="Iso", out_dir=tmp_path)
    assert result["features_rendered"] == 1
    assert result["skipped_features"] == 0
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert "reachable_nodes" in doc
    assert "42" in doc
    assert "Isochrone" in doc


def test_extract_features_mixed_points_and_polygon_bounds_data():
    payload = {
        "features": [
            {"type": "Feature", "geometry": {"type": "Point",
             "coordinates": [CENTER_LON + 0.5, CENTER_LAT + 0.5]},
             "properties": {"name": "Far point"}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [_OUTER_RING]},
             "properties": {"name": "Near polygon"}},
        ]
    }
    result = mapview.extract_features(payload)
    assert len(result["points"]) == 1
    assert len(result["shapes"]) == 1
    doc = mapview.render_html(result["points"], title="Mixed", shapes=result["shapes"])
    # every vertex needed for client-side bounds fitting must be embedded.
    assert str(CENTER_LON + 0.5) in doc
    assert str(CENTER_LAT + 0.5) in doc
    for lon, lat in _OUTER_RING:
        assert str(lon) in doc
        assert str(lat) in doc


def test_render_html_with_shapes_has_no_external_references():
    payload = {
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [_OUTER_RING]},
             "properties": {"name": "Area"}},
        ]
    }
    result = mapview.extract_features(payload)
    doc = mapview.render_html(result["points"], title="Shapes", shapes=result["shapes"])
    assert not _EXTERNAL_REF_RE.search(doc)


def test_extract_features_malformed_geometry_is_skipped_and_counted():
    payload = {
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[]]},
             "properties": {}},
            {"type": "Feature", "geometry": {"type": "Polygon",
             "coordinates": [[[0, 0], [1, 1]]]}, "properties": {}},  # ring too short
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0]]},
             "properties": {}},  # line needs >= 2 points
            {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": []},
             "properties": {}},
            {"type": "Feature", "geometry": {"type": "Polygon",
             "coordinates": [[["a", "b"], [1, 1], [2, 2], [0, 0]]]}, "properties": {}},
            {"type": "Feature", "geometry": {"type": "Sphere", "coordinates": []},
             "properties": {}},  # unsupported geometry type
            {"type": "Feature", "geometry": {"type": "Point",
             "coordinates": [CENTER_LON, CENTER_LAT]}, "properties": {"name": "Good point"}},
        ]
    }
    result = mapview.extract_features(payload)
    assert len(result["points"]) == 1
    assert len(result["shapes"]) == 0
    assert result["skipped_features"] == 6


def test_write_artifact_malformed_geometry_degrades_gracefully(tmp_path):
    payload = {
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[]]},
             "properties": {}},
            {"type": "Feature", "geometry": {"type": "Point",
             "coordinates": [CENTER_LON, CENTER_LAT]}, "properties": {"name": "Good"}},
        ]
    }
    result = mapview.write_artifact(payload, title="Degrade", out_dir=tmp_path)
    assert result["features_rendered"] == 1
    assert result["skipped_features"] == 1
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse_ok(doc)  # must not raise even with a partially-malformed input


# --- render_html: shapes (#34) ----------------------------------------------


def test_render_html_renders_shapes_group_and_evenodd_fill_rule():
    shapes = [{"kind": "polygon", "rings": [_OUTER_RING, _HOLE_RING], "name": "Hole test",
               "props": {}}]
    doc = mapview.render_html([], title="Shapes only", shapes=shapes)
    assert "shape-polygon" in doc
    assert "fill-rule" in doc
    assert '"kind": "polygon"' in doc or '"kind":"polygon"' in doc


# --- Issue #74: MAX_RENDER_VERTICES cap (defense in depth) -----------------


def test_cap_vertices_keeps_everything_under_the_cap():
    points = [{"lat": 0.0, "lon": 0.0}] * 5
    shapes = [{"kind": "line", "lines": [[[0, 0], [1, 1]]]}]  # 2 vertices
    kept_points, kept_shapes, dropped = mapview._cap_vertices(points, shapes, 100)
    assert kept_points == points
    assert kept_shapes == shapes
    assert dropped == 0


def test_cap_vertices_drops_points_past_the_cap():
    points = [{"lat": 0.0, "lon": 0.0}] * 10
    kept_points, kept_shapes, dropped = mapview._cap_vertices(points, [], 4)
    assert len(kept_points) == 4
    assert kept_shapes == []
    assert dropped == 6


def test_cap_vertices_drops_a_shape_that_would_exceed_the_cap():
    points = [{"lat": 0.0, "lon": 0.0}] * 3
    small_shape = {"kind": "line", "lines": [[[0, 0], [1, 1]]]}  # 2 vertices
    big_shape = {"kind": "polygon", "rings": [[[0, 0]] * 20]}  # 20 vertices
    kept_points, kept_shapes, dropped = mapview._cap_vertices(
        points, [small_shape, big_shape], 6
    )
    assert kept_points == points
    assert kept_shapes == [small_shape]
    assert dropped == 1


def test_write_artifact_truncates_points_past_vertex_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(mapview, "MAX_RENDER_VERTICES", 5)
    payload = {"results": _rows(10)}
    result = mapview.write_artifact(payload, title="Too Many", out_dir=tmp_path)
    assert result["truncated"] is True
    assert result["features_rendered"] == 5
    assert result["skipped_features"] == 5
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse_ok(doc)  # still a valid, bounded artifact


def test_write_artifact_not_truncated_under_normal_cap():
    """Sanity: the real MAX_RENDER_VERTICES default is far above realistic
    tool output, so ordinary inputs must never hit the cap or gain the
    truncated key — this pins the exact-envelope assertion elsewhere too."""
    assert mapview.MAX_RENDER_VERTICES > 1000


def test_render_html_no_shapes_backward_compatible():
    points = mapview.extract_points({"results": _rows(2)})
    doc = mapview.render_html(points, title="No shapes")
    assert "shape-polygon" in doc  # CSS class always present (empty shapes array)
    assert '"shapes": []' in doc or '"shapes":[]' in doc
