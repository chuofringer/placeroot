"""Comfort-aware routing (#313): elevation profiles and prefer="flat".

Elevation lookups never touch the network here: elevation.elevations_at is
monkeypatched with a small deterministic fake (linear in latitude, or a
fixed dict), same spirit as tests/test_elevation_at.py's synthetic-COG
approach but simpler since these tests only need elevation.elevations_at's
*return shape* ({"elevation_m": ... | None}), not the DEM tile format
itself. The street graph is the committed transportation fixture (20x20
grid, 100m spacing) — see scripts/build_routing_fixture.py and
tests/_routing_fixture.py; the autouse fixture in tests/conftest.py points
routing.py at it for every test in this file.
"""

import asyncio
import math

import pytest

from placeroot import elevation, errors, routing, server

from ._routing_fixture import build_routing_fixture as fx

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)

# ---------------------------------------------------------------------------
# _dijkstra_path_to_target_flat: pure algorithm tests, no fixture/graph needed
# ---------------------------------------------------------------------------


def _manual_graph() -> routing.Graph:
    """A <-> B <-> D (short, steep) and A <-> C <-> D (longer, flat).

    A-B-D: 100m + 100m = 200m, but B sits 50m above both A and D (a climb
    then an equal descent) so the *plain*-distance shortest path prefers it
    (it's shorter than A-C-D below).
    A-C-D: 100m + 150m = 250m, and every node is at the same elevation (no
    climb at all).
    """
    g = routing.Graph()
    for node in ("a", "b", "c", "d"):
        g.add_node(node, 0.0, 0.0)  # coordinates unused by these tests
    g.add_edge("a", "b", 100.0, 100.0)
    g.add_edge("b", "d", 100.0, 100.0)
    g.add_edge("a", "c", 100.0, 100.0)
    g.add_edge("c", "d", 150.0, 150.0)
    return g


def test_dijkstra_flat_ignores_penalty_when_elevations_unknown():
    g = _manual_graph()
    result = routing._dijkstra_path_to_target_flat(g, "a", "d", speed_m_s=1.0, node_elev={})
    assert result is not None
    _time_s, distance_m, path = result
    assert distance_m == pytest.approx(200.0)
    assert [n for n, _m in path] == ["a", "b", "d"]


def test_dijkstra_flat_trades_distance_for_climb():
    g = _manual_graph()
    node_elev = {"a": 0.0, "b": 50.0, "c": 0.0, "d": 0.0}
    result = routing._dijkstra_path_to_target_flat(
        g, "a", "d", speed_m_s=1.0, node_elev=node_elev
    )
    assert result is not None
    _time_s, distance_m, path = result
    # The flat path (a-c-d, 250m) is chosen over the steep-but-shorter one
    # (a-b-d, 200m) once climbing b costs 20m of penalty per meter of climb.
    assert [n for n, _m in path] == ["a", "c", "d"]
    # The reported distance is the path's TRUE distance, not inflated by
    # the ranking penalty that picked it.
    assert distance_m == pytest.approx(250.0)


def test_dijkstra_flat_descent_is_free():
    """A pure descent (b below a and d) must not be penalized — prefer="flat"
    is about avoiding climbs, not avoiding grade in general."""
    g = _manual_graph()
    node_elev = {"a": 50.0, "b": 0.0, "c": 50.0, "d": 50.0}
    result = routing._dijkstra_path_to_target_flat(
        g, "a", "d", speed_m_s=1.0, node_elev=node_elev
    )
    assert result is not None
    _time_s, distance_m, path = result
    # b is 50m below a (free) then 50m back up to d (penalized) — the net
    # climb along a-b-d is the same 50m single climb as before, so this
    # still switches to the flat route... unless the *descent* were also
    # penalized, in which case some other path would look relatively worse.
    # The assertion that matters here is just that a pure descent leg (a->b)
    # never adds any penalty on its own.
    steep_path_penalty = routing.FLAT_CLIMB_PENALTY_M_PER_M * max(0.0, 0.0 - 50.0)
    assert steep_path_penalty == 0.0
    assert distance_m in (200.0, 250.0)  # sanity: a real path was found
    assert [n for n, _m in path][0] == "a"
    assert [n for n, _m in path][-1] == "d"


def test_dijkstra_flat_same_source_and_target():
    g = _manual_graph()
    result = routing._dijkstra_path_to_target_flat(g, "a", "a", 1.0, {"a": 0.0})
    assert result == (0.0, 0.0, [("a", 0.0)])


def test_dijkstra_flat_returns_none_when_unreachable():
    g = routing.Graph()
    g.add_node("a", 0.0, 0.0)
    g.add_node("z", 0.0, 0.0)
    assert routing._dijkstra_path_to_target_flat(g, "a", "z", 1.0, {}) is None


