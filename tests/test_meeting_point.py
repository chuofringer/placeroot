"""Tests for meeting.py/server.meeting_point() — issue #306.

Fixture-backed cases reuse the offline 20x20 street-grid transportation
fixture (see scripts/build_routing_fixture.py, same as test_route.py /
test_optimize_route.py) and, like test_places_along_route.py, build a
small one-off places fixture positioned against that grid's geometry
rather than the committed places fixture (which sits ~9km away).
"""

import duckdb
import pytest

from placeroot import geo, meeting, overture, server, tool_profiles

from ._routing_fixture import build_routing_fixture as fx


def _origins(*points_and_modes):
    return [{"lat": lat, "lon": lon, "mode": mode} for lat, lon, mode in points_and_modes]


# --- pure logic: fairness_key ---------------------------------------------


def test_fairness_key_picks_minimize_max_not_minimize_average():
    # A: everyone waits 10 min. B: one person waits 20, another 2 (lower
    # average than A) -- fairness must still prefer A.
    a = meeting.fairness_key([10.0, 10.0])
    b = meeting.fairness_key([20.0, 2.0])
    assert a < b


def test_fairness_key_tie_breaks_by_spread_then_total():
    # Same max (10) for both -- the one with the smaller max-min spread wins.
    even = meeting.fairness_key([10.0, 10.0])
    uneven = meeting.fairness_key([10.0, 1.0])
    assert even < uneven

    # Same max (10) and same spread (5, from the shared min of 5) -- the
    # smaller total wins. A third element holds min/max/spread fixed while
    # total varies.
    lower_total = meeting.fairness_key([10.0, 5.0, 5.0])
    higher_total = meeting.fairness_key([10.0, 5.0, 7.0])
    assert lower_total < higher_total


def test_fairness_key_empty_times_raises():
    with pytest.raises(ValueError):
        meeting.fairness_key([])


def test_ranking_by_fairness_key_over_synthetic_candidates():
    candidates = [
        {"name": "fair", "times": [12.0, 13.0]},
        {"name": "unfair", "times": [2.0, 25.0]},
        {"name": "best", "times": [8.0, 9.0]},
    ]
    ranked = sorted(candidates, key=lambda c: meeting.fairness_key(c["times"]))
    assert [c["name"] for c in ranked] == ["best", "fair", "unfair"]


# --- pure logic: find_center ----------------------------------------------


def test_find_center_requires_at_least_one_origin():
    with pytest.raises(ValueError):
        meeting.find_center([])


def test_find_center_of_identical_origins_is_that_point():
    lat, lon = 40.70, -73.90
    center_lat, center_lon = meeting.find_center([(lat, lon, "walk"), (lat, lon, "drive")])
    assert center_lat == pytest.approx(lat, abs=1e-6)
    assert center_lon == pytest.approx(lon, abs=1e-6)


def test_find_center_pulls_toward_the_walking_participant():
    """A walk origin implies a much larger travel TIME than a drive origin
    at the same raw distance, so the fairness-seeking center should sit
    closer (in plain distance) to the walker than to the driver, even
    though they are equidistant from the geometric midpoint."""
    lat0, lon0 = 40.70, -73.90
    walk_origin = fx._offset(lat0, lon0, 1000.0, 0)  # 1km due north
    drive_origin = fx._offset(lat0, lon0, 1000.0, 180)  # 1km due south

    center_lat, center_lon = meeting.find_center([(*walk_origin, "walk"), (*drive_origin, "drive")])

    dist_to_walker = geo.haversine_m(center_lat, center_lon, *walk_origin)
    dist_to_driver = geo.haversine_m(center_lat, center_lon, *drive_origin)
    assert dist_to_walker < dist_to_driver


# --- fixture-backed end to end --------------------------------------------

# Two walk origins 1600m apart along column i=2 of the fixture grid; the
# fair meeting point for two same-mode, symmetric origins sits near their
# midpoint, node (2, 10).
ORIGIN_A = fx.node_latlon(2, 2)
ORIGIN_B = fx.node_latlon(2, 18)
MIDPOINT_NODE = fx.node_latlon(2, 10)

# An origin nowhere near the fixture grid -- inside no extraction circle
# any candidate would plausibly use, so routing legs to it always raise
# NoGraphNearby (mirrors test_optimize_route.py's "no street nearby" case).
FAR_FROM_GRID = (fx.ORIGIN_LAT + 0.05, fx.ORIGIN_LON + 0.05)


def _place_row(id_, name, lat, lon, category="shop", basic_category="shop"):
    return (
        id_,
        {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat},
        {"primary": name},
        {"primary": category, "alternates": []},
        basic_category,
        "open",
        0.9,
        [],
        [],
        [],
        [],
        None,
        [],
    )


def _write_places(tmp_path, rows, filename="meeting_places.parquet"):
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
    con.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    places_path = tmp_path / filename
    con.execute(f"COPY places TO '{places_path}' (FORMAT PARQUET)")
    overture.set_data_path(str(places_path))


@pytest.fixture
def meeting_places(tmp_path):
    """Places positioned against the routing grid: one right at the fair
    midpoint, one far off in a corner (outside the search radius)."""
    mid_lat, mid_lon = MIDPOINT_NODE
    far_lat, far_lon = fx.node_latlon(18, 18)
    _write_places(
        tmp_path,
        [
            _place_row(
                "p-mid",
                "Midpoint Cafe",
                mid_lat,
                mid_lon,
                category="coffee_shop",
                basic_category="coffee_shop",
            ),
            _place_row("p-far", "Faraway Diner", far_lat, far_lon),
        ],
    )
    yield


