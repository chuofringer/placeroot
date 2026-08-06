import re
from html.parser import HTMLParser

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
    assert set(result) == {"path", "bytes", "features_rendered"}
    assert result["features_rendered"] == 8
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
