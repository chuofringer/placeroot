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


# --- shape style roles, labels, callouts (#368) -----------------------------

_OUTER_RING = [
    [CENTER_LON - 0.01, CENTER_LAT - 0.01],
    [CENTER_LON + 0.01, CENTER_LAT - 0.01],
    [CENTER_LON + 0.01, CENTER_LAT + 0.01],
    [CENTER_LON - 0.01, CENTER_LAT + 0.01],
    [CENTER_LON - 0.01, CENTER_LAT - 0.01],
]


def _polygon_feature(props: dict) -> dict:
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [_OUTER_RING]},
    }


def test_shed_role_gets_dashed_low_opacity_fill_styling(tmp_path):
    payload = {"features": [_polygon_feature({"name": "5-min shed", "role": "shed"})]}
    result = mapview.export_report(payload, title="Shed", out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse(doc)
    assert "role-shed" in doc
    assert "fill-opacity: 0.15" in doc
    assert "stroke-dasharray" in doc
    # The 0.15 rule shares specificity with the generic .shape-polygon:hover
    # rule and is declared later, so sheds need their own hover override to
    # keep hover feedback.
    assert ".shape-polygon.role-shed:hover { fill-opacity: 0.4; }" in doc
    data = _embedded_data(doc)
    assert data["shapes"][0]["role"] == "shed"


def test_outline_role_gets_no_fill_strong_edge_styling(tmp_path):
    payload = {"features": [_polygon_feature({"name": "Compared area", "role": "outline"})]}
    result = mapview.export_report(payload, title="Outline", out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse(doc)
    assert "role-outline" in doc
    assert ".shape-polygon.role-outline { fill: none;" in doc
    data = _embedded_data(doc)
    assert data["shapes"][0]["role"] == "outline"


def test_unknown_role_falls_back_to_default_style_silently(tmp_path):
    payload = {"features": [_polygon_feature({"name": "Mystery", "role": "sparkle"})]}
    result = mapview.export_report(payload, title="Unknown role", out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse(doc)
    assert "role-sparkle" not in doc
    data = _embedded_data(doc)
    # Not carried onto the render entry at all — same envelope as no role.
    assert "role" not in data["shapes"][0]
    assert result["features_rendered"] == 1
    assert result["skipped_features"] == 0


def test_undecorated_shape_is_unchanged_by_role_label_callout_support(tmp_path):
    """No regression (#368): a shape with no role/label/callout still
    serializes to exactly today's shape dict and default styling."""
    payload = {"features": [_polygon_feature({"name": "Plain park"})]}
    result = mapview.export_report(payload, title="Plain", out_dir=tmp_path)
    assert set(result) == {"path", "bytes", "features_rendered", "skipped_features"}
    doc = Path(result["path"]).read_text(encoding="utf-8")
    data = _embedded_data(doc)
    assert len(data["shapes"]) == 1
    assert data["shapes"][0] == {
        "kind": "polygon",
        "name": "Plain park",
        "props": {"name": "Plain park"},
        "rings": [_OUTER_RING],
    }
    assert "role-" not in doc.split("<style>", 1)[1].split("</style>", 1)[0].replace(
        ".role-shed", ""
    ).replace(".role-outline", "")


def test_shape_label_and_callout_rendered_as_data_not_html(tmp_path):
    payload = {
        "features": [
            _polygon_feature(
                {
                    "name": "Walkshed",
                    "role": "shed",
                    "label": "15-min shed",
                    "callout": "Covers the whole block",
                }
            )
        ]
    }
    result = mapview.export_report(payload, title="Labeled shed", out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse(doc)
    data = _embedded_data(doc)
    assert data["shapes"][0]["label"] == "15-min shed"
    assert data["shapes"][0]["callout"] == "Covers the whole block"
    # Rendered via textContent in JS (same convention as marker names, #34),
    # not spliced into server-rendered HTML markup.
    assert "shape-label-name" in doc
    assert "shape-label-callout" in doc


def test_shape_label_and_callout_cannot_break_out_of_script_tag(tmp_path):
    payload = {
        "features": [
            _polygon_feature(
                {
                    "name": "Danger",
                    "label": "</script><script>alert(1)</script>",
                    "callout": "</script><script>alert(2)</script>",
                }
            )
        ]
    }
    result = mapview.export_report(payload, title="XSS shape", out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    assert "</script><script>alert(1)</script>" not in doc
    assert "</script><script>alert(2)</script>" not in doc
    data = _embedded_data(doc)
    assert "<script>" in data["shapes"][0]["label"]  # carried as data, not stripped


def test_shape_label_and_callout_truncated_at_caps(tmp_path):
    long_label = "x" * 60
    long_callout = "y" * 120
    payload = {
        "features": [
            _polygon_feature(
                {"name": "Shed A", "role": "shed", "label": long_label, "callout": long_callout}
            )
        ]
    }
    result = mapview.export_report(payload, title="Cap test", out_dir=tmp_path)
    doc = Path(result["path"]).read_text(encoding="utf-8")
    data = _embedded_data(doc)
    label = data["shapes"][0]["label"]
    callout = data["shapes"][0]["callout"]
    assert len(label) == mapview.SHAPE_LABEL_MAX_CHARS + 1  # +1 for the ellipsis
    assert label.endswith("…")
    assert long_label not in doc
    assert len(callout) == mapview.SHAPE_CALLOUT_MAX_CHARS + 1
    assert callout.endswith("…")
    assert long_callout not in doc


def test_draw_order_sheds_render_before_outlines_and_points(tmp_path):
    doc = mapview.render_html([], title="Order")
    shapes_idx = doc.index('id="shapes"')
    labels_idx = doc.index('id="labels"')
    markers_idx = doc.index('id="markers"')
    # Shapes under labels under markers: label chips sit above shape fills,
    # and markers stay last so neither a fill nor an opaque chip ever covers
    # a marker dot.
    assert shapes_idx < labels_idx < markers_idx
    # Within shapes, sheds render before outlines so a soft translucent fill
    # never sits over a strong-edged boundary.
    assert "shedShapes.concat(otherShapes, outlineShapes)" in doc


def test_callout_text_wraps_to_chip_width_instead_of_overflowing(tmp_path):
    # SHAPE_CALLOUT_MAX_CHARS (~80) exceeds what the 260px-capped chip can
    # hold on one line (~43 chars at the 5.6px/char estimate), so the JS
    # wraps the callout into chip-width lines rather than overflowing.
    doc = mapview.render_html([], title="Wrap")
    assert "var CALLOUT_WRAP_CHARS = 43;" in doc
    assert "wrapCallout(s.callout)" in doc
    # The wrap width stays consistent with the chip's width formula.
    assert 43 * 5.6 + 16 <= 260
    assert (43 + 1) * 5.6 + 16 > 260


def test_isochrone_payload_carries_role_label_and_callout(tmp_path):
    """#371: role/label/callout must survive _shape_from_isochrone_result's
    props rebuild for the exact payload the feature was designed for."""
    payload = {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "minutes": 15,
        "mode": "walk",
        "role": "shed",
        "label": "15-min walkshed",
        "callout": "x" * 120,  # still truncated downstream like any shape
        "polygon": {"type": "Polygon", "coordinates": [_OUTER_RING]},
        "stats": {"area_km2": 0.62},
    }
    result = mapview.export_report(payload, title="Iso shed", out_dir=tmp_path)
    assert result["features_rendered"] == 1
    doc = Path(result["path"]).read_text(encoding="utf-8")
    _parse(doc)
    data = _embedded_data(doc)
    shape = data["shapes"][0]
    assert shape["role"] == "shed"
    assert shape["label"] == "15-min walkshed"
    assert len(shape["callout"]) == mapview.SHAPE_CALLOUT_MAX_CHARS + 1
    assert shape["callout"].endswith("…")
    # Stats/params flattening still works alongside the annotations.
    assert shape["props"]["area_km2"] == 0.62
    assert shape["props"]["minutes"] == 15


def test_vertex_cap_still_applies_to_labeled_shapes(monkeypatch, tmp_path):
    monkeypatch.setattr(mapview, "MAX_RENDER_VERTICES", 4)
    payload = {
        "features": [
            _polygon_feature(
                {
                    "name": "Big shed",
                    "role": "shed",
                    "label": "Shed",
                    "callout": "Reachable in 15 min",
                }
            )
        ]
    }
    result = mapview.write_artifact(payload, title="Cap", out_dir=tmp_path)
    assert result["truncated"] is True
    assert result["features_rendered"] == 0
    assert result["skipped_features"] == 1


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
