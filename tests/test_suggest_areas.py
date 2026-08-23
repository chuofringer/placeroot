"""Tests for server.suggest_areas() and area_suggest.py — issue #350, the
composed tool combining routing.isochrone (#39), divisions.divisions_in_polygon
(#348), and area_score.score_locality (#349).

Fixture-backed cases reuse the offline routing grid fixture (see
scripts/build_routing_fixture.py, same grid test_route.py/test_meeting_point.py
use) and build a small custom divisions (type=division_area + type=division)
fixture positioned against that grid's geometry, plus a small places fixture
positioned near one of those divisions — the same "build test data against
the grid, not the committed places/divisions fixtures ~9km away" pattern
test_meeting_point.py already established.

Layout along the grid's row j=10 (spacing 100m, so 1 grid step = 100m).
Anchors sit on ADJACENT grid nodes, 100m apart:

    ONLY_A (i=2,j=11) --100m-- ANCHOR_A (2,10) --100m-- ANCHOR_B (3,10) --100m-- ONLY_B (4,10)
                                       \\_____________ BOTH (midpoint) ______________/

A 2-minute walk budget reaches only each anchor's 4 immediate grid
neighbors (a 5-node reachable set, under routing.CONCAVE_MIN_NODES — see
that module's own concave_boundary docstring — so the isochrone's drawn
shape is the exact convex hull of those 5 points, not the grid-cell
boundary trace). The trace itself no longer degenerates on a uniformly
spaced grid: #389 taught concave_boundary to coarsen its cells until they
actually join up, so the layout below is chosen for the 5-node convex-hull
path on its own merits, not to dodge that bug. Because the two anchors are only 100m
apart, each one's own position sits ON the other's reachable diamond
(A reaches B directly and vice versa) — the two diamonds overlap
substantially rather than merely touching at a point, and BOTH (placed at
the anchors' midpoint) sits deep inside that overlap. ONLY_A is A's north
neighbor (100m from A, ~141m/2-hop from B — outside B's budget either way);
ONLY_B is B's east neighbor (mirror).
"""

import duckdb
import pytest

from placeroot import area_score, area_suggest, gers, overture, server, tool_profiles

from ._routing_fixture import build_routing_fixture as fx

ANCHOR_A = fx.node_latlon(2, 10)
ANCHOR_B = fx.node_latlon(3, 10)
ONLY_A_NODE = fx.node_latlon(2, 11)
ONLY_B_NODE = fx.node_latlon(4, 10)
BOTH_NODE = ((ANCHOR_A[0] + ANCHOR_B[0]) / 2, (ANCHOR_A[1] + ANCHOR_B[1]) / 2)

WALK_MINUTES = 2

ONLY_A_ID = "gers-div-only-a" + "0" * 14
BOTH_ID = "gers-div-both00" + "0" * 14
ONLY_B_ID = "gers-div-only-b" + "0" * 14


def _anchor(lat, lon, mode="walk", minutes=WALK_MINUTES):
    return {"lat": lat, "lon": lon, "mode": mode, "minutes": minutes}


def _box_wkt(lat, lon, half_deg=0.0003):
    lat_min, lat_max = lat - half_deg, lat + half_deg
    lon_min, lon_max = lon - half_deg, lon + half_deg
    return (
        f"POLYGON(({lon_min} {lat_min}, {lon_min} {lat_max}, "
        f"{lon_max} {lat_max}, {lon_max} {lat_min}, {lon_min} {lat_min}))"
    )