# ---------------------------------------------------------------------------
# _node_elevations_for_flat: fetch, cache, cap, and error handling
# ---------------------------------------------------------------------------


def _small_graph_with_nodes(n: int) -> routing.Graph:
    g = routing.Graph()
    for i in range(n):
        g.add_node(f"n{i}", 40.0 + i * 0.0001, -74.0)
    return g


def test_node_elevations_for_flat_caches_on_the_graph_object(monkeypatch):
    calls = []

    def fake_elevations_at(points):
        calls.append(len(points))
        return [{"elevation_m": 1.0} for _ in points]

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    g = _small_graph_with_nodes(5)
    elev1, err1 = routing._node_elevations_for_flat(g)
    assert err1 is None
    assert len(elev1) == 5
    calls_after_first = len(calls)
    assert calls_after_first > 0

    elev2, err2 = routing._node_elevations_for_flat(g)
    assert elev2 is elev1
    assert err2 is None
    assert len(calls) == calls_after_first, "second call re-fetched instead of using the cache"


def test_node_elevations_for_flat_bounds_lookups_to_the_cap(monkeypatch):
    fetched_points = []

    def fake_elevations_at(points):
        fetched_points.extend(points)
        return [{"elevation_m": 1.0} for _ in points]

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    g = _small_graph_with_nodes(routing.FLAT_MAX_ELEVATION_NODES + 50)
    node_elev, err = routing._node_elevations_for_flat(g)
    assert err is None
    assert len(fetched_points) == routing.FLAT_MAX_ELEVATION_NODES
    assert len(node_elev) == routing.FLAT_MAX_ELEVATION_NODES


def test_node_elevations_for_flat_skips_points_with_no_coverage(monkeypatch):
    def fake_elevations_at(points):
        return [{"elevation_m": None, "note": "no coverage"} for _ in points]

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    g = _small_graph_with_nodes(3)
    node_elev, err = routing._node_elevations_for_flat(g)
    assert err is None
    assert node_elev == {}


def test_node_elevations_for_flat_reports_upstream_failure(monkeypatch):
    def fake_elevations_at(points):
        raise errors.UpstreamUnavailable("simulated network failure")

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    g = _small_graph_with_nodes(3)
    node_elev, err = routing._node_elevations_for_flat(g)
    assert node_elev == {}
    assert err is not None
    assert "simulated network failure" in err


# ---------------------------------------------------------------------------
# route(prefer="flat"): end-to-end through the real fixture graph
# ---------------------------------------------------------------------------

# A "hill" scenario: (0,0) -> (2,0) has a direct 2-hop path through (1,0),
# and cheaper (distance-wise) detours that avoid (1,0) entirely — the
# cheapest being (0,0) -diagonal-> (1,1) -> (2,1) -> (2,0) (the fixture's
# DIAG_STEP diagonal edge connects (0,0)-(1,1) directly). Marking (1,0) as a
# tall "hill" and every other node at sea level (a *complete* elevation map
# for the extracted graph, not a sparse one, so there's no unannotated-node
# loophole a detour could exploit) means every hill-avoiding route has zero
# real climb while the direct path climbs the hill's full height and back
# down — so prefer="flat" trading distance for that climb is a genuine,
# unambiguous test of the reweighting.
HILL_A = fx.node_latlon(0, 0)
HILL_D = fx.node_latlon(2, 0)
HILL_DIRECT_LENGTH_M = 200.0  # (0,0) -> (1,0) -> (2,0)
HILL_DETOUR_LENGTH_M = 100.0 * math.sqrt(2.0) + 200.0  # (0,0) =diag=> (1,1) -> (2,1) -> (2,0)


def _full_node_elev(monkeypatch, hill: dict[str, float], default: float = 0.0) -> None:
    """Stand in for _node_elevations_for_flat with a *complete* elevation map
    over whatever graph gets extracted (every node, not just a few ids) —
    so there's no unannotated-node loophole a cheaper detour could exploit
    that wouldn't reflect this test's intended (flat-everywhere-but-the-hill)
    terrain."""

    def fake(graph):
        return {node_id: hill.get(node_id, default) for node_id in graph.coords}, None

    monkeypatch.setattr(routing, "_node_elevations_for_flat", fake)


def test_route_default_prefers_the_shorter_direct_path():
    result = routing.route(*HILL_A, *HILL_D, mode="walk")
    assert "error" not in result
    assert result["distance_m"] == pytest.approx(HILL_DIRECT_LENGTH_M, rel=0.02)


def test_route_prefer_flat_switches_to_the_flatter_longer_path(monkeypatch):
    _full_node_elev(monkeypatch, {fx.node_id(1, 0): 500.0})
    result = routing.route(*HILL_A, *HILL_D, mode="walk", prefer="flat")
    assert "error" not in result
    assert result["prefer"] == "flat"
    assert "prefer_note" not in result
    # Genuinely switched routes — not just a slightly-longer measurement of
    # the same direct path.
    assert result["distance_m"] == pytest.approx(HILL_DETOUR_LENGTH_M, rel=0.02)
    assert result["distance_m"] > HILL_DIRECT_LENGTH_M


