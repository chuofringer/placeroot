"""compare_modes: one-call walk/cycle/drive comparison (#459).

Same mocked resolves and committed routing fixture as test_from_to.py. The
ends must resolve exactly once regardless of how many modes are compared,
per-mode failures must stay inline in their own row, and the compact rows
must never carry route()'s geometry/export payload.
"""

import asyncio

import pytest

from placeroot import geocode, progress, routing, server

from ._routing_fixture import build_routing_fixture as fx

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)

ROW_FIELDS = {"mode", "distance_m", "duration_s", "duration_min"}


def _hit(name, lat, lon, type_="place", id_="gers-x"):
    return {"name": name, "lat": lat, "lon": lon, "id": id_, "type": type_}


def _ab(query):
    if query == "A":
        return _hit(query, FROM_LAT, FROM_LON, id_="gers-a")
    return _hit(query, TO_LAT, TO_LON, id_="gers-b")


@pytest.fixture
def resolve_ab(monkeypatch):
    monkeypatch.setattr(geocode, "resolve_named_place", _ab)


def test_compare_modes_schema():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["compare_modes"].input_schema
    props = schema["properties"]
    assert set(props) == {"from", "to", "modes", "include_elevation", "confirm"}
    assert "from_" not in props
    assert set(schema["required"]) == {"from", "to"}
    assert props["modes"]["items"]["enum"] == ["cycle", "drive", "walk"]
    for name in ("from", "to"):
        types = {branch.get("type") for branch in props[name]["anyOf"]}
        assert types == {"string", "object"}


def test_all_three_modes_succeed_in_requested_order(resolve_ab):
    result = server.compare_modes("A", "B", confirm=True)
    assert "error" not in result
    assert [r["mode"] for r in result["modes"]] == ["walk", "cycle", "drive"]
    for row in result["modes"]:
        assert "error" not in row
        assert row["distance_m"] > 0
        assert row["duration_s"] > 0
        assert isinstance(row["duration_min"], int)
        assert row["duration_min"] == round(row["duration_s"] / 60)
        assert not ({"path", "export", "from", "to", "progress", "status"} & set(row))
        assert set(row) <= ROW_FIELDS | {"truncated"}
    assert result["fastest"] in ("walk", "cycle", "drive")
    assert result["shortest"] in ("walk", "cycle", "drive")
    # Every mode routes the same two coordinates, so cycle (4.2 m/s) must
    # beat walk (1.4 m/s) on duration.
    by_mode = {r["mode"]: r for r in result["modes"]}
    assert by_mode["cycle"]["duration_s"] < by_mode["walk"]["duration_s"]
    assert result["fastest"] != "walk"
    assert isinstance(result["summary"], str) and result["summary"]
    assert "no live traffic" in result["summary"]
    # Resolved ends echoed once, at the top, with the name/id the input resolved to.
    assert result["from"]["name"] == "A" and result["from"]["id"] == "gers-a"
    assert result["to"]["name"] == "B" and result["to"]["lat"] == TO_LAT


def test_subset_modes_and_duplicates(resolve_ab):
    result = server.compare_modes("A", "B", modes=["walk"], confirm=True)
    assert [r["mode"] for r in result["modes"]] == ["walk"]
    assert result["fastest"] == "walk" and result["shortest"] == "walk"
    assert "Only walking" in result["summary"]

    result = server.compare_modes("A", "B", modes=["drive", "walk", "drive"], confirm=True)
    assert [r["mode"] for r in result["modes"]] == ["drive", "walk"]


def test_bad_modes_are_bad_request(resolve_ab):
    result = server.compare_modes("A", "B", modes=["fly"], confirm=True)
    assert result["error"] == "bad_request"
    assert "fly" in result["detail"]
    assert result["supported"] == ["walk", "cycle", "drive"]
    assert server.compare_modes("A", "B", modes=[], confirm=True)["error"] == "bad_request"
    assert server.compare_modes("A", "B", modes="walk", confirm=True)["error"] == "bad_request"


def test_one_mode_too_far_stays_inline(resolve_ab, monkeypatch):
    caps = dict(routing.ROUTE_MAX_STRAIGHT_LINE_M)
    caps["drive"] = 1.0
    monkeypatch.setattr(routing, "ROUTE_MAX_STRAIGHT_LINE_M", caps)
    result = server.compare_modes("A", "B", confirm=True)
    assert "error" not in result
    by_mode = {r["mode"]: r for r in result["modes"]}
    assert by_mode["drive"]["error"] == "too_far"
    assert by_mode["drive"]["max_distance_m"] == 1.0
    assert "'A'" in by_mode["drive"]["detail"] and "compare_modes" in by_mode["drive"]["detail"]
    assert "distance_m" not in by_mode["drive"]
    assert "error" not in by_mode["walk"] and "error" not in by_mode["cycle"]
    assert result["fastest"] in ("walk", "cycle")
    assert result["shortest"] in ("walk", "cycle")
    assert "drive (too_far)" in result["summary"]
    assert "no live traffic" not in result["summary"]