def _write_divisions(tmp_path, rows):
    """rows: list of (id, name, lat, lon). Writes a division_areas (polygon)
    fixture and a matching type=division (point) fixture sharing the same
    ids, the same two-table shape gers.gers_lookup's division_id path and
    divisions_in_polygon's polygon scan each need (see test_area_score.py's
    DOWNTOWN_DIVISION_ID, built the same way by scripts/build_fixture.py +
    scripts/build_geocode_fixture.py)."""
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    area_rows = []
    point_rows = []
    for id_, name, lat, lon in rows:
        (wkb,) = con.execute(f"SELECT ST_AsWKB(ST_GeomFromText('{_box_wkt(lat, lon)}'))").fetchone()
        half = 0.0003
        bbox = {
            "xmin": lon - half, "ymin": lat - half, "xmax": lon + half, "ymax": lat + half,
        }
        area_rows.append((id_, {"primary": name}, "neighborhood", "US", wkb, bbox, id_))
        point_bbox = {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat}
        point_rows.append((
            id_, point_bbox, {"primary": name, "common": None}, "neighborhood",
            "US", None, None, None,
        ))

    con.execute("""
        CREATE TABLE division_areas (
            id VARCHAR,
            names STRUCT("primary" VARCHAR),
            subtype VARCHAR,
            country VARCHAR,
            geometry BLOB,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            division_id VARCHAR
        )
    """)
    con.executemany("INSERT INTO division_areas VALUES (?, ?, ?, ?, ?, ?, ?)", area_rows)
    area_path = tmp_path / "suggest_division_areas.parquet"
    con.execute(f"COPY division_areas TO '{area_path}' (FORMAT PARQUET)")

    con.execute("""
        CREATE TABLE divisions (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            names STRUCT("primary" VARCHAR, common MAP(VARCHAR, VARCHAR)),
            subtype VARCHAR,
            country VARCHAR,
            region VARCHAR,
            hierarchies STRUCT("name" VARCHAR)[][],
            population BIGINT
        )
    """)
    con.executemany("INSERT INTO divisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", point_rows)
    point_path = tmp_path / "suggest_divisions.parquet"
    con.execute(f"COPY divisions TO '{point_path}' (FORMAT PARQUET)")

    overture.set_data_path(str(area_path), theme="divisions")
    overture.set_data_path(str(point_path), theme="divisions", type_="division")


@pytest.fixture
def grid_divisions(tmp_path):
    _write_divisions(
        tmp_path,
        [
            (ONLY_A_ID, "Only A", *ONLY_A_NODE),
            (BOTH_ID, "Both", *BOTH_NODE),
            (ONLY_B_ID, "Only B", *ONLY_B_NODE),
        ],
    )
    yield


def _place_row(id_, name, lat, lon, category):
    return (
        id_,
        {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat},
        {"primary": name},
        {"primary": category, "alternates": []},
        category,
        "open",
        0.9,
        [],
        [],
        [],
        [],
        None,
        [],
    )


