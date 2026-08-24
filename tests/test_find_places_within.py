"""Tests for find_places' `within` reachability filter (roadmap §4.2/§6E).

The committed routing fixture (tests/_routing_fixture.py) and the committed
places fixture live at deliberately different coordinates (see
build_routing_fixture.py's ORIGIN_LAT/LON docstring), so an end-to-end test
against a REAL isochrone shed isn't possible offline. Instead these tests
monkeypatch routing.isochrone/isochrone_graph_is_cached to return a small,
known GeoJSON polygon and exercise the real thing everywhere else: real SQL
filtering (ST_Contains) over the real places fixture, real cursor pagination,
real confirm-gate wiring. Only the shed itself is synthetic.

BOX_POLYGON is a tiny box (~3m x 3m) built to contain exactly the two
nearest fixture places to CENTER_LAT/CENTER_LON ("Cluster Place 131" and
"Cluster Place 041", 8-10m out) and exclude every other fixture place,
including the next-nearest one at ~30m. WIDE_BOX_POLYGON is a larger box
covering the ten nearest fixture places (up to ~70m out), for the
pagination test.
"""

import pytest

from placeroot import routing, server

from .conftest import CENTER_LAT, CENTER_LON
from .test_find_places_in_area import (
    DIV_NOTCH,
    INSIDE_LAT,
    INSIDE_LON,
    polygon_fixtures,  # noqa: F401
)

# The two nearest fixture places to CENTER_LAT/CENTER_LON (see module
# docstring); everything else is >=20m further out.
NEAR_A_ID = "b40d5be4b27961c8932635890b248bc0"  # "Cluster Place 131", ~8m out
NEAR_A_NAME = "Cluster Place 131"
NEAR_B_NAME = "Cluster Place 041"


def _box_polygon(latmin, lonmin, latmax, lonmax):
    return {
        "type": "Polygon",
        "coordinates": [[
            [lonmin, latmin], [lonmax, latmin],
            [lonmax, latmax], [lonmin, latmax], [lonmin, latmin],
        ]],
    }


BOX_POLYGON = _box_polygon(40.699885533, -73.90005836, 40.699960785, -73.89999151)
# Covers the ten nearest fixture places to CENTER (up to ~72m out).
WIDE_BOX_POLYGON = _box_polygon(40.6990, -73.9010, 40.7010, -73.8985)


def _fake_isochrone(polygon):
    calls = []

    def fake(lat, lon, minutes=15, mode="walk", **kw):
        calls.append({"lat": lat, "lon": lon, "minutes": minutes, "mode": mode})
        return {
            "center": {"lat": lat, "lon": lon},
            "minutes": minutes,
            "mode": mode,
            "speed_m_s": 1.4,
            "polygon": polygon,
            "polygon_method": "convex_hull",
            "stats": {"reachable_nodes": 2, "max_radius_m": 10.0, "area_km2": 0.0001},
        }

    return fake, calls


@pytest.fixture(autouse=True)
def _cached_graph(monkeypatch):
    """Every test in this module assumes a warm graph unless it overrides
    this itself (the confirm-gate tests do)."""
    monkeypatch.setattr(routing, "isochrone_graph_is_cached", lambda *a, **k: True)


def test_within_filters_to_the_shed_and_notes_it(monkeypatch):
    fake, calls = _fake_isochrone(BOX_POLYGON)
    monkeypatch.setattr(routing, "isochrone", fake)

    baseline = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert len(baseline["results"]) > 2  # plenty of fixture places in the plain radius

    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        within={"minutes": 15, "mode": "walk"}, limit=25,
    )
    assert "error" not in result
    names = {r["name"] for r in result["results"]}
    assert names == {NEAR_A_NAME, NEAR_B_NAME}
    assert "note" in result and "reachability-filtered" in result["note"]
    assert "15" in result["note"] and "walk" in result["note"]
    assert "resolved" not in result  # of defaulted to the search center — nothing to echo

    # of defaulted to the search center.
    assert calls == [{"lat": CENTER_LAT, "lon": CENTER_LON, "minutes": 15.0, "mode": "walk"}]


def test_within_response_shape_is_additive_when_absent():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=200, limit=5)
    assert "resolved" not in result
    assert "reachability" not in (result.get("note") or "")


def test_within_of_as_gers_id_adds_resolved_echo(monkeypatch):
    fake, calls = _fake_isochrone(BOX_POLYGON)
    monkeypatch.setattr(routing, "isochrone", fake)

    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        within={"minutes": 10, "of": NEAR_A_ID}, limit=25,
    )
    assert "error" not in result
    assert result["resolved"]["id"] == NEAR_A_ID
    assert result["resolved"]["name"] == NEAR_A_NAME
    assert result["resolved"]["matched_by"] == "gers_id"
    # isochrone was run from the resolved place's own coordinates, not CENTER.
    assert calls[0]["lat"] != CENTER_LAT or calls[0]["lon"] != CENTER_LON


def test_within_bad_minutes_is_bad_request():
    for bad in (0, -5, 61, "nope"):
        result = server.find_places(
            CENTER_LAT, CENTER_LON, within={"minutes": bad}
        )
        assert result["error"] == "bad_request"


