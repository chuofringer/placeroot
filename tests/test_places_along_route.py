"""Tests for routing.places_along_route()/server.places_along_route() — issue #171.

Corridor search reuses the same offline 20x20 street-grid transportation
fixture as test_route.py (see scripts/build_routing_fixture.py), but the
committed places fixture sits ~9km away from that grid, so this file builds
its own small places fixture positioned against the grid's geometry — every
place at a known metre offset from a known path node, so the corridor
filter can be graded against exact distances rather than "it returned
something".

The route under test is node (2, 2) -> node (2, 5): a straight 300m
vertical run up column i=2 through nodes (2,2), (2,3), (2,4), (2,5), with
no diagonal or motorway shortcut in play (same reasoning as test_route.py's
pair).
"""

import duckdb
import pytest

from placeroot import overture, routing, server

from ._routing_fixture import build_routing_fixture as fx

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)

# Places, each an exact (distance_m, bearing_deg) offset from a path node.
# East is bearing 90, north is 0 — the route runs north, so an eastward
# offset is a clean perpendicular distance to the path.
NEAR_START_M = 60.0  # off node (2, 2): along_m ~= 0
MID_M = 80.0  # off node (2, 4): along_m ~= 200
NEAR_END_M = 120.0  # off node (2, 5): along_m ~= 300
MAX_DETOUR_M = 200.0

# Beyond the corridor but INSIDE its bounding box: 150m east and 190m north
# of the route's last node, so both offsets are under the box's
# max_detour_m padding while the true distance to the nearest path node
# (~242m) is not. Only the per-place corridor test can exclude this one —
# the bbox prefilter alone lets it through.
BEYOND_EAST_M = 150.0
BEYOND_NORTH_M = 190.0


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


def _offset(lat, lon, distance_m, bearing_deg):
    return fx._offset(lat, lon, distance_m, bearing_deg)


NEAR_START_LATLON = _offset(FROM_LAT, FROM_LON, NEAR_START_M, 90)
MID_LATLON = _offset(*fx.node_latlon(2, 4), MID_M, 90)
NEAR_END_LATLON = _offset(TO_LAT, TO_LON, NEAR_END_M, 90)
_beyond_east = _offset(TO_LAT, TO_LON, BEYOND_EAST_M, 90)
BEYOND_LATLON = _offset(*_beyond_east, BEYOND_NORTH_M, 0)
# Far outside the corridor bbox entirely, at the far corner of the grid.
FAR_LATLON = fx.node_latlon(18, 18)


@pytest.fixture
def corridor_places(tmp_path):
    """Point the places theme at a fixture built around the routing grid."""
    con = duckdb.connect()
    rows = [
        _place_row("p-start", "Start Deli", *NEAR_START_LATLON),
        _place_row("p-mid", "Midway Coffee", *MID_LATLON,
                   category="coffee_shop", basic_category="coffee_shop"),
        _place_row("p-end", "Journey's End Bar", *NEAR_END_LATLON),
        _place_row("p-beyond", "Off Corridor Diner", *BEYOND_LATLON),
        _place_row("p-far", "Far Corner Shop", *FAR_LATLON),
    ]
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
    con.executemany(
        "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    places_path = tmp_path / "corridor_places.parquet"
    con.execute(f"COPY places TO '{places_path}' (FORMAT PARQUET)")
    overture.set_data_path(str(places_path))
    yield


def _corridor(**kwargs):
    params = {"mode": "walk", "max_detour_m": MAX_DETOUR_M}
    params.update(kwargs)
    return routing.places_along_route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, **params)


def test_returns_corridor_places_in_route_order(corridor_places):
    result = _corridor()
    names = [r["name"] for r in result["results"]]
    assert names == ["Start Deli", "Midway Coffee", "Journey's End Bar"]
    assert [r["along_m"] for r in result["results"]] == sorted(
        r["along_m"] for r in result["results"]
    )


def test_detour_and_along_match_the_fixture_geometry(corridor_places):
    """detour_m is twice the straight-line distance to the nearest path node,
    and along_m is the route distance at that node — both checked against the
    exact offsets the fixture places were built with."""
    by_name = {r["name"]: r for r in _corridor()["results"]}

    assert by_name["Start Deli"]["detour_m"] == pytest.approx(2 * NEAR_START_M, abs=1.0)
    assert by_name["Start Deli"]["along_m"] == pytest.approx(0.0, abs=1.0)

    assert by_name["Midway Coffee"]["detour_m"] == pytest.approx(2 * MID_M, abs=1.0)
    # Node (2, 4) is two 100m grid edges along the route from node (2, 2).
    assert by_name["Midway Coffee"]["along_m"] == pytest.approx(200.0, rel=0.02)

    assert by_name["Journey's End Bar"]["detour_m"] == pytest.approx(2 * NEAR_END_M, abs=1.0)
    assert by_name["Journey's End Bar"]["along_m"] == pytest.approx(
        _corridor()["route"]["distance_m"], rel=0.02
    )


