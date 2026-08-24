"""Named-place compose: from_to (#328).

Mocked resolves for the happy path so the test does not depend on live
geocoding; the street graph is the committed routing fixture. Far-apart
cities must error before any graph is built.
"""

import asyncio

import pytest

from placeroot import geocode, preferences, progress, routing, server

from ._routing_fixture import build_routing_fixture as fx

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)


def _call_from_to(origin: str, dest: str, mode: str = "walk", confirm: bool = True) -> dict:
    return server.from_to(origin, dest, mode=mode, confirm=confirm)


def _hit(name, lat, lon, type_="place", id_="gers-x"):
    return {"name": name, "lat": lat, "lon": lon, "id": id_, "type": type_}


def _ab(query):
    if query == "A":
        return _hit(query, FROM_LAT, FROM_LON)
    return _hit(query, TO_LAT, TO_LON)


def test_from_to_schema_accepts_from_and_to():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools["from_to"].input_schema["properties"]
    assert "from" in props
    assert "to" in props
    assert "from_" not in props
    required = set(tools["from_to"].input_schema.get("required") or [])
    assert "from" in required and "to" in required
    assert "confirm" in props
    assert "confirm" not in required
    # #328/#395 regression: the FromToArguments monkeypatch used to drop
    # every parameter from_to() actually accepts beyond from/to/mode/confirm.
    assert "include_path" in props
    assert "include_elevation" in props
    assert "prefer" in props
    assert props["mode"]["enum"] == ["cycle", "drive", "walk"]
    assert props["prefer"]["enum"] == ["flat"]
    # LocationRef wave 1 (roadmap #4.1): from/to widened to accept a
    # {lat, lon} dict or a GERS id string, not just a free-text name.
    for name in ("from", "to"):
        types = {branch.get("type") for branch in props[name]["anyOf"]}
        assert types == {"string", "object"}


def test_from_to_omitted_mode_uses_stored_preference(monkeypatch):
    preferences.update(mode="cycle")
    monkeypatch.setattr(geocode, "resolve_named_place", _ab)
    result = server.from_to("A", "B", confirm=True)
    assert "error" not in result
    assert result["mode"] == "cycle"


def test_from_to_explicit_mode_wins_over_stored_preference(monkeypatch):
    preferences.update(mode="cycle")
    monkeypatch.setattr(geocode, "resolve_named_place", _ab)
    result = server.from_to("A", "B", mode="walk", confirm=True)
    assert "error" not in result
    assert result["mode"] == "walk"


def test_from_to_with_mocked_resolves_returns_route_shape(monkeypatch):
    def fake_resolve(query):
        if query == "Shibuya Station":
            return _hit("Shibuya Station", FROM_LAT, FROM_LON, id_="gers-a")
        if query == "Yoyogi Park":
            return _hit("Yoyogi Park", TO_LAT, TO_LON, id_="gers-b")
        raise AssertionError(f"unexpected resolve {query!r}")

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = _call_from_to("Shibuya Station", "Yoyogi Park")
    assert "error" not in result
    assert result["mode"] == "walk"
    assert result["distance_m"] > 0
    assert result["duration_s"] > 0
    assert result["from"]["name"] == "Shibuya Station"
    assert result["to"]["name"] == "Yoyogi Park"
    assert "export" in result
    export = result["export"]
    assert "maps_link" in export
    assert "google" in export["maps_link"]
    assert "apple" in export["maps_link"]
    assert "gpx" in export
    assert "text" in export
    assert "Shibuya Station" in export["text"] or "Shibuya" in export["gpx"]


def test_from_to_far_apart_cities_errors_without_building_a_graph(monkeypatch):
    def fake_resolve(query):
        if "Tokyo" in query:
            return _hit("Tokyo", 35.68, 139.69, type_="locality")
        return _hit("New York", 40.71, -74.01, type_="locality")

    def boom(*_a, **_k):
        raise AssertionError("must not build a continent graph")

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    monkeypatch.setattr(routing, "route", boom)
    result = _call_from_to("Tokyo", "New York")
    assert result["error"] == "too_far"
    assert result["from"]["name"] == "Tokyo"
    assert result["to"]["name"] == "New York"
    assert result["distance_m"] > 1_000_000
    assert result["max_distance_m"] == pytest.approx(routing.ROUTE_MAX_STRAIGHT_LINE_M["walk"])
    assert result["mode"] == "walk"


def test_from_to_empty_names_are_bad_request():
    assert _call_from_to("  ", "Yoyogi Park")["error"] == "bad_request"
    assert _call_from_to("Shibuya", "")["error"] == "bad_request"


def test_from_to_unresolved_name(monkeypatch):
    monkeypatch.setattr(geocode, "resolve_named_place", lambda *_a, **_k: None)
    result = _call_from_to("Nowhereville", "Also Nowhere")
    assert result["error"] == "not_found"
    # Roadmap §4, next tier: not_found from name resolution names the next
    # move rather than leaving the caller to re-guess.
    assert result["try"]
    assert len(result["try"]) < 200


