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

import pytest

from placeroot import preferences, routing, server

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


def test_omitted_mode_uses_stored_preference():
    preferences.update(mode="cycle")
    origins = [fx.node_latlon(2, 2)]
    destinations = [fx.node_latlon(2, 5)]
    result = server.travel_time_matrix(
        origins=_as_dicts(origins), destinations=_as_dicts(destinations)
    )
    assert "error" not in result
    assert result["mode"] == "cycle"


def test_explicit_mode_wins_over_stored_preference():
    preferences.update(mode="cycle")
    origins = [fx.node_latlon(2, 2)]
    destinations = [fx.node_latlon(2, 5)]
    result = server.travel_time_matrix(
        origins=_as_dicts(origins), destinations=_as_dicts(destinations), mode="walk"
    )
    assert "error" not in result
    assert result["mode"] == "walk"


def test_routing_level_empty_lists_return_the_empty_matrix_shape():
    # The library entry point (not just the server wrapper) must answer an
    # empty side with the tool's own empty shape, never a bare ValueError
    # out of a max() over zero pairs.
    point = fx.node_latlon(2, 2)
    for origins, destinations in ([], [point]), ([point], []), ([], []):
        result = routing.travel_time_matrix(origins, destinations, mode="walk")
        assert result["elements"] == []
        assert result["mode"] == "walk"
        assert "durations_note" in result


# --- shared-vs-fallback dispatch geometry -----------------------------------


def test_wide_origin_spread_with_short_cells_stays_on_the_shared_graph(monkeypatch):
    # Origins ~8.4km apart (over walk's ~7.5km straight-line cap), one
    # destination midway: every actual matrix cell is ~4.2km. The dispatch
    # gate is the widest ORIGIN<->DESTINATION pair — origin<->origin
    # separations no cell routes must not push this onto the per-pair path.
    class Chosen(Exception):
        pass

    def sentinel(*args, **kwargs):
        raise Chosen

    monkeypatch.setattr(routing, "_get_or_build_graph", sentinel)
    origins = [(40.75, -74.05), (40.75, -73.95)]
    destinations = [(40.75, -74.00)]
    with pytest.raises(Chosen):
        routing._travel_time_matrix_shared_graph(origins, destinations, "walk")


def test_oversized_enclosing_circle_still_falls_back_per_pair(monkeypatch):
    # Cross pairs (~6.7km) are under walk's cap, but the combined set's
    # enclosing circle (~6.7km radius before buffering) outgrows the
    # extraction cap — no shared graph may be built; fall back per pair.
    def sentinel(*args, **kwargs):
        raise AssertionError("must not build a graph past the extraction cap")

    monkeypatch.setattr(routing, "_get_or_build_graph", sentinel)
    origins = [(40.75, -74.08), (40.75, -73.92)]
    destinations = [(40.75, -74.00)]
    assert routing._travel_time_matrix_shared_graph(origins, destinations, "walk") is None


def test_cross_pair_over_the_cap_falls_back_without_building(monkeypatch):
    def sentinel(*args, **kwargs):
        raise AssertionError("an over-cap cross pair must skip the shared graph entirely")

    monkeypatch.setattr(routing, "_get_or_build_graph", sentinel)
    origins = [fx.node_latlon(2, 2)]
    destinations = [fx.node_latlon(5, 5), FAR_POINT]
    assert routing._travel_time_matrix_shared_graph(origins, destinations, "walk") is None


# --- truncation honesty ------------------------------------------------------


def test_shared_graph_truncation_is_surfaced(monkeypatch):
    original = routing._get_or_build_graph
    touched = []

    def capped(*args, **kwargs):
        graph = original(*args, **kwargs)
        touched.append((graph, graph.truncated))
        graph.truncated = True
        return graph

    monkeypatch.setattr(routing, "_get_or_build_graph", capped)
    try:
        result = server.travel_time_matrix(
            origins=_as_dicts([fx.node_latlon(2, 2)]),
            destinations=_as_dicts([fx.node_latlon(5, 5)]),
            mode="walk",
        )
    finally:
        # Graphs are cached and shared across tests; restore their flags.
        for graph, was_truncated in touched:
            graph.truncated = was_truncated

    assert result["truncated"] is True
    assert "size cap" in result["note"]
    assert result["elements"][0]["distance_m"] is not None


def test_fallback_surfaces_per_pair_truncation(monkeypatch):
    monkeypatch.setattr(routing, "_travel_time_matrix_shared_graph", lambda *a, **k: None)
    monkeypatch.setattr(
        routing,
        "route",
        lambda *a, **k: {"duration_s": 60.0, "distance_m": 100.0, "truncated": True},
    )
    result = routing.travel_time_matrix(
        [fx.node_latlon(2, 2)], [fx.node_latlon(5, 5)], mode="walk"
    )
    assert result["truncated"] is True
    assert "size cap" in result["note"]


def test_untruncated_matrix_has_no_truncated_flag():
    result = server.travel_time_matrix(
        origins=_as_dicts([fx.node_latlon(2, 2)]),
        destinations=_as_dicts([fx.node_latlon(5, 5)]),
        mode="walk",
    )
    assert "truncated" not in result
    assert "note" not in result


# --- fallback NoGraphNearby contract ----------------------------------------


def test_fallback_raises_when_every_pair_has_no_graph(monkeypatch):
    def no_graph(*args, **kwargs):
        raise routing.NoGraphNearby(40.0, -74.0, 1000.0, mode="walk")

    monkeypatch.setattr(routing, "route", no_graph)
    monkeypatch.setattr(routing, "_travel_time_matrix_shared_graph", lambda *a, **k: None)
    # Library level: the documented top-level failure, not a matrix of nulls.
    with pytest.raises(routing.NoGraphNearby):
        routing.travel_time_matrix([(40.0, -74.0)], [(40.5, -74.5)], mode="walk")
    # Server level: the documented structured error.
    result = server.travel_time_matrix(
        origins=[{"lat": 40.0, "lon": -74.0}],
        destinations=[{"lat": 40.5, "lon": -74.5}],
    )
    assert result["error"] == "no_graph_nearby"


def test_fallback_mixed_failures_still_return_a_matrix(monkeypatch):
    calls = iter(["no_graph", "too_long"])

    def failing(*args, **kwargs):
        if next(calls) == "no_graph":
            raise routing.NoGraphNearby(40.0, -74.0, 1000.0, mode="walk")
        raise routing.RouteTooLong(9000.0, 7520.0)

    monkeypatch.setattr(routing, "route", failing)
    grid, truncated = routing._travel_time_matrix_fallback(
        [(40.0, -74.0)], [(40.5, -74.5), (40.6, -74.6)], "walk"
    )
    assert grid == [[None, None]]
    assert truncated is False