def test_place_beyond_max_detour_is_excluded_though_inside_the_bbox(corridor_places):
    """The off-corridor diner sits inside the corridor's padded bounding box
    (both its east and north offsets are under max_detour_m) but ~242m from
    the nearest path node — so only the per-place distance test can drop it.
    Widening max_detour_m past that true distance brings it back, proving the
    exclusion is the corridor filter and not the bbox."""
    nearest_m = routing._haversine_m(*BEYOND_LATLON, TO_LAT, TO_LON)
    assert MAX_DETOUR_M < nearest_m < MAX_DETOUR_M + BEYOND_NORTH_M  # inside the bbox pad

    names = {r["name"] for r in _corridor()["results"]}
    assert "Off Corridor Diner" not in names

    widened = {r["name"] for r in _corridor(max_detour_m=nearest_m + 5)["results"]}
    assert "Off Corridor Diner" in widened


def test_place_far_outside_the_corridor_is_excluded(corridor_places):
    """The far-corner shop is ~2km off the route: excluded by the bbox
    prefilter at a generous 1km corridor, and admitted only once
    max_detour_m is widened past its true distance."""
    nearest_m = min(
        routing._haversine_m(*FAR_LATLON, *fx.node_latlon(2, j)) for j in (2, 3, 4, 5)
    )
    assert nearest_m > 1000.0
    assert "Far Corner Shop" not in {r["name"] for r in _corridor(max_detour_m=1000)["results"]}
    assert "Far Corner Shop" in {
        r["name"] for r in _corridor(max_detour_m=nearest_m + 5)["results"]
    }


def test_category_filter_composes(corridor_places):
    result = _corridor(category="coffee_shop")
    assert [r["name"] for r in result["results"]] == ["Midway Coffee"]


def test_name_filter_composes(corridor_places):
    result = _corridor(name="Deli")
    assert [r["name"] for r in result["results"]] == ["Start Deli"]


def test_limit_caps_results_keeping_route_order(corridor_places):
    result = _corridor(limit=2)
    assert [r["name"] for r in result["results"]] == ["Start Deli", "Midway Coffee"]


def test_route_block_matches_the_route_tool(corridor_places):
    result = _corridor()
    plain = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert result["route"] == {
        "distance_m": plain["distance_m"],
        "duration_s": plain["duration_s"],
        "mode": "walk",
    }


@pytest.mark.parametrize("mode", ["walk", "cycle", "drive"])
def test_every_mode_answers(corridor_places, mode):
    result = routing.places_along_route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode=mode, max_detour_m=MAX_DETOUR_M
    )
    assert [r["name"] for r in result["results"]] == [
        "Start Deli", "Midway Coffee", "Journey's End Bar"
    ]
    assert result["route"]["mode"] == mode
    assert result["route"]["distance_m"] > 0


def test_rows_keep_the_find_places_shape(corridor_places):
    row = _corridor()["results"][0]
    for field in (
        "id", "name", "category", "basic_category", "operating_status",
        "confidence", "brand", "has_website", "has_phone", "lat", "lon",
    ):
        assert field in row
    assert row["detour_m"] >= 0 and row["along_m"] >= 0


def test_invalid_max_detour_raises_value_error(corridor_places):
    with pytest.raises(ValueError):
        _corridor(max_detour_m=0)
    with pytest.raises(ValueError):
        _corridor(max_detour_m=-100)
    with pytest.raises(ValueError):
        _corridor(max_detour_m=routing.CORRIDOR_MAX_DETOUR_M + 1)


def test_route_too_long_propagates(corridor_places):
    with pytest.raises(routing.RouteTooLong):
        routing.places_along_route(0.0, 0.0, 10.0, 10.0, mode="walk")


def test_unsupported_mode_propagates(corridor_places):
    with pytest.raises(routing.UnsupportedMode):
        _corridor(mode="teleport")


def test_no_route_result_is_returned_when_nothing_connects(corridor_places, monkeypatch):
    """A disconnected pair yields route()'s own structured no_route answer
    rather than a bare empty result set."""
    monkeypatch.setattr(
        routing, "_shortest_path", lambda *a, **k: (routing.Graph(), None)
    )
    result = _corridor()
    assert result["error"] == "no_route"
    assert result["mode"] == "walk"


def test_truncated_flag_when_candidate_cap_is_hit(corridor_places, monkeypatch):
    monkeypatch.setattr(
        overture, "find_places_in_bbox", lambda *a, **k: ([], True)
    )
    result = _corridor()
    assert result["truncated"] is True
    assert "narrow" in result["note"]


