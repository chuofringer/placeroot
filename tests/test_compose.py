"""Named-place compose: from_to and find_near (issue #328).

Public tools take names, resolve inside the server, and return the
resolved pin on the answer. Ambiguous names return candidates.
"""

import asyncio
import re

import pytest

from placeroot import geocode, server
from placeroot.errors import AmbiguousPlace

from ._routing_fixture import build_routing_fixture as fx

FROM_LAT, FROM_LON = fx.node_latlon(2, 2)
TO_LAT, TO_LON = fx.node_latlon(2, 5)


def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


# --- schema: names, not coordinates ----------------------------------------


def test_publish_from_keyword_fails_loudly_if_mcp_internals_move():
    class _Broken:
        pass

    with pytest.raises(AssertionError, match="from_to schema patch failed; mcp internals changed"):
        server._publish_from_keyword(_Broken())


def test_from_to_schema_accepts_from_and_to_as_locationref():
    # LocationRef wave 1 (roadmap #4.1): from/to widened to str | {lat,lon}
    # dict (a GERS id travels as the string arm) — no longer bare strings.
    schema = _tools()["from_to"].input_schema
    props = schema["properties"]
    required = set(schema.get("required") or [])
    for name in ("from", "to"):
        assert name in props
        types = {branch.get("type") for branch in props[name]["anyOf"]}
        assert types == {"string", "object"}
    assert required >= {"from", "to"}
    assert "from_" not in props
    for coord in ("from_lat", "from_lon", "to_lat", "to_lon", "lat", "lon"):
        assert coord not in props


def test_find_near_schema_requires_category_and_near_as_strings():
    schema = _tools()["find_near"].input_schema
    props = schema["properties"]
    required = set(schema.get("required") or [])
    assert "category" in props and props["category"].get("type") == "string"
    assert "near" in props and props["near"].get("type") == "string"
    assert required >= {"category", "near"}
    for coord in ("lat", "lon", "near_lat", "near_lon"):
        assert coord not in props


def test_descriptions_tell_the_agent_to_pass_names_not_look_them_up():
    tools = _tools()
    for name in ("from_to", "find_near"):
        desc = tools[name].description
        flat = re.sub(r"\s+", " ", desc)
        assert "place name" in flat.lower()
        assert "Do not call geocode(), resolve_place(), or geocode_batch() first." in flat


def test_no_coord_overrides_on_either_tool():
    tools = _tools()
    for name in ("from_to", "find_near"):
        props = tools[name].input_schema["properties"]
        assert not any("lat" in k or "lon" in k for k in props)


# --- resolution / ambiguity ------------------------------------------------


def test_unique_name_resolves():
    resolved = geocode.resolve_named_place("Brooklyn")
    assert resolved["name"] == "Brooklyn"
    assert resolved["id"] == "gers-div-brooklyn"
    assert "lat" in resolved and "lon" in resolved


def test_place_name_resolves():
    resolved = geocode.resolve_named_place("Blue Bottle Roastery")
    assert resolved["name"] == "Blue Bottle Roastery"
    assert resolved["lat"]
    assert resolved["lon"]


def test_ambiguous_name_raises_with_candidates():
    with pytest.raises(AmbiguousPlace) as excinfo:
        geocode.resolve_named_place("London")
    ids = {c["id"] for c in excinfo.value.candidates}
    assert ids == {"gers-div-london-oh", "gers-div-london-on"}
    assert all("lat" in c and "lon" in c for c in excinfo.value.candidates)


def test_from_to_ambiguous_from_returns_candidates():
    result = server.from_to(from_="London", to="Brooklyn")
    assert result["error"] == "ambiguous_place"
    assert result["field"] == "from"
    ids = {c["id"] for c in result["candidates"]}
    assert "gers-div-london-oh" in ids
    assert "gers-div-london-on" in ids


def test_find_near_ambiguous_near_returns_candidates():
    result = server.find_near("coffee_shop", "London")
    assert result["error"] == "ambiguous_place"
    assert result["field"] == "near"
    assert len(result["candidates"]) >= 2


def test_unresolvable_name_is_not_found():
    result = server.from_to(from_="Definitely Not A Real Place XYZ", to="Brooklyn")
    assert result["error"] == "not_found"
    assert result["field"] == "from"


def test_empty_names_are_bad_request():
    assert server.from_to(from_="   ", to="Brooklyn")["error"] == "bad_request"
    assert server.find_near("coffee_shop", "  ")["error"] == "bad_request"
    assert server.find_near("  ", "Brooklyn")["error"] == "bad_request"


# --- happy path ------------------------------------------------------------


def test_from_to_happy_path_returns_resolved_ends_and_export(monkeypatch):
    origin = {
        "name": "Ferry Building",
        "lat": FROM_LAT,
        "lon": FROM_LON,
        "id": "a",
        "type": "place",
    }
    dest = {"name": "Dolores Park", "lat": TO_LAT, "lon": TO_LON, "id": "b", "type": "place"}

    def fake_resolve(query):
        return origin if "Ferry" in query else dest

    monkeypatch.setattr(server, "_resolve_named_place", fake_resolve)
    result = server.from_to(from_="Ferry Building", to="Dolores Park", confirm=True)
    assert "error" not in result
    assert result["distance_m"] > 0
    assert result["mode"] == "walk"
    assert result["from"]["name"] == "Ferry Building"
    assert result["to"]["name"] == "Dolores Park"
    assert result["from"]["lat"] == FROM_LAT
    assert result["to"]["lon"] == TO_LON
    assert "export" in result


def test_from_to_city_apart_is_too_far_not_a_planet_scan():
    result = server.from_to(from_="Brooklyn", to="Springfield")
    assert result["error"] == "too_far"
    assert "from" in result and "to" in result
    assert result["from"]["name"]
    assert result["to"]["name"]
    assert result["max_distance_m"]


def test_find_near_resolves_pin_and_returns_rows():
    result = server.find_near("coffee_shop", "Blue Bottle Roastery", radius_m=2000)
    assert "error" not in result
    assert result["near"]["name"] == "Blue Bottle Roastery"
    assert "lat" in result["near"] and "lon" in result["near"]
    assert "results" in result
    assert result["category"] == "coffee_shop"


def test_find_near_maps_free_text_category():
    result = server.find_near("coffee", "Blue Bottle Roastery", radius_m=2000)
    assert "error" not in result
    assert result["category"] != "coffee" or result.get("category_resolved_from") == "coffee"


def test_from_to_default_mode_is_walk(monkeypatch):
    seen = {}

    def fake_resolve(query):
        lat, lon = (FROM_LAT, FROM_LON) if query == "A" else (TO_LAT, TO_LON)
        return {"name": query, "lat": lat, "lon": lon, "id": query, "type": "place"}

    monkeypatch.setattr(server, "_resolve_named_place", fake_resolve)
    result = server.from_to(from_="A", to="B", confirm=True)
    seen["mode"] = result.get("mode")
    assert result["mode"] == "walk"
