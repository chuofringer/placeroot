"""Tests for the travel_time_matrix tool (#360): routed times/distances between
every origin and destination, over the offline transportation fixture.

Uses the same 20x20 street grid (100m spacing) as test_route.py/
test_optimize_route.py — see scripts/build_routing_fixture.py and
tests/_routing_fixture.py. The whole fixture (~1900m across) comfortably
fits inside every mode's shared-graph extraction cap, so any pair of grid
points exercises the shared-graph path; a point far outside the grid is
used to force the per-pair fallback.

The fixture's "river" is crossed only by a footway bridge (column 9->10 at
row 10), which drive mode excludes, giving a genuinely unroutable pair to
check the null-cell/"unroutable" folding against real fixture data rather
than a mock.
"""

from placeroot import routing, server

from ._routing_fixture import build_routing_fixture as fx

# A point far outside the fixture's ~1900m grid (well past any mode's
# straight-line cap), forcing travel_time_matrix's per-pair fallback.
FAR_POINT = (40.90, -73.99)

# Opposite banks of the fixture's river; drive mode's only crossing is a
# footway bridge, which it excludes.
WEST_STOP = fx.node_latlon(5, 10)
EAST_STOP = fx.node_latlon(14, 10)
WEST_STOP_2 = fx.node_latlon(5, 12)


def _as_dicts(points):
    return [{"lat": lat, "lon": lon} for lat, lon in points]


# --- shared-graph correctness ---------------------------------------------


def test_matrix_values_match_individual_route_calls():
    origins = [fx.node_latlon(2, 2), fx.node_latlon(2, 5)]
    destinations = [fx.node_latlon(8, 2), fx.node_latlon(8, 5)]
    result = server.travel_time_matrix(
        origins=_as_dicts(origins), destinations=_as_dicts(destinations), mode="walk"
    )
    elements = {(e["origin_idx"], e["dest_idx"]): e for e in result["elements"]}
    assert len(elements) == 4

    for oi, (olat, olon) in enumerate(origins):
        for di, (dlat, dlon) in enumerate(destinations):
            expected = routing.route(olat, olon, dlat, dlon, mode="walk")
            e = elements[(oi, di)]
            assert e["distance_m"] == expected["distance_m"]
            assert e["duration_min"] == round(expected["duration_s"] / 60.0, 2)
            assert "note" not in e


def test_origin_major_ordering():
    origins = [fx.node_latlon(2, 2), fx.node_latlon(2, 5), fx.node_latlon(2, 8)]
    destinations = [fx.node_latlon(8, 2), fx.node_latlon(8, 5)]
    result = server.travel_time_matrix(
        origins=_as_dicts(origins), destinations=_as_dicts(destinations), mode="walk"
    )
    expected_order = [(oi, di) for oi in range(3) for di in range(2)]
    assert [(e["origin_idx"], e["dest_idx"]) for e in result["elements"]] == expected_order


def test_durations_note_present():
    point = fx.node_latlon(2, 2)
    result = server.travel_time_matrix(
        origins=_as_dicts([point]), destinations=_as_dicts([fx.node_latlon(5, 5)]), mode="walk"
    )
    assert "durations_note" in result
    assert "not live traffic" in result["durations_note"]
    assert result["mode"] == "walk"


# --- shared graph vs. per-pair fallback ------------------------------------