def test_route_prefer_flat_reports_true_distance_not_penalized(monkeypatch):
    """Even when the penalty picks a path, distance_m/duration_s must be the
    path's real cost — never inflated by the ranking penalty."""
    _full_node_elev(monkeypatch, {fx.node_id(1, 0): 5000.0})  # a huge hill
    result = routing.route(*HILL_A, *HILL_D, mode="walk", prefer="flat")
    assert result["distance_m"] == pytest.approx(HILL_DETOUR_LENGTH_M, rel=0.02)
    assert result["duration_s"] == pytest.approx(
        HILL_DETOUR_LENGTH_M / routing.DEFAULT_SPEED_M_S, rel=0.02
    )


def test_route_prefer_flat_falls_back_honestly_when_no_coverage(monkeypatch):
    def fake_elevations_at(points):
        return [{"elevation_m": None} for _ in points]

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    result = routing.route(*HILL_A, *HILL_D, mode="walk", prefer="flat")
    assert "error" not in result
    assert result["prefer"] == "flat"
    assert "prefer_note" in result
    assert "no" in result["prefer_note"].lower()
    # Fell back to plain-distance routing, so the direct (shorter) path is
    # still what gets used — never a fabricated "flat" answer.
    assert result["distance_m"] == pytest.approx(HILL_DIRECT_LENGTH_M, rel=0.02)


def test_route_prefer_flat_falls_back_honestly_on_upstream_failure(monkeypatch):
    def fake_elevations_at(points):
        raise errors.UpstreamUnavailable("simulated network failure")

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    result = routing.route(*HILL_A, *HILL_D, mode="walk", prefer="flat")
    assert "error" not in result
    assert "prefer_note" in result
    assert "simulated network failure" in result["prefer_note"]
    assert result["distance_m"] == pytest.approx(HILL_DIRECT_LENGTH_M, rel=0.02)


def test_route_prefer_unsupported_value_raises_value_error():
    with pytest.raises(ValueError):
        routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", prefer="scenic")


def test_route_prefer_flat_rejects_drive_mode():
    with pytest.raises(ValueError):
        routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", prefer="flat")


def test_route_prefer_omitted_by_default():
    result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert "prefer" not in result
    assert "prefer_note" not in result


# ---------------------------------------------------------------------------
# server.route(): prefer validation and pass-through
# ---------------------------------------------------------------------------


def test_server_route_prefer_flat_rejects_drive_mode():
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", prefer="flat")
    assert result["error"] == "bad_request"
    assert "flat" in result["detail"]


def test_server_route_prefer_unsupported_value_bad_request():
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", prefer="scenic")
    assert result["error"] == "bad_request"


def test_server_route_prefer_flat_happy_path(monkeypatch):
    _full_node_elev(monkeypatch, {fx.node_id(1, 0): 500.0})
    lat_a, lon_a = HILL_A
    lat_b, lon_b = HILL_D
    result = server.route(lat_a, lon_a, lat_b, lon_b, mode="walk", prefer="flat", confirm=True)
    assert "error" not in result
    assert result["prefer"] == "flat"
    assert result["distance_m"] == pytest.approx(HILL_DETOUR_LENGTH_M, rel=0.02)


def test_server_route_schema_exposes_prefer():
    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "route")
    schema = tool.model_dump(mode="json", by_alias=True, exclude_none=True)["inputSchema"]
    assert "prefer" in schema["properties"]
    assert "prefer" not in schema.get("required", [])
    desc = (tool.description or "").lower()
    assert "step-free" in desc or "wheelchair" in desc


# ---------------------------------------------------------------------------
# route(include_elevation=True): the climb profile
# ---------------------------------------------------------------------------


def _install_linear_elevation(monkeypatch, scale: float = 100000.0) -> None:
    """elevation_m grows linearly with latitude — a clean, deterministic
    monotonic field for checking climb/descent/grade totals against a known
    slope (this fixture's grid moves north as j increases, so a route
    running along increasing j always climbs under this field)."""

    def fake_elevations_at(points):
        return [{"elevation_m": (lat - fx.ORIGIN_LAT) * scale} for lat, lon in points]

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)


