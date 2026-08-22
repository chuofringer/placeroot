"""Shareable one-pager export (issue #310).

render_map / export_report write one dependency-free HTML file: interactive
map, composed verdict, per-stop details, required attribution. These tests
pin the document shape and the fallback verdicts; the existing mapview
suite still owns projection, vertex caps, and the SVG viewer.
"""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from placeroot import mapview, server

CENTER_LAT = 40.700000
CENTER_LON = -73.900000

_EXTERNAL_REF_RE = re.compile(
    r"""
    (?:
        (?:src|href|action)\s*=\s*["\']?https?://   # quoted or unquoted attrs
      | url\s*\(\s*["\']?https?://                   # CSS url()
      | \bfetch\s*\(\s*[`"\']https?://               # fetch("https://...")
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _embedded_data(doc: str) -> dict:
    start = doc.index("var DATA = ") + len("var DATA = ")
    data, _ = json.JSONDecoder().raw_decode(doc[start:])
    return data


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


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def _parse(doc: str) -> _Parser:
    parser = _Parser()
    parser.feed(doc)
    parser.close()
    return parser


def test_export_report_writes_onepager_sections(tmp_path):
    result = mapview.export_report(
        {"results": _rows(3)},
        title="Saturday errands",
        summary="Pharmacy first, then hardware, then the post office.",
        out_dir=tmp_path,
    )
    assert set(result) == {"path", "bytes", "features_rendered", "skipped_features"}
    assert result["features_rendered"] == 3
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse(doc)
    assert 'id="verdict"' in doc
    assert "Pharmacy first, then hardware, then the post office." in doc
    assert 'id="stops"' in doc
    assert "Place 0" in doc
    assert "Place 2" in doc
    assert "coffee_shop" in doc
    assert "Overture Maps Foundation" in doc
    assert "OpenStreetMap contributors" in doc
    assert "local file" in doc
    assert not _EXTERNAL_REF_RE.search(doc)
    assert "cdn." not in doc.lower()


def test_export_report_escapes_summary_and_stop_names(tmp_path):
    payload = {
        "results": [
            {
                "name": "<script>alert(1)</script>",
                "lat": CENTER_LAT,
                "lon": CENTER_LON,
                "category": "cafe",
            }
        ]
    }
    result = mapview.export_report(
        payload,
        title="XSS",
        summary="<img src=x onerror=alert(1)>",
        out_dir=tmp_path,
    )
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in doc
    assert "<img src=x onerror=alert(1)>" not in doc
    assert "&lt;script&gt;" in doc
    assert "&lt;img" in doc


def test_write_artifact_without_summary_composes_a_fallback(tmp_path):
    result = mapview.write_artifact({"results": _rows(4)}, title="Cafes", out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert "4 places" in doc
    assert "Nearest: Place 0" in doc
    assert 'id="verdict"' in doc
    assert 'id="stops"' in doc


def test_compose_summary_find_places():
    text = mapview.compose_summary({"results": _rows(3)})
    assert text.startswith("3 places.")
    assert "Place 0" in text
    assert "0 m" in text


def test_compose_summary_summarize_area():
    text = mapview.compose_summary(
        {
            "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
            "radius_m": 1000,
            "total_places": 42,
            "top_categories": [
                {"category": "coffee_shop", "count": 10},
                {"category": "restaurant", "count": 8},
            ],
        }
    )
    assert "42 places" in text
    assert "1.0 km" in text
    assert "coffee_shop (10)" in text


def test_compose_summary_compare_areas():
    text = mapview.compose_summary(
        {
            "areas": [
                {
                    "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
                    "total_places": 80,
                    "density_per_km2": 25.5,
                },
                {
                    "center": {"lat": CENTER_LAT + 0.01, "lon": CENTER_LON},
                    "total_places": 12,
                    "density_per_km2": 3.8,
                },
            ],
            "categories": ["coffee_shop"],
            "differentiators": [{"category": "coffee_shop", "relative_difference": 1.0}],
        }
    )
    assert "Area 1: 80 places" in text
    assert "Area 2: 12 places" in text
    assert "coffee_shop" in text


def test_compose_summary_isochrone():
    text = mapview.compose_summary(
        {
            "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
            "minutes": 15,
            "mode": "walk",
            "polygon": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [CENTER_LON, CENTER_LAT],
                        [CENTER_LON + 0.01, CENTER_LAT],
                        [CENTER_LON + 0.01, CENTER_LAT + 0.01],
                        [CENTER_LON, CENTER_LAT + 0.01],
                        [CENTER_LON, CENTER_LAT],
                    ]
                ],
            },
            "stats": {"area_km2": 0.62},
        }
    )
    assert "15-minute walk" in text
    assert "0.62" in text


def test_compose_summary_optimize_route():
    text = mapview.compose_summary(
        {
            "order": [0, 2, 1],
            "legs": [],
            "total_distance_m": 2400,
            "total_duration_s": 420,
        }
    )
    assert "3-stop run" in text
    assert "2.4 km" in text
    assert "7 min" in text


def test_extract_features_compare_areas_becomes_one_marker_per_area():
    payload = {
        "areas": [
            {
                "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
                "total_places": 80,
                "density_per_km2": 25.5,
                "category_counts": {"coffee_shop": 10},
            },
            {
                "center": {"lat": CENTER_LAT + 0.02, "lon": CENTER_LON + 0.01},
                "total_places": 12,
                "density_per_km2": 3.8,
                "category_counts": {"coffee_shop": 1},
            },
        ],
        "categories": ["coffee_shop"],
        "differentiators": [{"category": "coffee_shop", "relative_difference": 0.9}],
    }
    extracted = mapview.extract_features(payload)
    assert extracted["skipped_features"] == 0
    assert len(extracted["points"]) == 2
    assert extracted["points"][0]["name"] == "Area 1"
    assert extracted["points"][1]["lat"] == CENTER_LAT + 0.02
    assert extracted["points"][0]["props"]["total_places"] == 80


def test_export_report_compare_areas_lists_both_areas(tmp_path):
    payload = {
        "areas": [
            {
                "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
                "total_places": 80,
                "density_per_km2": 25.5,
            },
            {
                "center": {"lat": CENTER_LAT + 0.02, "lon": CENTER_LON + 0.01},
                "total_places": 12,
                "density_per_km2": 3.8,
            },
        ],
        "categories": ["coffee_shop"],
        "differentiators": [{"category": "coffee_shop", "relative_difference": 0.9}],
    }
    result = mapview.export_report(payload, title="Two neighborhoods", out_dir=tmp_path)
    assert result["features_rendered"] == 2
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert "Area 1" in doc
    assert "Area 2" in doc
    assert "80 places" in doc


def test_summary_is_clipped_to_the_cap(tmp_path):
    huge = "word " * (mapview.SUMMARY_MAX_CHARS)
    result = mapview.export_report(
        {"results": _rows(1)}, title="Long", summary=huge, out_dir=tmp_path
    )
    doc = Path(result["path"]).read_text(encoding="utf-8")
    # The raw dump must not appear in full; an ellipsis marks the clip.
    assert "…" in doc
    assert huge not in doc


def test_render_map_passes_summary_through(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_ARTIFACT_DIR", str(tmp_path))
    result = server.render_map(
        {"results": _rows(2)},
        title="For the landlord",
        summary="This block has two cafes and no grocery.",
    )
    assert result["features_rendered"] == 2
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert "This block has two cafes and no grocery." in doc
    assert "For the landlord" in doc
    assert 'id="verdict"' in doc
    assert 'id="stops"' in doc


def test_onepager_still_has_interactive_map_hooks(tmp_path):
    result = mapview.export_report({"results": _rows(2)}, title="Map", out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert 'id="map"' in doc
    assert 'id="viewport"' in doc
    assert 'id="popup"' in doc
    assert "scale" in doc.lower()
    parser = _parse(doc)
    assert "svg" in parser.tags
    assert "script" in parser.tags


def test_external_ref_regex_catches_css_unquoted_form_and_fetch():
    assert _EXTERNAL_REF_RE.search('<img src="https://evil.example/x">')
    assert _EXTERNAL_REF_RE.search("<img src=https://evil.example/x>")
    assert _EXTERNAL_REF_RE.search("<link href='https://evil.example/x'>")
    assert _EXTERNAL_REF_RE.search("background:url(https://evil.example/x)")
    assert _EXTERNAL_REF_RE.search("background: url('https://evil.example/x')")
    assert _EXTERNAL_REF_RE.search('<form action="https://evil.example/x">')
    assert _EXTERNAL_REF_RE.search("<form action=https://evil.example/x>")
    assert _EXTERNAL_REF_RE.search('fetch("https://evil.example/x")')
    assert _EXTERNAL_REF_RE.search("fetch('https://evil.example/x')")
    # XML namespaces and in-page refs must not trip it.
    assert not _EXTERNAL_REF_RE.search('xmlns="http://www.w3.org/2000/svg"')
    assert not _EXTERNAL_REF_RE.search('<a href="#stops">')
    assert not _EXTERNAL_REF_RE.search("var SVGNS = \"http://www.w3.org/2000/svg\";")


def test_compose_summary_compare_areas_without_total_places():
    text = mapview.compose_summary(
        {
            "areas": [
                {
                    "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
                    "density_per_km2": 25.5,
                },
                {
                    "center": {"lat": CENTER_LAT + 0.01, "lon": CENTER_LON},
                    "total_places": 12,
                },
            ],
            "categories": ["coffee_shop"],
            "differentiators": [],
        }
    )
    assert "None places" not in text
    assert "Area 1" in text
    assert "25.5/km" in text
    assert "Area 2: 12 places" in text


def test_place_name_matching_template_token_does_not_break_js(tmp_path):
    payload = {
        "results": [
            {
                "name": "@@STOPS@@",
                "lat": CENTER_LAT,
                "lon": CENTER_LON,
                "category": "cafe",
            }
        ]
    }
    result = mapview.export_report(
        payload, title="@@SUMMARY@@", summary="Stay put.", out_dir=tmp_path
    )
    doc = Path(result["path"]).read_text(encoding="utf-8")
    data = _embedded_data(doc)
    assert data["points"][0]["name"] == "@@STOPS@@"
    # Title token is not rewritten by later substitutions.
    assert "<title>@@SUMMARY@@</title>" in doc
    assert "Stay put." in doc
    # Stop-list markup was not spliced into the script.
    script = doc[doc.index("<script>") : doc.index("</script>")]
    assert '<li class="stop"' not in script


def test_export_report_empty_payload(tmp_path):
    result = mapview.export_report({}, title="Empty", out_dir=tmp_path)
    assert result["features_rendered"] == 0
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse(doc)
    assert "No places to show." in doc
    assert 'id="empty-msg"' in doc
    assert "if (!POINTS.length && !SHAPES.length)" in doc
    data = _embedded_data(doc)
    assert data["points"] == []
    assert data["shapes"] == []
    assert "No stop details." in doc
    assert not _EXTERNAL_REF_RE.search(doc)


def test_export_report_shapes_only(tmp_path):
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Walkshed"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [CENTER_LON, CENTER_LAT],
                            [CENTER_LON + 0.01, CENTER_LAT],
                            [CENTER_LON + 0.01, CENTER_LAT + 0.01],
                            [CENTER_LON, CENTER_LAT + 0.01],
                            [CENTER_LON, CENTER_LAT],
                        ]
                    ],
                },
            }
        ],
    }
    result = mapview.export_report(payload, title="Shape only", out_dir=tmp_path)
    assert result["features_rendered"] == 1
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse(doc)
    assert "Walkshed" in doc
    assert "1 shape" in doc
    # Shape rows are styled as stops but have no data-idx (list click is inert).
    assert re.search(r'<li class="stop">', doc)
    assert not re.search(r'<li class="stop"[^>]*data-idx=', doc)
    assert 'id="verdict"' in doc
    data = _embedded_data(doc)
    assert data["points"] == []
    assert len(data["shapes"]) == 1
    assert not _EXTERNAL_REF_RE.search(doc)


# --- pin classes + legend (#367) --------------------------------------------


def _classed_rows(classes):
    """Like _rows(), but each row carries props["class"] = classes[i]."""
    rows = _rows(len(classes))
    for row, cls in zip(rows, classes):
        row["class"] = cls
    return rows


def test_render_map_legend_two_classes_get_distinct_fills_and_a_legend_box(tmp_path):
    payload = {"results": _classed_rows(["open", "closed"])}
    legend = {
        "open": {"label": "Open now", "color": "#009e73"},
        "closed": {"label": "Permanently closed", "color": "#d55e00"},
    }
    result = mapview.export_report(payload, title="Classed", legend=legend, out_dir=tmp_path)
    assert "note" not in result
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert 'class="legend"' in doc
    assert "Open now" in doc
    assert "Permanently closed" in doc
    data = _embedded_data(doc)
    assert {p["cls"] for p in data["points"]} == {"open", "closed"}
    assert data["legend"]["open"]["color"] == "#009e73"
    assert data["legend"]["closed"]["color"] == "#d55e00"


def test_render_map_legend_explicit_hex_color_is_honored(tmp_path):
    payload = {"results": _classed_rows(["hazard"])}
    legend = {"hazard": {"label": "Hazard", "color": "#abc"}}
    result = mapview.export_report(payload, legend=legend, out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    data = _embedded_data(doc)
    assert data["legend"]["hazard"]["color"] == "#abc"
    assert "background:#abc" in doc


def test_render_map_legend_invalid_color_falls_back_and_is_never_written(tmp_path):
    payload = {"results": _classed_rows(["hazard"])}
    evil = "red;} </style><script>alert(1)</script>"
    legend = {"hazard": {"label": "Hazard", "color": evil}}
    result = mapview.export_report(payload, legend=legend, out_dir=tmp_path)
    assert "note" in result
    assert "hazard" in result["note"]
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert evil not in doc
    data = _embedded_data(doc)
    color = data["legend"]["hazard"]["color"]
    assert re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", color)


def test_render_map_legend_unknown_class_falls_back_to_default_dot_and_notes(tmp_path):
    payload = {"results": _classed_rows(["open", "mystery"])}
    legend = {"open": {"label": "Open now"}}
    result = mapview.export_report(payload, legend=legend, out_dir=tmp_path)
    assert "note" in result
    assert "1 feature" in result["note"]
    doc = Path(result["path"]).read_text(encoding="utf-8")
    data = _embedded_data(doc)
    classed = [p for p in data["points"] if "cls" in p]
    assert len(classed) == 1
    assert classed[0]["cls"] == "open"
    assert "mystery" not in data.get("legend", {})


def test_render_map_legend_labels_are_html_escaped(tmp_path):
    payload = {"results": _classed_rows(["danger"])}
    legend = {"danger": {"label": "<script>alert(1)</script>"}}
    result = mapview.export_report(payload, legend=legend, out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in doc
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc


def test_render_map_classless_input_is_unaffected_by_legend_support(tmp_path):
    payload = {"results": _rows(3)}
    result = mapview.export_report(payload, title="Plain", out_dir=tmp_path)
    assert set(result) == {"path", "bytes", "features_rendered", "skipped_features"}
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert 'class="legend"' not in doc
    data = _embedded_data(doc)
    assert "legend" not in data
    assert all("cls" not in p for p in data["points"])


def test_render_map_tool_accepts_legend_and_returns_note(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_ARTIFACT_DIR", str(tmp_path))
    result = server.render_map(
        {"results": _classed_rows(["open", "mystery"])},
        title="Legend via tool",
        legend={"open": {"label": "Open now", "color": "#009e73"}},
    )
    assert "note" in result
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert "Open now" in doc