def test_from_to_unsupported_mode(monkeypatch):
    monkeypatch.setattr(
        geocode,
        "resolve_named_place",
        lambda query: _hit(query, FROM_LAT, FROM_LON),
    )
    result = _call_from_to("A", "B", mode="hovercraft")
    assert result["error"] == "unsupported_mode"


def test_from_to_cold_without_confirm_is_needs_confirm(monkeypatch):
    routing.clear_graph_cache()
    monkeypatch.setattr(
        geocode,
        "resolve_named_place",
        _ab,
    )
    result = _call_from_to("A", "B", confirm=False)
    assert result["error"] == "needs_confirm"
    assert result["eta"] == progress.format_eta(*progress.GRAPH_BUILD_S)
    assert result["eta_s"] == [int(progress.GRAPH_BUILD_S[0]), int(progress.GRAPH_BUILD_S[1])]


def test_from_to_confirm_runs(monkeypatch):
    routing.clear_graph_cache()
    monkeypatch.setattr(
        geocode,
        "resolve_named_place",
        _ab,
    )
    result = _call_from_to("A", "B", confirm=True)
    assert "error" not in result
    assert result["distance_m"] > 0


def test_from_to_parallel_resolves_keep_progress(monkeypatch):
    """ThreadPool workers must see the request log (contextvars.copy_context)."""
    progress.clear()

    def resolve_with_progress(query):
        progress.report(f"Resolving {query}")
        return _ab(query)

    monkeypatch.setattr(geocode, "resolve_named_place", resolve_with_progress)
    origin, dest = server._resolve_pair("A", "B")
    assert origin["name"] == "A"
    assert dest["name"] == "B"
    attached = progress.attach({"ok": True})
    lines = attached.get("progress") or []
    assert any("Resolving A" in line for line in lines)
    assert any("Resolving B" in line for line in lines)


# --- LocationRef (roadmap #4.1): from/to as {lat,lon} dict or GERS id ----


def test_from_to_with_coordinate_dicts():
    result = server.from_to(
        {"lat": FROM_LAT, "lon": FROM_LON}, {"lat": TO_LAT, "lon": TO_LON},
        mode="walk", confirm=True,
    )
    assert "error" not in result
    assert result["distance_m"] > 0
    assert result["from"]["lat"] == FROM_LAT
    assert result["to"]["lat"] == TO_LAT
    # A bare coordinate endpoint carries no name/id to echo.
    assert "name" not in result["from"]


def test_from_to_with_gers_ids(monkeypatch):
    from_id = "a" * 32
    to_id = "b" * 32

    def fake_lookup(id_, near_lat=None, near_lon=None):
        if id_ == from_id:
            lat, lon = FROM_LAT, FROM_LON
            name = "Origin GERS"
        else:
            lat, lon = TO_LAT, TO_LON
            name = "Dest GERS"
        return {
            "id": id_, "theme": "places", "type": "place", "name": name,
            "lat": lat, "lon": lon, "summary": {}, "related": {},
        }

    monkeypatch.setattr(server.gers, "gers_lookup", fake_lookup)
    result = server.from_to(from_id, to_id, mode="walk", confirm=True)
    assert "error" not in result
    assert result["from"]["name"] == "Origin GERS"
    assert result["from"]["id"] == from_id
    assert result["to"]["name"] == "Dest GERS"
    assert result["to"]["id"] == to_id


def test_from_to_with_mixed_dict_and_name(monkeypatch):
    monkeypatch.setattr(geocode, "resolve_named_place", _ab)
    result = server.from_to({"lat": FROM_LAT, "lon": FROM_LON}, "B", mode="walk", confirm=True)
    assert "error" not in result
    assert "name" not in result["from"]
    assert result["to"]["name"] == "B"


def test_from_to_plain_names_still_use_the_original_ambiguous_place_shape(monkeypatch):
    """Byte-identical fast path: both ends plain names still resolve through
    _resolve_pair, so the pre-existing ambiguous_place shape is unchanged."""
    from placeroot import errors

    def fake_resolve(query):
        if query == "A":
            raise errors.AmbiguousPlace(query, candidates=[{"name": "A1"}, {"name": "A2"}])
        return _ab(query)

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    result = server.from_to("A", "B", mode="walk", confirm=True)
    assert result["error"] == "ambiguous_place"
    assert result["field"] == "from"
    assert result["candidates"] == [{"name": "A1"}, {"name": "A2"}]
    assert "index" not in result


def test_from_to_bad_dict_ref_is_bad_request():
    result = server.from_to({"lat": 91.0, "lon": 0.0}, {"lat": TO_LAT, "lon": TO_LON})
    assert result["error"] == "bad_request"
    assert result["field"] == "from"
