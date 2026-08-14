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


def _write_places(tmp_path, rows, filename="corridor_places.parquet"):
    """Build a one-off places parquet from _place_row tuples and activate it."""
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
    con.executemany(
        "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    places_path = tmp_path / filename
    con.execute(f"COPY places TO '{places_path}' (FORMAT PARQUET)")
    overture.set_data_path(str(places_path))


@pytest.fixture
def corridor_places(tmp_path):
    """Point the places theme at a fixture built around the routing grid."""
    _write_places(tmp_path, [
        _place_row("p-start", "Start Deli", *NEAR_START_LATLON),
        _place_row("p-mid", "Midway Coffee", *MID_LATLON,
                   category="coffee_shop", basic_category="coffee_shop"),
        _place_row("p-end", "Journey's End Bar", *NEAR_END_LATLON),
        _place_row("p-beyond", "Off Corridor Diner", *BEYOND_LATLON),
        _place_row("p-far", "Far Corner Shop", *FAR_LATLON),
    ])
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


def test_limit_samples_across_the_route_rather_than_taking_a_prefix(corridor_places):
    """limit=2 over three corridor places keeps the first and the last, not the
    first two: a prefix would drop the end of the journey silently."""
    result = _corridor(limit=2)
    assert [r["name"] for r in result["results"]] == ["Start Deli", "Journey's End Bar"]
    assert result["truncated"] is True
    assert "even sample" in result["note"]


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


# --- regressions: candidate coverage, truncation, mid-segment distance -------


@pytest.fixture
def alphabetical_decoy_places(tmp_path):
    """A dense cluster of alphabetically-early places at the route's midpoint.

    599 of them, more than overture.BBOX_MAX_CANDIDATES, all packed against
    the middle path node — plus one match at the far end. A single query over
    the whole route's bounding box fills its 500-row cap with "Aaa Shop"
    rows and never sees the cafe at the end at all; the cluster sits at the
    route's centre, so ranking that one query by distance from the centre
    instead would bury the cafe just as thoroughly. Only per-chunk boxes,
    each with their own share of the budget, reach it.
    """
    decoy_lat, decoy_lon = _offset(*fx.node_latlon(2, 4), 30.0, 90)
    rows = [
        _place_row(f"decoy-{i:03d}", f"Aaa Shop {i:03d}",
                   *_offset(decoy_lat, decoy_lon, float(i % 20), 0))
        for i in range(599)
    ]
    rows.append(_place_row("p-zzz", "Zzz Cafe", *_offset(TO_LAT, TO_LON, 20.0, 90),
                           category="coffee_shop", basic_category="coffee_shop"))
    _write_places(tmp_path, rows, "decoy_places.parquet")
    yield


def test_dense_alphabetical_cluster_does_not_hide_places_further_along(
    alphabetical_decoy_places,
):
    """Coverage is spatially uniform, not alphabetical: the 599-strong cluster
    at the start exhausts its own chunk's share of the candidate budget, but
    the chunk covering the route's end still finds the cafe 20m off it."""
    result = _corridor(max_detour_m=50.0, limit=overture.MAX_ROWS)
    names = [r["name"] for r in result["results"]]
    assert "Zzz Cafe" in names
    assert names[-1] == "Zzz Cafe"  # last along the route
    assert sum(1 for n in names if n.startswith("Aaa Shop")) > 0

    # A category filter narrows to it too — the cluster never crowded it out
    # of the candidate set in the first place.
    only_cafes = _corridor(max_detour_m=50.0, category="coffee_shop")
    assert [r["name"] for r in only_cafes["results"]] == ["Zzz Cafe"]


@pytest.fixture
def twelve_delis(tmp_path):
    """12 delis spread evenly along the whole 300m route, 40m east of it."""
    rows = []
    for k in range(12):
        base = _offset(FROM_LAT, FROM_LON, 300.0 * k / 11.0, 0)
        rows.append(_place_row(f"deli-{k:02d}", f"Deli {k:02d}", *_offset(*base, 40.0, 90)))
    _write_places(tmp_path, rows, "deli_places.parquet")
    yield


def test_over_limit_results_span_the_whole_route_and_flag_truncation(twelve_delis):
    """12 matches at limit=10: the answer reaches the end of the route instead
    of stopping two-thirds of the way along, and says it dropped some."""
    result = _corridor(max_detour_m=100.0, limit=10)
    names = [r["name"] for r in result["results"]]

    assert len(names) == 10
    assert names[0] == "Deli 00"
    assert names[-1] == "Deli 11"
    assert names == sorted(names)  # still route order
    assert result["truncated"] is True
    assert "12 places are on the way" in result["note"]
    assert "even sample" in result["note"]

    # The final third of the route is represented, which a rows[:limit]
    # prefix (Deli 00..Deli 09) would not be.
    assert result["results"][-1]["along_m"] == pytest.approx(300.0, abs=5.0)

    # Under the limit, nothing is dropped and nothing is flagged.
    full = _corridor(max_detour_m=100.0, limit=12)
    assert len(full["results"]) == 12
    assert "truncated" not in full


@pytest.fixture
def mid_segment_place(tmp_path):
    """One place 60m from the middle of the route's first 100m segment.

    Its distance to the nearest path *node* is sqrt(50^2 + 60^2) ~= 78m, so a
    node-only corridor test excludes it at max_detour_m=70 even though the
    route passes 60m away.
    """
    midpoint = _offset(FROM_LAT, FROM_LON, 50.0, 0)  # halfway to node (2, 3)
    _write_places(tmp_path, [
        _place_row("p-block", "Mid Block Bakery", *_offset(*midpoint, 60.0, 90)),
    ], "mid_segment_places.parquet")
    yield


def test_place_beside_a_segment_midpoint_counts_as_on_the_way(mid_segment_place):
    """Distance is measured to the route polyline, not to its nodes."""
    place_latlon = _offset(*_offset(FROM_LAT, FROM_LON, 50.0, 0), 60.0, 90)
    nearest_node_m = min(
        routing._haversine_m(*place_latlon, *fx.node_latlon(2, j)) for j in (2, 3, 4, 5)
    )
    assert nearest_node_m == pytest.approx(78.1, abs=1.0)  # would fail a 70m node test

    result = _corridor(max_detour_m=70.0)
    assert [r["name"] for r in result["results"]] == ["Mid Block Bakery"]
    row = result["results"][0]
    assert row["detour_m"] == pytest.approx(2 * 60.0, abs=1.0)
    # along_m is interpolated along the segment, not snapped to an endpoint.
    assert row["along_m"] == pytest.approx(50.0, abs=2.0)


def test_point_to_segment_measures_perpendicular_distance_and_position():
    a_lat, a_lon = FROM_LAT, FROM_LON
    b_lat, b_lon = fx.node_latlon(2, 3)  # 100m due north of a
    p_lat, p_lon = _offset(*_offset(a_lat, a_lon, 25.0, 0), 40.0, 90)

    dist_m, t = routing._point_to_segment_m(p_lat, p_lon, a_lat, a_lon, b_lat, b_lon)
    assert dist_m == pytest.approx(40.0, abs=0.5)
    assert t == pytest.approx(0.25, abs=0.01)

    # Past the far end: clamped to B, so the distance is to B itself.
    beyond = _offset(b_lat, b_lon, 30.0, 0)
    dist_m, t = routing._point_to_segment_m(*beyond, a_lat, a_lon, b_lat, b_lon)
    assert t == 1.0
    assert dist_m == pytest.approx(30.0, abs=0.5)


def test_corridor_bbox_wraps_instead_of_spanning_the_globe_at_the_seam():
    """A path straddling the antimeridian gets a tight box running past +180
    (which overture folds into two in-range boxes), not a whole latitude band."""
    xmin, ymin, xmax, ymax = routing._corridor_bbox(
        [(-17.5, 179.98), (-17.5, -179.98)], 100.0
    )
    assert xmin > 179.0
    assert xmax > 180.0
    assert xmax - xmin < 1.0
    assert ymin < -17.5 < ymax


def test_path_chunks_overlap_so_every_segment_is_covered():
    points = [(0.0, float(i), float(i)) for i in range(41)]
    chunks = routing._path_chunks(points, 20)

    assert len(chunks) == 20
    assert chunks[0][0] == points[0]
    assert chunks[-1][-1] == points[-1]
    # Each chunk ends where the next begins, so no segment falls between two.
    for earlier, later in zip(chunks, chunks[1:]):
        assert earlier[-1] == later[0]
    assert sum(len(c) - 1 for c in chunks) == len(points) - 1

    # Degenerate inputs stay usable rather than producing empty chunks.
    assert routing._path_chunks(points[:1], 20) == [points[:1]]
    assert routing._path_chunks(points[:2], 20) == [points[:2]]


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


def test_subsample_path_keeps_both_ends_and_respects_the_cap():
    path = [(f"n{i}", float(i)) for i in range(100)]
    sampled = routing._subsample_path(path, 10)
    assert len(sampled) <= 10
    assert sampled[0] == path[0]
    assert sampled[-1] == path[-1]
    assert routing._subsample_path(path, 500) == path


def test_sample_evenly_returns_exactly_count_items_spanning_the_input():
    items = list(range(1000))
    for count in (2, 3, 7, 10, 999):
        sampled = routing._sample_evenly(items, count)
        assert len(sampled) == count
        assert sampled[0] == items[0]
        assert sampled[-1] == items[-1]
        assert sampled == sorted(sampled)
    assert routing._sample_evenly(items, 1) == [0]
    assert routing._sample_evenly(items, 0) == []
    assert routing._sample_evenly([1, 2], 5) == [1, 2]


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
    assert result["detail"] == "unsupported mode 'teleport'; supported: ['cycle', 'drive', 'walk']"


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


def test_server_tool_category_hint_keeps_the_truncation_note(corridor_places, monkeypatch):
    """A capped corridor plus a category filter that yields nothing: the
    routing layer's "candidates were dropped before measurement" note must
    survive alongside the category hint, not be clobbered by it — an empty
    list under a hit cap does NOT establish that no places matched, and the
    right advice (narrow) is the opposite of the hint's (widen)."""
    monkeypatch.setattr(overture, "find_places_in_bbox", lambda *a, **k: ([], True))
    result = server.places_along_route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", category="coffee_shop"
    )
    assert result["results"] == []
    assert result["truncated"] is True
    assert "not considered" in result["note"]  # the routing-layer cap note
    assert "search_categories" in result["note"]  # the category hint, appended
