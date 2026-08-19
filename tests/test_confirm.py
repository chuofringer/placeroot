"""Predict-then-ask before a >15s hop (#336).

A cold street-graph build (and a cold city warmup) must return
needs_confirm in well under 500ms unless the caller already asked the
user and passed confirm=true. A warm / cached graph never asks.
"""

import asyncio
import time

from placeroot import routing, server

from ._routing_fixture import build_routing_fixture as fx
from .conftest import CENTER_LAT, CENTER_LON

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)


def test_cold_route_without_confirm_is_needs_confirm_and_fast():
    routing.clear_graph_cache()
    started = time.monotonic()
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    elapsed_ms = (time.monotonic() - started) * 1000
    assert result["error"] == "needs_confirm"
    assert result["eta"] == "about 5–25 seconds"
    assert result["eta_s"] == [5, 25]
    assert "confirm=true" in result["detail"]
    assert "street graph" in result["detail"]
    assert elapsed_ms < 500


def test_cold_route_with_confirm_runs():
    routing.clear_graph_cache()
    result = server.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", confirm=True
    )
    assert "error" not in result
    assert result["distance_m"] > 0
    assert result["mode"] == "walk"
    assert "status" in result
    assert result["progress"]
    assert any("street graph" in line.lower() or "routing a" in line.lower()
               for line in result["progress"])


def test_warm_graph_never_asks(monkeypatch):
    routing.clear_graph_cache()
    # Populate the in-memory graph cache the same way a prior walk would.
    routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert routing.route_graph_is_cached(FROM_LAT, FROM_LON, TO_LAT, TO_LON, "walk")

    built = []
    real = routing.build_graph

    def spy(*args, **kwargs):
        built.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", spy)
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert "error" not in result
    assert result["distance_m"] > 0
    assert built == []
    assert any("Routing a walk" in line for line in result.get("progress", []))


def test_graph_is_cached_does_not_build(monkeypatch):
    routing.clear_graph_cache()
    assert routing.graph_is_cached(FROM_LAT, FROM_LON, "walk") is False
    routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk")
    assert routing.graph_is_cached(FROM_LAT, FROM_LON, "walk") is True
    built = []
    real = routing.build_graph

    def spy(*args, **kwargs):
        built.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", spy)
    assert routing.graph_is_cached(FROM_LAT, FROM_LON, "walk") is True
    assert built == []


def test_unsupported_mode_is_not_needs_confirm():
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="teleport")
    assert result["error"] == "unsupported_mode"


def test_route_too_long_is_not_needs_confirm():
    result = server.route(0.0, 0.0, 10.0, 10.0, mode="walk")
    assert result["error"] == "route_too_long"


def test_cold_warmup_without_confirm_is_needs_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)
    started = time.monotonic()
    result = server.warmup_city(lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000)
    elapsed_ms = (time.monotonic() - started) * 1000
    assert result["error"] == "needs_confirm"
    assert result["eta_s"] == [5, 25]
    assert "confirm=true" in result["detail"]
    assert elapsed_ms < 500


def test_warmup_cache_off_never_asks(monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    result = server.warmup_city(lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000)
    assert result["status"] == "cache_disabled"
    assert result.get("error") != "needs_confirm"


def test_route_confirm_docstring_does_not_mention_geocode():
    doc = server.route.__doc__.lower()
    assert "confirm=true after the user agreed" in doc
    assert "geocode" not in doc


def _tool(name):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    return tools[name]


def test_confirm_descriptions_require_needs_confirm_and_user_yes():
    """CTO schema bar: confirm=true only after needs_confirm AND the user said wait."""
    for name in ("route", "from_to", "warmup_city"):
        tool = _tool(name)
        desc = (tool.description or "").lower()
        assert "confirm=true after the user agreed" in desc, name
        assert "needs_confirm" in desc, name
        schema = tool.input_schema
        prop = schema["properties"]["confirm"]
        assert prop.get("type") == "boolean", name
        assert prop.get("default") is False, name
        assert "confirm" not in (schema.get("required") or []), name


def test_from_to_stays_names_only():
    props = _tool("from_to").input_schema["properties"]
    assert "lat" not in props and "lon" not in props
    assert "near_lat" not in props and "near_lon" not in props
    assert set(props) >= {"from", "to", "mode", "confirm"}


def test_find_near_has_no_confirm():
    props = _tool("find_near").input_schema["properties"]
    assert "confirm" not in props
    assert "near_lat" not in props


def test_needs_confirm_detail_asks_then_same_tool():
    for result in (
        server._needs_confirm_graph("walk"),
        server._needs_confirm_warmup(),
    ):
        detail = result["detail"].lower()
        assert "ask the user" in detail
        assert "same tool" in detail
        assert "confirm=true" in detail
        assert "geocode" not in detail
        assert "resolve_place" not in detail
        assert "geocode_batch" not in detail