def test_all_modes_fail_gives_null_winners(resolve_ab, monkeypatch):
    monkeypatch.setattr(
        routing, "ROUTE_MAX_STRAIGHT_LINE_M", {m: 1.0 for m in routing.ROUTE_MAX_STRAIGHT_LINE_M}
    )
    result = server.compare_modes("A", "B", confirm=True)
    assert "error" not in result
    assert all(r["error"] == "too_far" for r in result["modes"])
    assert result["fastest"] is None and result["shortest"] is None
    assert result["summary"].startswith("No mode could be routed")
    assert "walk — too_far" in result["summary"]


def test_cold_graph_needs_confirm_per_row(resolve_ab):
    routing.clear_graph_cache()
    result = server.compare_modes("A", "B", modes=["walk"], confirm=False)
    assert "error" not in result
    row = result["modes"][0]
    assert row["error"] == "needs_confirm"
    assert row["eta"] == progress.format_eta(*progress.GRAPH_BUILD_S)
    assert row["eta_s"] == [int(progress.GRAPH_BUILD_S[0]), int(progress.GRAPH_BUILD_S[1])]
    assert result["fastest"] is None


def test_ends_resolve_exactly_once_regardless_of_mode_count(monkeypatch):
    calls = []

    def counting(query):
        calls.append(query)
        return _ab(query)

    monkeypatch.setattr(geocode, "resolve_named_place", counting)
    result = server.compare_modes("A", "B", confirm=True)
    assert len(result["modes"]) == 3
    assert sorted(calls) == ["A", "B"]


def test_end_errors_fail_the_whole_call(monkeypatch):
    result = server.compare_modes("", "B")
    assert result["error"] == "bad_request" and result["field"] == "from"
    result = server.compare_modes("A", "   ")
    assert result["error"] == "bad_request" and result["field"] == "to"

    monkeypatch.setattr(geocode, "resolve_named_place", lambda *_a, **_k: None)
    result = server.compare_modes("Nowhereville", "Also Nowhere")
    assert result["error"] == "not_found"
    assert result["field"] == "from"
    assert "modes" not in result


def test_ambiguous_end_propagates_with_field(monkeypatch):
    from placeroot import errors

    def fake_resolve(query):
        if query == "B":
            raise errors.AmbiguousPlace(query, candidates=[{"name": "B1"}, {"name": "B2"}])
        return _ab(query)

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.compare_modes("A", "B")
    assert result["error"] == "ambiguous_place"
    assert result["field"] == "to"
    assert result["candidates"] == [{"name": "B1"}, {"name": "B2"}]


def test_coordinate_dict_ends_pass_through():
    result = server.compare_modes(
        {"lat": FROM_LAT, "lon": FROM_LON},
        {"lat": TO_LAT, "lon": TO_LON},
        modes=["walk", "cycle"],
        confirm=True,
    )
    assert "error" not in result
    assert result["from"]["lat"] == FROM_LAT and "name" not in result["from"]
    assert all("error" not in r for r in result["modes"])


def test_include_elevation_passes_climb_through(resolve_ab, monkeypatch):
    seen = []

    def fake_route(from_lat, from_lon, to_lat, to_lon, mode, **kwargs):
        seen.append((mode, kwargs.get("include_elevation")))
        return {
            "distance_m": 1000.0,
            "duration_s": 600.0,
            "mode": mode,
            "from": {"lat": from_lat, "lon": from_lon},
            "to": {"lat": to_lat, "lon": to_lon},
            "export": {"gpx": "..."},
            "elevation": {"total_climb_m": 18.0, "total_descent_m": 4.0, "samples": [[0, 1]]},
        }

    monkeypatch.setattr(server, "route", fake_route)
    result = server.compare_modes("A", "B", include_elevation=True, confirm=True)
    assert seen == [("walk", True), ("cycle", True), ("drive", True)]
    for row in result["modes"]:
        assert row["total_climb_m"] == 18.0
        assert row["total_descent_m"] == 4.0
        assert "elevation" not in row and "export" not in row
    assert result["from"]["name"] == "A"

    # Without the flag the block is neither requested nor echoed, even if
    # route() were to return one.
    seen.clear()
    result = server.compare_modes("A", "B", modes=["walk"], confirm=True)
    assert seen == [("walk", False)]
    assert "total_climb_m" not in result["modes"][0]


def test_placeroot_call_dispatches_from_alias(resolve_ab):
    """The progressive dispatcher maps the published `from` back to from_."""
    through = server.placeroot_call(
        "compare_modes", {"from": "A", "to": "B", "modes": ["walk"], "confirm": True}
    )
    assert "error" not in through
    assert through["modes"][0]["mode"] == "walk"