def test_two_origins_rank_the_midpoint_venue_first(meeting_places):
    result = server.meeting_point(_origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")))
    assert "error" not in result
    assert result["candidates"], "expected at least one routable candidate"
    top = result["candidates"][0]
    assert top["id"] == "p-mid"
    assert top["name"] == "Midpoint Cafe"
    assert len(top["per_person"]) == 2
    assert {p["origin_idx"] for p in top["per_person"]} == {0, 1}
    for p in top["per_person"]:
        assert p["mode"] == "walk"
        assert p["travel_time_min"] > 0
        assert p["distance_m"] > 0
    assert top["max_travel_time_min"] == max(p["travel_time_min"] for p in top["per_person"])
    assert top["spread_min"] == pytest.approx(
        max(p["travel_time_min"] for p in top["per_person"])
        - min(p["travel_time_min"] for p in top["per_person"]),
        abs=1e-6,
    )


def test_center_is_returned_and_near_the_grid(meeting_places):
    result = server.meeting_point(_origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")))
    center = result["center"]
    mid_lat, mid_lon = MIDPOINT_NODE
    # Same mode, symmetric origins -> the fair center should land close to
    # the geometric midpoint too.
    assert geo.haversine_m(center["lat"], center["lon"], mid_lat, mid_lon) < 500.0


def test_category_filter_passes_through_to_places_search(meeting_places):
    result = server.meeting_point(
        _origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")), category="coffee_shop"
    )
    assert "error" not in result
    assert all(c["category"] == "coffee_shop" for c in result["candidates"])


def test_category_matching_nothing_returns_empty_candidates_with_a_note(meeting_places):
    result = server.meeting_point(
        _origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")), category="totally_bogus_category"
    )
    assert result["candidates"] == []
    assert "note" in result
    assert "search_categories" in result["note"]


def test_result_carries_no_geometry(meeting_places):
    result = server.meeting_point(_origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")))
    for candidate in result["candidates"]:
        assert set(candidate) <= {
            "id",
            "name",
            "category",
            "lat",
            "lon",
            "per_person",
            "max_travel_time_min",
            "spread_min",
        }


# --- validation ------------------------------------------------------------


def test_one_origin_is_a_bad_request():
    result = server.meeting_point(_origins((*ORIGIN_A, "walk")))
    assert result["error"] == "bad_request"
    assert "2 and 5" in result["detail"]


def test_six_origins_is_a_bad_request():
    pts = _origins(*[(*fx.node_latlon(2, i), "walk") for i in range(6)])
    result = server.meeting_point(pts)
    assert result["error"] == "bad_request"
    assert "2 and 5" in result["detail"]


def test_bad_mode_is_a_bad_request():
    origins = [
        {"lat": ORIGIN_A[0], "lon": ORIGIN_A[1], "mode": "teleport"},
        {"lat": ORIGIN_B[0], "lon": ORIGIN_B[1], "mode": "walk"},
    ]
    result = server.meeting_point(origins)
    assert result["error"] == "bad_request"
    assert "origins[0]" in result["detail"]
    assert "walk" in result["supported"]


def test_missing_mode_defaults_to_walk(meeting_places):
    origins = [
        {"lat": ORIGIN_A[0], "lon": ORIGIN_A[1]},
        {"lat": ORIGIN_B[0], "lon": ORIGIN_B[1]},
    ]
    result = server.meeting_point(origins)
    assert "error" not in result
    for candidate in result["candidates"]:
        assert all(p["mode"] == "walk" for p in candidate["per_person"])


def test_invalid_coord_is_a_structured_error():
    origins = [
        {"lat": 91.0, "lon": 0.0, "mode": "walk"},
        {"lat": ORIGIN_B[0], "lon": ORIGIN_B[1], "mode": "walk"},
    ]
    result = server.meeting_point(origins)
    assert result["error"] == "bad_request"
    assert "origins[0]" in result["detail"]


def test_missing_lat_is_a_bad_request():
    origins = [
        {"lon": ORIGIN_A[1], "mode": "walk"},
        {"lat": ORIGIN_B[0], "lon": ORIGIN_B[1], "mode": "walk"},
    ]
    result = server.meeting_point(origins)
    assert result["error"] == "bad_request"
    assert "origins[0]" in result["detail"]


def test_non_dict_origin_is_a_bad_request():
    result = server.meeting_point([[1, 2], {"lat": ORIGIN_B[0], "lon": ORIGIN_B[1]}])
    assert result["error"] == "bad_request"
    assert "origins[0]" in result["detail"]


def test_limit_is_clamped_between_one_and_five(meeting_places):
    result = server.meeting_point(_origins((*ORIGIN_A, "walk"), (*ORIGIN_B, "walk")), limit=99)
    assert "error" not in result
    assert len(result["candidates"]) <= 5


# --- unroutable handling -----------------------------------------------


def test_origin_far_from_any_graph_excludes_every_candidate_with_a_note(meeting_places):
    result = server.meeting_point(_origins((*ORIGIN_A, "walk"), (*FAR_FROM_GRID, "walk")))
    assert "error" not in result
    assert result["candidates"] == []
    assert "note" in result
    assert "no street graph" in result["note"] or "disconnected" in result["note"]


def test_five_origins_with_no_upstream_exception(meeting_places):
    """Five origins, all on-grid: exercises the full 5-origin x N-candidate
    fan-out without raising."""
    pts = _origins(
        (*fx.node_latlon(2, 2), "walk"),
        (*fx.node_latlon(2, 18), "walk"),
        (*fx.node_latlon(18, 2), "walk"),
        (*fx.node_latlon(10, 10), "walk"),
        (*fx.node_latlon(0, 0), "walk"),
    )
    result = server.meeting_point(pts)
    assert "error" not in result


# --- registration ------------------------------------------------------


def test_meeting_point_is_in_the_routing_profile():
    assert "meeting_point" in tool_profiles.PROFILES["routing"]
