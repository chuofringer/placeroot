"""LocationRef, wave 1 (docs/ROADMAP.md #4.1 / #5.1).

One location argument, everywhere: {"lat", "lon"} dict | GERS id string |
free-text name string, purely additive over the existing {lat, lon}
spelling. `server._resolve_location_ref`/`_resolve_location_refs` are the
shared machinery every widened tool (optimize_route, compare_areas,
isochrone, summarize_area, from_to) routes through; this file exercises
that machinery directly. Per-tool integration (the `resolved`/`from`/`to`
echo shape, the mutual-exclusion checks, byte-identical coordinate answers)
lives in each tool's own test file.

The GERS path is tested against PLACE_ID, a real 32-hex id in the committed
places.parquet fixture (see tests/test_gers_lookup.py). The name path is
tested with geocode.resolve_named_place monkeypatched, mirroring
tests/test_from_to.py's _hit/_ab pattern — offline, no live geocoding.
"""

import pytest

from placeroot import errors, geocode, server

from .conftest import CENTER_LAT, CENTER_LON
from .test_gers_lookup import PLACE_ID

# 32 lowercase hex characters, shaped like a real GERS id, but not one any
# fixture theme claims.
UNKNOWN_GERS_ID = "0" * 32


# --------------------------------------------------------------------
# _resolve_location_ref: single ref
# --------------------------------------------------------------------


def test_coordinate_dict_passes_through_untouched():
    item, err = server._resolve_location_ref({"lat": CENTER_LAT, "lon": CENTER_LON})
    assert err is None
    assert item == {"lat": CENTER_LAT, "lon": CENTER_LON}
    assert "matched_by" not in item
    assert "id" not in item
    assert "name" not in item


def test_gers_id_resolves_via_gers_lookup():
    item, err = server._resolve_location_ref(PLACE_ID)
    assert err is None
    assert item["matched_by"] == "gers_id"
    assert item["id"] == PLACE_ID
    assert isinstance(item["lat"], float)
    assert isinstance(item["lon"], float)


def test_gers_shaped_id_matching_nothing_is_not_found():
    item, err = server._resolve_location_ref(UNKNOWN_GERS_ID)
    assert item is None
    assert err["error"] == "not_found"
    assert "GERS id" in err["detail"]


def test_free_text_name_resolves_via_resolve_named_place(monkeypatch):
    def fake_resolve(query):
        assert query == "Cluster Place 000"
        return {
            "name": query, "lat": CENTER_LAT, "lon": CENTER_LON,
            "id": "gers-cluster", "type": "place",
        }

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    item, err = server._resolve_location_ref("Cluster Place 000")
    assert err is None
    assert item == {
        "id": "gers-cluster", "name": "Cluster Place 000",
        "lat": CENTER_LAT, "lon": CENTER_LON, "matched_by": "name",
    }


def test_ambiguous_name_becomes_ambiguous_place_error(monkeypatch):
    def fake_resolve(query):
        raise errors.AmbiguousPlace(query, candidates=[{"name": "A"}, {"name": "B"}])

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    item, err = server._resolve_location_ref("Springfield")
    assert item is None
    assert err["error"] == "ambiguous_place"
    assert err["candidates"] == [{"name": "A"}, {"name": "B"}]


def test_unresolvable_name_is_not_found(monkeypatch):
    monkeypatch.setattr(geocode, "resolve_named_place", lambda query: None)
    item, err = server._resolve_location_ref("Nowhere At All")
    assert item is None
    assert err["error"] == "not_found"


@pytest.mark.parametrize(
    "ref",
    [
        {},
        {"lat": 91.0, "lon": 0.0},
        {"lat": "north", "lon": 0.0},
        {"lat": CENTER_LAT},
        "",
        "   ",
        123,
        None,
        [1, 2],
    ],
)
def test_garbage_refs_are_bad_request(ref):
    item, err = server._resolve_location_ref(ref)
    assert item is None
    assert err["error"] == "bad_request"
    assert "GERS id" in err["detail"]
    assert "lat" in err["detail"]


# --------------------------------------------------------------------
# _resolve_location_refs: list, indexed errors, parallel resolution
# --------------------------------------------------------------------


def test_list_resolves_mixed_coords_and_names_in_order(monkeypatch):
    def fake_resolve(query):
        return {
            "name": query, "lat": CENTER_LAT + 0.001, "lon": CENTER_LON,
            "id": "gers-y", "type": "place",
        }

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    refs = [{"lat": CENTER_LAT, "lon": CENTER_LON}, "Some Name"]
    resolved, err = server._resolve_location_refs(refs, "stops")
    assert err is None
    assert "matched_by" not in resolved[0]
    assert resolved[1]["matched_by"] == "name"
    assert resolved[1]["name"] == "Some Name"


def test_list_error_carries_index_and_param_prefix(monkeypatch):
    def fake_resolve(query):
        if query == "Bad":
            raise errors.AmbiguousPlace(query, candidates=[{"name": "X"}, {"name": "Y"}])
        return {"name": query, "lat": CENTER_LAT, "lon": CENTER_LON, "id": "g", "type": "place"}

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    resolved, err = server._resolve_location_refs(["Good", "Bad"], "stops")
    assert resolved is None
    assert err["error"] == "ambiguous_place"
    assert err["index"] == 1
    assert err["detail"].startswith("stops[1]: ")
    assert err["candidates"] == [{"name": "X"}, {"name": "Y"}]


def test_list_reports_lowest_failing_index_deterministically(monkeypatch):
    def fake_resolve(query):
        if query in ("A", "C"):
            return None
        return {"name": query, "lat": CENTER_LAT, "lon": CENTER_LON, "id": "g", "type": "place"}

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    resolved, err = server._resolve_location_refs(["A", "B", "C"], "areas")
    assert resolved is None
    assert err["index"] == 0
    assert err["detail"].startswith("areas[0]: ")


def test_refs_not_a_list_is_bad_request():
    resolved, err = server._resolve_location_refs("not-a-list", "areas")
    assert resolved is None
    assert err["error"] == "bad_request"