@pytest.fixture
def grid_places(tmp_path):
    """A park and a grocery store right at the "Both" division, nothing near
    "Only A"/"Only B" — enough signal for a plausible, differentiated
    ranked shortlist without needing the committed (~9km away) places
    fixture."""
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE places (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            names STRUCT("primary" VARCHAR),
            taxonomy STRUCT("primary" VARCHAR, alternates VARCHAR[]),
            basic_category VARCHAR,
            operating_status VARCHAR,
            confidence DOUBLE,
            addresses STRUCT(
                freeform VARCHAR, locality VARCHAR, region VARCHAR,
                postcode VARCHAR, country VARCHAR
            )[],
            websites VARCHAR[],
            phones VARCHAR[],
            socials VARCHAR[],
            brand STRUCT(names STRUCT("primary" VARCHAR)),
            sources STRUCT(dataset VARCHAR, record_id VARCHAR)[]
        )
    """)
    both_lat, both_lon = BOTH_NODE
    rows = [
        _place_row("p-park", "Both Park", both_lat, both_lon, "park"),
        _place_row("p-grocery", "Both Grocery", both_lat, both_lon, "grocery_store"),
    ]
    con.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    places_path = tmp_path / "suggest_places.parquet"
    con.execute(f"COPY places TO '{places_path}' (FORMAT PARQUET)")
    overture.set_data_path(str(places_path))
    yield


# --- acceptance: one anchor -------------------------------------------


def test_one_anchor_returns_a_plausible_ranked_shortlist_with_reasons(grid_divisions, grid_places):
    result = server.suggest_areas(
        [_anchor(*ANCHOR_A)], ["parks", "groceries"], confirm=True
    )
    assert "error" not in result
    assert result["results"], "expected at least one candidate"
    ids = [r["division_id"] for r in result["results"]]
    assert BOTH_ID in ids
    top = result["results"][0]
    assert top["division_id"] == BOTH_ID
    assert top["overall_score"] > 0
    assert top["reason"]
    assert "score" in top["reason"] or "measurable" in top["reason"]
    labels = {r["label"] for r in top["requirements"]}
    assert labels == {"parks", "groceries"}
    # Only A's shed reaches Only A (100m) too, but never Only B (700m,
    # outside a single 6-minute walk anchor's extraction radius).
    assert ONLY_B_ID not in ids


def test_single_anchor_travel_context_carries_the_anchor_mode_and_budget(
    grid_divisions, grid_places
):
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["parks"], confirm=True)
    top = result["results"][0]
    assert len(top["travel"]) == 1
    leg = top["travel"][0]
    assert leg["anchor_idx"] == 0
    assert leg["mode"] == "walk"
    assert leg["minutes_budget"] == WALK_MINUTES
    assert leg["travel_time_min"] <= WALK_MINUTES


# --- acceptance: two anchors, intersection ------------------------------


def test_two_anchors_exclude_single_anchor_only_areas(grid_divisions):
    result = server.suggest_areas(
        [_anchor(*ANCHOR_A), _anchor(*ANCHOR_B)], ["parks"], limit=10, confirm=True
    )
    assert "error" not in result
    ids = {r["division_id"] for r in result["results"]}
    assert BOTH_ID in ids
    assert ONLY_A_ID not in ids
    assert ONLY_B_ID not in ids


def test_two_anchors_both_candidate_carries_two_travel_legs(grid_divisions):
    result = server.suggest_areas(
        [_anchor(*ANCHOR_A), _anchor(*ANCHOR_B)], ["parks"], confirm=True
    )
    top = next(r for r in result["results"] if r["division_id"] == BOTH_ID)
    assert len(top["travel"]) == 2
    assert {leg["anchor_idx"] for leg in top["travel"]} == {0, 1}


def test_non_overlapping_anchors_return_empty_results_with_a_note(grid_divisions):
    # A budget too small to reach anywhere: a 1-minute walk (60s * 1.4 m/s =
    # 84m) doesn't even reach the nearest grid node 100m away, so each
    # anchor's own "shed" degenerates to a zero-area point at its own
    # position — two distinct points never overlap.
    result = server.suggest_areas(
        [_anchor(*ANCHOR_A, minutes=1), _anchor(*ANCHOR_B, minutes=1)],
        ["parks"],
        confirm=True,
    )
    assert "error" not in result
    assert result["results"] == []
    assert "note" in result
    assert "overlap" in result["note"]


# --- honesty / requirement passthrough ----------------------------------


def test_subjective_requirement_passes_through_as_measurable_false(grid_divisions, grid_places):
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["quiet streets"], confirm=True)
    top = result["results"][0]
    assert top["requirements"][0]["measurable"] is False
    assert top["overall_score"] is None
    assert "not measurable" in top["reason"] or "subjective" in top["reason"].lower() or top[
        "reason"
    ]


def test_honesty_note_present(grid_divisions, grid_places):
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["parks"], confirm=True)
    assert "honesty" in result
    assert result["honesty"] == area_score.HONESTY


def test_result_carries_stable_gers_id_chainable(grid_divisions, grid_places):
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["parks"], confirm=True)
    top = result["results"][0]
    entity = gers.gers_lookup(top["division_id"], near_lat=ANCHOR_A[0], near_lon=ANCHOR_A[1])
    assert entity is not None
    assert entity["name"] == top["name"]


# --- validation -----------------------------------------------------------


def test_zero_anchors_is_bad_request():
    result = server.suggest_areas([], ["parks"])
    assert result["error"] == "bad_request"
    assert "1 and" in result["detail"]


def test_too_many_anchors_is_bad_request():
    anchors = [_anchor(*ANCHOR_A)] * (area_suggest.MAX_ANCHORS + 1)
    result = server.suggest_areas(anchors, ["parks"])
    assert result["error"] == "bad_request"


def test_bad_anchor_mode_is_bad_request():
    result = server.suggest_areas(
        [{"lat": ANCHOR_A[0], "lon": ANCHOR_A[1], "mode": "teleport"}], ["parks"]
    )
    assert result["error"] == "bad_request"
    assert "anchors[0]" in result["detail"]


def test_non_positive_minutes_is_bad_request():
    result = server.suggest_areas([_anchor(*ANCHOR_A, minutes=0)], ["parks"])
    assert result["error"] == "bad_request"
    assert "minutes" in result["detail"]


def test_invalid_anchor_coord_is_bad_request():
    result = server.suggest_areas([{"lat": 999.0, "lon": 0.0}], ["parks"])
    assert result["error"] == "bad_request"
    assert "anchors[0]" in result["detail"]


def test_empty_requirements_is_bad_request():
    result = server.suggest_areas([_anchor(*ANCHOR_A)], [])
    assert result["error"] == "bad_request"


def test_too_many_requirements_is_bad_request():
    result = server.suggest_areas(
        [_anchor(*ANCHOR_A)], ["a"] * (area_score.MAX_REQUIREMENTS + 1)
    )
    assert result["error"] == "bad_request"


def test_limit_is_clamped(grid_divisions, grid_places):
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["parks"], limit=999, confirm=True)
    assert "error" not in result
    assert len(result["results"]) <= area_suggest.MAX_LIMIT


# --- confirm gate -----------------------------------------------------


def test_cold_graph_without_confirm_is_needs_confirm(grid_divisions, monkeypatch):
    monkeypatch.setattr(server.routing, "isochrone_graph_is_cached", lambda *a, **k: False)
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["parks"])
    assert result["error"] == "needs_confirm"
    assert "confirm=true" in result["detail"]


def test_cached_graph_runs_without_confirm(grid_divisions, monkeypatch):
    monkeypatch.setattr(server.routing, "isochrone_graph_is_cached", lambda *a, **k: True)
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["parks"])
    assert "error" not in result


def test_confirm_gate_checks_every_anchor_before_building_any(grid_divisions, monkeypatch):
    """Anchor 0 cached, anchor 1 cold: must ask for confirm rather than
    building anchor 0's graph and then stalling on anchor 1."""
    calls = []

    def fake_cached(lat, lon, minutes, mode, *a, **k):
        calls.append((lat, lon))
        return len(calls) == 1  # only the first anchor checked is "cached"

    monkeypatch.setattr(server.routing, "isochrone_graph_is_cached", fake_cached)
    isochrone_calls = []
    real_isochrone = server.routing.isochrone
    monkeypatch.setattr(
        server.routing, "isochrone",
        lambda *a, **k: (isochrone_calls.append(1), real_isochrone(*a, **k))[1],
    )
    result = server.suggest_areas([_anchor(*ANCHOR_A), _anchor(*ANCHOR_B)], ["parks"])
    assert result["error"] == "needs_confirm"
    assert isochrone_calls == []