def test_close_points_use_the_shared_graph_not_per_pair_route(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("route() must not be called when one shared graph covers everything")

    monkeypatch.setattr(routing, "route", boom)

    origins = [fx.node_latlon(2, 2), fx.node_latlon(2, 5)]
    destinations = [fx.node_latlon(8, 2), fx.node_latlon(2, 8)]
    result = server.travel_time_matrix(
        origins=_as_dicts(origins), destinations=_as_dicts(destinations), mode="walk"
    )
    assert "error" not in result
    assert len(result["elements"]) == 4
    for e in result["elements"]:
        assert e["distance_m"] is not None


def test_far_apart_points_fall_back_to_per_pair_route(monkeypatch):
    calls = []
    original_route = routing.route

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original_route(*args, **kwargs)

    monkeypatch.setattr(routing, "route", spy)

    origins = [fx.node_latlon(2, 2)]
    destinations = [fx.node_latlon(5, 5), FAR_POINT]
    result = server.travel_time_matrix(
        origins=_as_dicts(origins), destinations=_as_dicts(destinations), mode="walk"
    )
    # One route() call per (origin, destination) pair proves the fallback
    # loop ran rather than a single shared-graph pass.
    assert len(calls) == 2

    elements = {(e["origin_idx"], e["dest_idx"]): e for e in result["elements"]}
    near = elements[(0, 0)]
    assert near["distance_m"] is not None
    assert "note" not in near

    far = elements[(0, 1)]
    assert far["duration_min"] is None
    assert far["distance_m"] is None
    assert far["note"] == "unroutable"


# --- unroutable pairs -------------------------------------------------------


def test_unroutable_pair_gets_null_cells_others_stay_intact():
    result = server.travel_time_matrix(
        origins=_as_dicts([WEST_STOP]),
        destinations=_as_dicts([EAST_STOP, WEST_STOP_2]),
        mode="drive",
    )
    elements = {(e["origin_idx"], e["dest_idx"]): e for e in result["elements"]}

    across_river = elements[(0, 0)]
    assert across_river["duration_min"] is None
    assert across_river["distance_m"] is None
    assert across_river["note"] == "unroutable"

    same_bank = elements[(0, 1)]
    assert same_bank["duration_min"] is not None
    assert same_bank["distance_m"] is not None
    assert "note" not in same_bank
    assert "note" not in result


# --- validation --------------------------------------------------------------


def test_over_cap_origins():
    origins = [{"lat": 0.0, "lon": 0.0}] * 6
    result = server.travel_time_matrix(origins=origins, destinations=[{"lat": 0.0, "lon": 0.0}])
    assert result["error"] == "bad_request"
    assert "elements" not in result
    assert result["detail"] == "origins accepts at most 5 points, got 6"


def test_over_cap_destinations():
    destinations = [{"lat": 0.0, "lon": 0.0}] * 6
    result = server.travel_time_matrix(
        origins=[{"lat": 0.0, "lon": 0.0}], destinations=destinations
    )
    assert result["error"] == "bad_request"
    assert result["detail"] == "destinations accepts at most 5 points, got 6"


def test_over_cap_both():
    origins = [{"lat": 0.0, "lon": 0.0}] * 7
    destinations = [{"lat": 0.0, "lon": 0.0}] * 8
    result = server.travel_time_matrix(origins=origins, destinations=destinations)
    assert result["error"] == "bad_request"
    assert result["detail"] == (
        "origins and destinations each accept at most 5 points, got 7 origins and 8 destinations"
    )


def test_malformed_point():
    result = server.travel_time_matrix(
        origins=[{"lon": -97.7431}], destinations=[{"lat": 0.0, "lon": 0.0}]
    )
    assert result["error"] == "bad_request"


def test_invalid_coord():
    result = server.travel_time_matrix(
        origins=[{"lat": 95.0, "lon": 0.0}], destinations=[{"lat": 0.0, "lon": 0.0}]
    )
    assert result["error"] == "bad_request"
    assert "origins[0]" in result["detail"]


def test_empty_origins():
    result = server.travel_time_matrix(origins=[], destinations=[{"lat": 0.0, "lon": 0.0}])
    assert result == {"elements": []}


def test_empty_destinations():
    result = server.travel_time_matrix(origins=[{"lat": 0.0, "lon": 0.0}], destinations=[])
    assert result == {"elements": []}


def test_bad_mode():
    point = {"lat": 0.0, "lon": 0.0}
    result = server.travel_time_matrix(origins=[point], destinations=[point], mode="teleport")
    assert result["error"] == "bad_request"
    assert "teleport" in result["detail"]