def test_within_unknown_key_is_bad_request():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, within={"minutes": 10, "bogus": 1}
    )
    assert result == {
        "error": "bad_request",
        "detail": "within has unrecognized keys: ['bogus']; accepted: minutes, mode, of",
    }


def test_within_bad_mode_is_unsupported_mode():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, within={"minutes": 10, "mode": "teleport"}
    )
    assert result["error"] == "unsupported_mode"
    assert "supported" in result


def test_within_division_mode_without_of_is_bad_request():
    result = server.find_places(division_id="whatever", within={"minutes": 10})
    assert result == {
        "error": "bad_request",
        "detail": "within needs a point to measure from — pass of",
    }


def test_within_area_mode_without_of_is_bad_request():
    result = server.find_places(area="whatever", within={"minutes": 10})
    assert result["error"] == "bad_request"
    assert "pass of" in result["detail"]


def test_within_cold_graph_without_confirm_is_needs_confirm(monkeypatch):
    monkeypatch.setattr(routing, "isochrone_graph_is_cached", lambda *a, **k: False)
    result = server.find_places(
        CENTER_LAT, CENTER_LON, within={"minutes": 10}
    )
    assert result["error"] == "needs_confirm"
    assert "eta" in result


def test_within_confirm_true_proceeds_on_cold_graph(monkeypatch):
    monkeypatch.setattr(routing, "isochrone_graph_is_cached", lambda *a, **k: False)
    fake, _calls = _fake_isochrone(BOX_POLYGON)
    monkeypatch.setattr(routing, "isochrone", fake)
    result = server.find_places(
        CENTER_LAT, CENTER_LON, within={"minutes": 10}, confirm=True
    )
    assert "error" not in result
    names = {r["name"] for r in result["results"]}
    assert names == {NEAR_A_NAME, NEAR_B_NAME}


def test_within_grouped_by_category_composes(monkeypatch):
    fake, _calls = _fake_isochrone(WIDE_BOX_POLYGON)
    monkeypatch.setattr(routing, "isochrone", fake)
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        categories=["shop", "bakery", "grocery", "restaurant", "cafe"],
        group_by_category=True,
        within={"minutes": 20},
    )
    assert "error" not in result
    every_name = {r["name"] for rows in result["results"].values() for r in rows}
    # Sanity: the grouped scan only ever returns names inside the wide box's
    # rough lat/lon envelope, never CENTER-far ones.
    for rows in result["results"].values():
        for r in rows:
            assert 40.6990 <= r["lat"] <= 40.7010
            assert -73.9010 <= r["lon"] <= -73.8985
    assert every_name  # composed with at least one category and found something


def test_within_cursor_round_trip_reruns_the_same_shed(monkeypatch):
    fake, calls = _fake_isochrone(WIDE_BOX_POLYGON)
    monkeypatch.setattr(routing, "isochrone", fake)

    page1 = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, within={"minutes": 20}, limit=3,
    )
    assert page1.get("truncated") is True
    assert "cursor" in page1
    page1_ids = {r["id"] for r in page1["results"]}
    assert len(page1_ids) == 3

    page2 = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, within={"minutes": 20}, limit=3,
        cursor=page1["cursor"],
    )
    assert "error" not in page2
    page2_ids = {r["id"] for r in page2["results"]}
    assert page2_ids  # non-empty
    assert page1_ids.isdisjoint(page2_ids)
    # The shed was recomputed identically both times (same monkeypatched
    # fake, called with the same of/minutes/mode each page).
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_within_in_params_key_changed_minutes_breaks_cursor(monkeypatch):
    fake, _calls = _fake_isochrone(WIDE_BOX_POLYGON)
    monkeypatch.setattr(routing, "isochrone", fake)

    page1 = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, within={"minutes": 20}, limit=3,
    )
    assert "cursor" in page1

    replay = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, within={"minutes": 21}, limit=3,
        cursor=page1["cursor"],
    )
    assert replay["error"] == "bad_cursor"


def test_within_composes_with_division_id(monkeypatch, polygon_fixtures):  # noqa: F811
    """within(of=explicit point) alongside division_id: a row must be inside
    BOTH the division's polygon and the reachability shed."""
    small_box = _box_polygon(0.0, 0.0, 4.0, 4.0)
    fake, calls = _fake_isochrone(small_box)
    monkeypatch.setattr(routing, "isochrone", fake)

    result = server.find_places(
        division_id=DIV_NOTCH,
        within={"minutes": 10, "of": {"lat": INSIDE_LAT, "lon": INSIDE_LON}},
    )
    assert "error" not in result
    names = {r["name"] for r in result["results"]}
    assert names == {"Inside Place", "Inside Coffee"}
    assert "Inside Bank" not in names  # inside the division, outside the small shed
    assert calls == [
        {"lat": INSIDE_LAT, "lon": INSIDE_LON, "minutes": 10.0, "mode": "walk"}
    ]


def test_within_of_dict_gives_no_resolved_echo(monkeypatch):
    fake, _calls = _fake_isochrone(BOX_POLYGON)
    monkeypatch.setattr(routing, "isochrone", fake)
    result = server.find_places(
        CENTER_LAT, CENTER_LON,
        within={"minutes": 10, "of": {"lat": CENTER_LAT, "lon": CENTER_LON}},
    )
    assert "error" not in result
    assert "resolved" not in result