# --- error propagation -----------------------------------------------


def test_no_graph_nearby_anchor_is_a_structured_error():
    far = (fx.ORIGIN_LAT + 0.5, fx.ORIGIN_LON + 0.5)
    result = server.suggest_areas([_anchor(*far)], ["parks"], confirm=True)
    assert result["error"] == "no_graph_nearby"
    assert "anchors[0]" in result["detail"]


def test_divisions_upstream_outage_is_a_structured_error(grid_divisions, monkeypatch):
    def outage(*a, **k):
        raise server.divisions.overture.UpstreamUnavailable("divisions scan failed")

    monkeypatch.setattr(server.divisions, "divisions_in_polygon", outage)
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["parks"], confirm=True)
    assert result["error"] == "upstream_unavailable"
    assert result["retry_advised"] is True


def test_area_score_schema_degraded_is_a_structured_error(grid_divisions, monkeypatch):
    def degraded(*a, **k):
        raise server.area_score.overture.SchemaDegraded(["basic_category"])

    monkeypatch.setattr(server.area_score, "score_locality", degraded)
    result = server.suggest_areas([_anchor(*ANCHOR_A)], ["parks"], confirm=True)
    assert result["error"] == "schema_degraded"
    assert result["missing_columns"] == ["basic_category"]


# --- pure helper: area_suggest.intersect_sheds -------------------------


def _square(lat, lon, half_deg):
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - half_deg, lat - half_deg],
            [lon - half_deg, lat + half_deg],
            [lon + half_deg, lat + half_deg],
            [lon + half_deg, lat - half_deg],
            [lon - half_deg, lat - half_deg],
        ]],
    }


def test_intersect_sheds_single_polygon_passthrough():
    poly = _square(40.0, -73.0, 0.01)
    assert area_suggest.intersect_sheds([poly]) == [poly]


def test_intersect_sheds_overlapping_squares_returns_the_overlap():
    a = _square(40.0, -73.0, 0.01)
    b = _square(40.005, -73.0, 0.01)
    parts = area_suggest.intersect_sheds([a, b])
    assert len(parts) == 1
    assert parts[0]["type"] == "Polygon"


def test_intersect_sheds_disjoint_squares_returns_empty():
    a = _square(40.0, -73.0, 0.001)
    b = _square(41.0, -73.0, 0.001)
    assert area_suggest.intersect_sheds([a, b]) == []


def test_build_reason_all_unmeasurable():
    reason = area_suggest.build_reason([{"label": "quiet", "measurable": False}])
    assert "measurable" in reason


def test_build_reason_single_measurable():
    reason = area_suggest.build_reason(
        [{"label": "parks", "measurable": True, "score": 0.8}]
    )
    assert "parks" in reason
    assert "0.8" in reason


def test_build_reason_strongest_and_weakest():
    reason = area_suggest.build_reason([
        {"label": "parks", "measurable": True, "score": 0.9},
        {"label": "groceries", "measurable": True, "score": 0.1},
    ])
    assert "parks" in reason
    assert "groceries" in reason


# --- registration ------------------------------------------------------


def test_suggest_areas_is_in_the_analysis_profile():
    assert "suggest_areas" in tool_profiles.PROFILES["analysis"]