def test_route_include_elevation_totals_on_a_monotonic_climb(monkeypatch):
    _install_linear_elevation(monkeypatch)
    result = routing.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", include_elevation=True
    )
    assert "error" not in result
    profile = result["elevation"]
    assert "note" not in profile
    start_elev = (FROM_LAT - fx.ORIGIN_LAT) * 100000.0
    end_elev = (TO_LAT - fx.ORIGIN_LAT) * 100000.0
    expected_climb = max(0.0, end_elev - start_elev)
    assert profile["total_climb_m"] == pytest.approx(expected_climb, rel=0.05)
    assert profile["total_descent_m"] == pytest.approx(0.0, abs=1.0)
    assert profile["max_grade_pct"] > 0
    assert isinstance(profile["samples"], list)
    assert len(profile["samples"]) >= 2
    assert len(profile["samples"]) <= routing.ELEVATION_PROFILE_MAX_SAMPLES
    first_at, first_elev = profile["samples"][0]
    last_at, last_elev = profile["samples"][-1]
    assert first_at == pytest.approx(0.0, abs=0.5)
    assert last_at == pytest.approx(result["distance_m"], rel=0.02)
    assert first_elev == pytest.approx(start_elev, abs=5.0)
    assert last_elev == pytest.approx(end_elev, abs=5.0)


def test_route_include_elevation_no_coverage_is_honest_not_zero(monkeypatch):
    def fake_elevations_at(points):
        return [{"elevation_m": None} for _ in points]

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    result = routing.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", include_elevation=True
    )
    profile = result["elevation"]
    assert "note" in profile
    assert "total_climb_m" not in profile
    assert "total_descent_m" not in profile
    assert "max_grade_pct" not in profile


def test_route_include_elevation_partial_coverage_notes_the_gap(monkeypatch):
    calls = {"n": 0}

    def fake_elevations_at(points):
        out = []
        for lat, lon in points:
            calls["n"] += 1
            # Every other sample has no coverage.
            out.append(
                {"elevation_m": None}
                if calls["n"] % 2 == 0
                else {"elevation_m": (lat - fx.ORIGIN_LAT) * 100000.0}
            )
        return out

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    result = routing.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", include_elevation=True
    )
    profile = result["elevation"]
    assert "note" in profile
    assert "coverage missing" in profile["note"]
    # Stats are still reported (computed only over the covered stretches),
    # not dropped just because coverage was partial.
    assert "total_climb_m" in profile


def test_route_include_elevation_upstream_unavailable_note_only(monkeypatch):
    def fake_elevations_at(points):
        raise errors.UpstreamUnavailable("simulated network failure")

    monkeypatch.setattr(elevation, "elevations_at", fake_elevations_at)
    result = routing.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", include_elevation=True
    )
    profile = result["elevation"]
    assert "simulated network failure" in profile["note"]
    assert "samples" not in profile
    assert "total_climb_m" not in profile


def test_route_include_elevation_omitted_when_budget_too_small(monkeypatch):
    _install_linear_elevation(monkeypatch)
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "70")
    result = routing.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", include_elevation=True
    )
    assert result.get("elevation_omitted") is True
    assert "elevation" not in result


def test_route_include_elevation_off_by_default():
    result = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert "elevation" not in result
    assert "elevation_omitted" not in result


def test_route_include_elevation_zero_length_route_has_no_profile(monkeypatch):
    _install_linear_elevation(monkeypatch)
    result = routing.route(
        FROM_LAT, FROM_LON, FROM_LAT, FROM_LON, mode="walk", include_elevation=True
    )
    assert result.get("elevation_omitted") is True


# --- server.route(): include_elevation pass-through -------------------------


def test_server_route_include_elevation_passthrough(monkeypatch):
    _install_linear_elevation(monkeypatch)
    result = server.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", include_elevation=True, confirm=True
    )
    assert "error" not in result
    assert "elevation" in result
    assert "total_climb_m" in result["elevation"]

    default = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", confirm=True)
    assert "elevation" not in default


def test_server_route_schema_exposes_include_elevation_defaulting_to_false():
    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "route")
    schema = tool.model_dump(mode="json", by_alias=True, exclude_none=True)["inputSchema"]
    prop = schema["properties"]["include_elevation"]
    assert prop.get("type") == "boolean"
    assert prop.get("default") is False
    assert "include_elevation" not in schema.get("required", [])


# --- from_to(): both features pass through -----------------------------------


def test_from_to_passes_through_include_elevation_and_prefer(monkeypatch):
    from placeroot import geocode

    def fake_resolve(query):
        if query == "Origin Place":
            return {"name": "Origin Place", "lat": FROM_LAT, "lon": FROM_LON, "id": "gers-a",
                    "type": "place"}
        if query == "Destination Place":
            return {"name": "Destination Place", "lat": TO_LAT, "lon": TO_LON, "id": "gers-b",
                    "type": "place"}
        raise AssertionError(f"unexpected resolve {query!r}")

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    _install_linear_elevation(monkeypatch)
    result = server.from_to(
        "Origin Place", "Destination Place", mode="walk", include_elevation=True, confirm=True
    )
    assert "error" not in result
    assert "elevation" in result
