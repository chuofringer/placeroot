"""Explained-map payloads on compare_areas/meeting_point — issue #369.

`map` is built to be splatted straight into render_map:
`server.render_map(**result["map"])`. These tests exercise both
composed-tool builders (mapexplain.py) and the end-to-end path: feed a
real tool's `map` payload into render_map unchanged and assert the legend/
callouts restate the tool's own verdict.
"""

from placeroot import mapexplain, server

from .conftest import CENTER_LAT, CENTER_LON
from .test_meeting_point import ORIGIN_A, ORIGIN_B, _origins, meeting_places  # noqa: F401

AREAS = [{"lat": CENTER_LAT, "lon": CENTER_LON}, {"lat": 78.0, "lon": 15.0}]
GYM_PRIORITY = [{"label": "gyms", "category": "gym", "prefer": "more", "weight": 1}]


# --- compare_areas -----------------------------------------------------


def test_compare_areas_without_priorities_has_no_map():
    result = server.compare_areas(AREAS, radius_m=1000)
    assert "verdict" not in result
    assert "map" not in result


def test_compare_areas_verdict_mode_emits_map_payload():
    result = server.compare_areas(AREAS, radius_m=1000, priorities=GYM_PRIORITY)
    assert "verdict" in result
    assert "map" in result
    map_payload = result["map"]
    assert set(map_payload) == {"result", "legend", "summary"}

    fc = map_payload["result"]
    assert fc["type"] == "FeatureCollection"
    # One pin + one circle outline per area.
    assert len(fc["features"]) == 2 * len(AREAS)
    points = [f for f in fc["features"] if f["geometry"]["type"] == "Point"]
    polygons = [f for f in fc["features"] if f["geometry"]["type"] == "Polygon"]
    assert len(points) == len(AREAS)
    assert len(polygons) == len(AREAS)

    winner_idx = result["verdict"]["winner_idx"]
    assert winner_idx is not None
    winner_points = [p for p in points if p["properties"]["class"] == "winner"]
    assert len(winner_points) == 1
    assert winner_points[0]["properties"]["name"] == f"Area {winner_idx + 1}"

    # Shapes are cheap parametric circles, not a duplicated heavy boundary —
    # every polygon has exactly one ring closed to _CIRCLE_POINTS + 1 coords.
    for poly in polygons:
        rings = poly["geometry"]["coordinates"]
        assert len(rings) == 1
        assert len(rings[0]) == mapexplain._CIRCLE_POINTS + 1
        assert poly["properties"]["role"] == "outline"

    # Legend and summary restate the verdict.
    assert "winner" in map_payload["legend"]
    assert f"Area {winner_idx + 1}" in map_payload["summary"]
    assert "wins" in map_payload["summary"]


def test_compare_areas_map_callouts_restate_scores():
    result = server.compare_areas(AREAS, radius_m=1000, priorities=GYM_PRIORITY)
    winner_idx = result["verdict"]["winner_idx"]
    scores = result["verdict"]["scores"]
    fc = result["map"]["result"]
    polygons = [f for f in fc["features"] if f["geometry"]["type"] == "Polygon"]
    winner_poly = next(p for p in polygons if p["properties"]["label"] == f"Area {winner_idx + 1}")
    assert "winner" in winner_poly["properties"]["callout"]
    assert f"{scores[winner_idx]:g}" in winner_poly["properties"]["callout"]


def test_compare_areas_tie_has_no_winner_class_but_still_emits_map():
    # A "prefer" priority where both areas measure 0 (a bogus category) ties
    # every share at 1.0 -- winner_idx stays None, still a valid map.
    result = server.compare_areas(
        AREAS,
        radius_m=1000,
        priorities=[
            {"label": "bogus", "category": "totally_bogus_category_xyz", "prefer": "more"}
        ],
    )
    assert result["verdict"]["winner_idx"] is None
    map_payload = result["map"]
    points = [f for f in map_payload["result"]["features"] if f["geometry"]["type"] == "Point"]
    assert all(p["properties"]["class"] == "area" for p in points)
    assert "tied" in map_payload["summary"]


# --- meeting_point -------------------------------------------------------


def test_meeting_point_emits_map_payload(meeting_places):  # noqa: F811
    result = server.meeting_point(
        _origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")), confirm=True
    )
    assert result["candidates"]
    assert "map" in result
    map_payload = result["map"]
    assert set(map_payload) == {"result", "legend", "summary"}

    fc = map_payload["result"]
    classes = [f["properties"]["class"] for f in fc["features"]]
    assert classes.count("origin") == 2
    assert classes.count("best_candidate") == 1
    assert classes.count("fair_center") == 1

    best = result["candidates"][0]
    assert best["name"] in map_payload["summary"]
    assert str(best["max_travel_time_min"]) in map_payload["summary"]
    assert "fair_center" in map_payload["legend"]
    assert "best_candidate" in map_payload["legend"]


def test_meeting_point_no_candidates_has_no_map():
    result = server.meeting_point(_origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")))
    assert result["candidates"] == []
    assert "map" not in result


# --- end-to-end: the payload feeds render_map unchanged -------------------


def test_compare_areas_map_feeds_render_map_directly(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_ARTIFACT_DIR", str(tmp_path))
    result = server.compare_areas(AREAS, radius_m=1000, priorities=GYM_PRIORITY)
    winner_idx = result["verdict"]["winner_idx"]

    rendered = server.render_map(**result["map"], title="Compare", inline=True)
    assert rendered["skipped_features"] == 0
    assert rendered["features_rendered"] == len(result["map"]["result"]["features"])
    html = rendered["html"]
    assert "Winner" in html  # legend label
    assert result["map"]["summary"] in html  # verdict callout in the one-pager
    assert f"Area {winner_idx + 1}" in html


def test_meeting_point_map_feeds_render_map_directly(
    meeting_places, tmp_path, monkeypatch  # noqa: F811
):
    monkeypatch.setenv("PLACEROOT_ARTIFACT_DIR", str(tmp_path))
    result = server.meeting_point(
        _origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")), confirm=True
    )

    rendered = server.render_map(**result["map"], title="Meet", inline=True)
    assert rendered["skipped_features"] == 0
    assert rendered["features_rendered"] == len(result["map"]["result"]["features"])
    html = rendered["html"]
    assert "Fairest venue" in html  # legend label
    assert result["map"]["summary"] in html  # verdict restated on the page
    assert result["candidates"][0]["name"] in html
