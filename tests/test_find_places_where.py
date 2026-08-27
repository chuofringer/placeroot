"""find_places' LocationRef point mode: `where` (issue #420).

`where` is a fourth way to give the SEARCH CENTER — {"lat","lon"} dict |
GERS id | free-text name — the same LocationRef summarize_area and
isochrone already take, and the reason a named search no longer needs the
separate find_near hop. Everything downstream of the resolve (the scan,
within's "of" default, the cursor) sees an ordinary numeric center, so
these tests mostly check the seams: mutual exclusion, the compact
"resolved" echo, and that the existing call shapes did not move.
"""

import asyncio

from placeroot import geocode, server

from .conftest import CENTER_LAT, CENTER_LON
from .test_find_places_in_area import (
    DIV_NOTCH,
    polygon_fixtures,  # noqa: F401
)
from .test_gers_lookup import PLACE_ID

NAMED_PLACE = "Blue Bottle Roastery"


def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


# --- the center, given four ways -------------------------------------------


def test_where_name_centers_the_search_and_echoes_the_match():
    result = server.find_places(where=NAMED_PLACE, category="coffee_shop")
    assert "error" not in result
    assert result["results"]
    assert result["resolved"]["name"] == NAMED_PLACE
    assert result["resolved"]["matched_by"] == "name"
    assert result["resolved"]["lat"] and result["resolved"]["lon"]


def test_where_name_matches_find_near_for_the_same_inputs():
    """The acceptance criterion: find_places(where=...) IS find_near, modulo
    find_near's own compose extras (its `near`/`category` echo and its
    smaller row projection)."""
    via_where = server.find_places(where=NAMED_PLACE, category="coffee_shop", detail="full")
    via_find_near = server.find_near("coffee_shop", NAMED_PLACE)
    assert "error" not in via_where and "error" not in via_find_near
    assert [r["id"] for r in via_where["results"]] == [r["id"] for r in via_find_near["results"]]
    assert via_where["resolved"]["name"] == via_find_near["near"]["name"]


def test_where_gers_id_centers_the_search_and_echoes_the_match():
    """Real fixture GERS id (tests/test_gers_lookup.py) — no monkeypatch."""
    result = server.find_places(where=PLACE_ID, radius_m=1000)
    assert "error" not in result
    assert result["resolved"]["matched_by"] == "gers_id"
    assert result["resolved"]["id"] == PLACE_ID


def test_where_coordinate_dict_adds_no_resolved_block():
    """Raw coordinate input must not grow the answer (_resolve_location_ref's
    contract) — the same rule summarize_area/isochrone follow."""
    result = server.find_places(where={"lat": CENTER_LAT, "lon": CENTER_LON}, radius_m=200)
    assert "error" not in result
    assert "resolved" not in result
    assert (
        result["results"]
        == server.find_places(lat=CENTER_LAT, lon=CENTER_LON, radius_m=200)["results"]
    )


def test_where_ambiguous_name_returns_candidates():
    result = server.find_places(where="London", category="coffee_shop")
    assert result["error"] == "ambiguous_place"
    ids = {c["id"] for c in result["candidates"]}
    assert {"gers-div-london-oh", "gers-div-london-on"} <= ids


def test_where_unresolvable_name_is_not_found(monkeypatch):
    monkeypatch.setattr(geocode, "resolve_named_place", lambda *_a, **_k: None)
    result = server.find_places(where="Not A Real Place 9z")
    assert result["error"] == "not_found"
    assert result["try"]


def test_where_malformed_ref_is_bad_request():
    assert server.find_places(where={"lat": 91.0, "lon": 0.0})["error"] == "bad_request"
    assert server.find_places(where="   ")["error"] == "bad_request"


# --- mutual exclusion ------------------------------------------------------


def test_where_with_latlon_is_bad_request():
    result = server.find_places(lat=CENTER_LAT, lon=CENTER_LON, where=NAMED_PLACE)
    assert result["error"] == "bad_request"
    assert "where" in result["detail"] and "lat" in result["detail"]