def test_truncated_flag_when_graph_is_truncated(corridor_places, monkeypatch):
    real = routing._shortest_path

    def truncated_graph(*args, **kwargs):
        graph, found = real(*args, **kwargs)
        graph.truncated = True
        return graph, found

    monkeypatch.setattr(routing, "_shortest_path", truncated_graph)
    result = _corridor()
    assert result["truncated"] is True
    assert "size cap" in result["note"]


# --- path tracking ----------------------------------------------------------


def test_dijkstra_path_to_target_returns_the_node_chain():
    graph = routing._get_or_build_graph(
        *routing._midpoint(FROM_LAT, FROM_LON, TO_LAT, TO_LON), 800.0, "walk", None
    )
    source = routing.snap_to_graph(graph, FROM_LAT, FROM_LON)
    target = routing.snap_to_graph(graph, TO_LAT, TO_LON)
    duration_s, distance_m, path = routing._dijkstra_path_to_target(
        graph, source, target, routing.DEFAULT_SPEED_M_S
    )
    assert [node for node, _along in path] == [
        fx.node_id(2, 2), fx.node_id(2, 3), fx.node_id(2, 4), fx.node_id(2, 5)
    ]
    alongs = [along for _node, along in path]
    assert alongs[0] == 0.0
    assert alongs == sorted(alongs)
    assert alongs[-1] == pytest.approx(distance_m)
    assert duration_s > 0


def test_dijkstra_to_target_still_returns_only_totals():
    """route()'s existing caller keeps its 2-tuple contract after the path
    variant was factored out underneath it."""
    graph = routing._get_or_build_graph(
        *routing._midpoint(FROM_LAT, FROM_LON, TO_LAT, TO_LON), 800.0, "walk", None
    )
    source = routing.snap_to_graph(graph, FROM_LAT, FROM_LON)
    target = routing.snap_to_graph(graph, TO_LAT, TO_LON)
    assert len(routing._dijkstra_to_target(graph, source, target, 1.4)) == 2


def test_subsample_path_keeps_both_ends_and_respects_the_cap():
    path = [(f"n{i}", float(i)) for i in range(100)]
    sampled = routing._subsample_path(path, 10)
    assert len(sampled) <= 10
    assert sampled[0] == path[0]
    assert sampled[-1] == path[-1]
    assert routing._subsample_path(path, 500) == path


# --- server tool ------------------------------------------------------------


def test_server_tool_returns_results_and_route(corridor_places):
    result = server.places_along_route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", max_detour_m=MAX_DETOUR_M
    )
    assert [r["name"] for r in result["results"]] == [
        "Start Deli", "Midway Coffee", "Journey's End Bar"
    ]
    assert result["route"]["mode"] == "walk"
    assert result["route"]["distance_m"] > 0


def test_server_tool_rejects_bad_coordinates(corridor_places):
    for args in (
        (91.0, FROM_LON, TO_LAT, TO_LON),
        (FROM_LAT, FROM_LON, TO_LAT, 181.0),
        (float("nan"), FROM_LON, TO_LAT, TO_LON),
    ):
        result = server.places_along_route(*args)
        assert result["error"] == "bad_request"


def test_server_tool_rejects_bad_max_detour(corridor_places):
    result = server.places_along_route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, max_detour_m=0
    )
    assert result["error"] == "bad_request"
    over_cap = server.places_along_route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON,
        max_detour_m=routing.CORRIDOR_MAX_DETOUR_M * 2,
    )
    assert over_cap["error"] == "bad_request"
    assert "cap" in over_cap["detail"]


def test_server_tool_route_too_long(corridor_places):
    result = server.places_along_route(0.0, 0.0, 10.0, 10.0, mode="walk")
    assert result["error"] == "route_too_long"
    assert result["max_distance_m"] == pytest.approx(
        routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"]
    )


def test_server_tool_unsupported_mode(corridor_places):
    result = server.places_along_route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="teleport"
    )
    assert result["error"] == "unsupported_mode"
    assert "walk" in result["supported"]


def test_server_tool_no_graph_nearby(corridor_places):
    """Two points inside the mode's straight-line cap but well away from the
    fixture grid — no usable street graph, so the route error taxonomy's
    no_graph_nearby comes through unchanged."""
    result = server.places_along_route(0.0, 0.0, 0.01, 0.01, mode="walk")
    assert result["error"] == "no_graph_nearby"


def test_server_tool_category_hint_when_nothing_matches(corridor_places):
    result = server.places_along_route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", category="nonexistent_category"
    )
    assert result["results"] == []
    assert "search_categories" in result["note"]
