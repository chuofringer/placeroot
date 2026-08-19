"""Named-place compose: from_to (#328).

Mocked resolves for the happy path so the test does not depend on live
geocoding; the street graph is the committed routing fixture. Far-apart
cities must error before any graph is built.
"""

import asyncio

import pytest

from placeroot import geocode, routing, server

from ._routing_fixture import build_routing_fixture as fx

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)


def _call_from_to(origin: str, dest: str, mode: str = "walk", confirm: bool = True) -> dict:
    return server.from_to(origin, dest, mode=mode, confirm=confirm)


def _hit(name, lat, lon, type_="place", id_="gers-x"):
    return {"name": name, "lat": lat, "lon": lon, "id": id_, "type": type_}


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
        lambda query: _hit(query, FROM_LAT, FROM_LON) if query == "A" else _hit(query, TO_LAT, TO_LON),
    )
    result = _call_from_to("A", "B", confirm=False)
    assert result["error"] == "needs_confirm"
    assert result["eta_s"] == [5, 25]


def test_from_to_confirm_runs(monkeypatch):
    routing.clear_graph_cache()
    monkeypatch.setattr(
        geocode,
        "resolve_named_place",
        lambda query: _hit(query, FROM_LAT, FROM_LON) if query == "A" else _hit(query, TO_LAT, TO_LON),
    )
    result = _call_from_to("A", "B", confirm=True)
    assert "error" not in result
    assert result["distance_m"] > 0