def test_where_with_area_is_bad_request():
    result = server.find_places(where=NAMED_PLACE, area="Palo Alto")
    assert result["error"] == "bad_request"
    assert "area" in result["detail"]


def test_where_with_division_id_is_bad_request():
    result = server.find_places(where=NAMED_PLACE, division_id="gers-div-brooklyn")
    assert result["error"] == "bad_request"
    assert "division_id" in result["detail"]


def test_no_mode_at_all_still_names_where_as_an_option():
    result = server.find_places()
    assert result["error"] == "bad_request"
    assert "where" in result["detail"]


# --- within.of defaults to the center, however the center was given --------


def test_within_of_defaults_to_wheres_center(monkeypatch):
    """within's "of defaults to the search center" contract has to hold for a
    center given as `where`, not just a raw lat/lon."""
    pin = geocode.resolve_named_place(NAMED_PLACE)
    calls = []

    def fake_isochrone(lat, lon, minutes=15, mode="walk", **kw):
        calls.append((lat, lon, minutes, mode))
        return {
            "polygon": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [pin["lon"] - 0.01, pin["lat"] - 0.01],
                        [pin["lon"] + 0.01, pin["lat"] - 0.01],
                        [pin["lon"] + 0.01, pin["lat"] + 0.01],
                        [pin["lon"] - 0.01, pin["lat"] + 0.01],
                        [pin["lon"] - 0.01, pin["lat"] - 0.01],
                    ]
                ],
            },
            "stats": {},
        }

    monkeypatch.setattr(server.routing, "isochrone", fake_isochrone)
    monkeypatch.setattr(server.routing, "isochrone_graph_is_cached", lambda *_a, **_k: True)
    result = server.find_places(where=NAMED_PLACE, within={"minutes": 10})
    assert "error" not in result
    assert calls == [(pin["lat"], pin["lon"], 10.0, "walk")]
    # The center's own echo owns "resolved"; `of` was never given.
    assert result["resolved"]["name"] == NAMED_PLACE


def test_within_of_given_as_a_name_alongside_where_moves_into_the_note(monkeypatch):
    """Two named points, one "resolved" key: the center keeps it and `of`'s
    match is named in the note instead of being dropped."""
    monkeypatch.setattr(
        server.routing,
        "isochrone",
        lambda lat, lon, minutes=15, mode="walk", **kw: {
            "polygon": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [lon - 0.01, lat - 0.01],
                        [lon + 0.01, lat - 0.01],
                        [lon + 0.01, lat + 0.01],
                        [lon - 0.01, lat + 0.01],
                        [lon - 0.01, lat - 0.01],
                    ]
                ],
            },
            "stats": {},
        },
    )
    monkeypatch.setattr(server.routing, "isochrone_graph_is_cached", lambda *_a, **_k: True)
    result = server.find_places(where=NAMED_PLACE, within={"minutes": 10, "of": "Brooklyn"})
    assert "error" not in result
    assert result["resolved"]["name"] == NAMED_PLACE
    assert "Brooklyn" in result["note"]


# --- regressions: the existing call shapes ---------------------------------


def test_existing_point_and_division_shapes_are_unchanged(polygon_fixtures):  # noqa: F811
    point = server.find_places(lat=CENTER_LAT, lon=CENTER_LON, radius_m=300)
    assert "error" not in point
    assert "resolved" not in point
    division = server.find_places(division_id=DIV_NOTCH)
    assert "error" not in division
    assert "resolved" not in division


# --- published schema ------------------------------------------------------


def test_find_places_schema_publishes_where_as_a_locationref():
    props = _tools()["find_places"].input_schema["properties"]
    assert "where" in props
    types = {branch.get("type") for branch in props["where"]["anyOf"]}
    assert {"string", "object"} <= types


def test_deprecation_notes_point_at_the_canonical_tools():
    tools = _tools()
    assert "find_places(where=" in tools["find_near"].description
    assert "route" in tools["from_to"].description.split("\n\n")[1]
