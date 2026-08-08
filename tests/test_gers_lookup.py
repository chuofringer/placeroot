"""gers_lookup: one GERS id -> the entity it names, across themes (issue #173).

Every test runs against the committed fixtures wired up by conftest's
offline_data: places.parquet, divisions.parquet (type=division, the
division *entities*), division_areas.parquet (the polygons admin_lookup
tests containment against) and buildings.parquet. The fixture geographies
overlap on purpose — the places and buildings sit inside the "Downtown"
division polygon — which is what makes the containing-division join
assertable here at all.
"""

import asyncio

import pytest

from placeroot import gers, overture, server

# A place, a division and a building that all exist in the fixtures, all
# within the Downtown polygon (-73.905..-73.895, 40.695..40.705).
PLACE_ID = "41b764a2b9ea71e97088d733d7f5898c"       # "Cluster Place 000"
DIVISION_ID = "gers-div-brooklyn"                    # locality, US-NY
BUILDING_ID = "394aca62078e6fb90fe1879e6e78e990"     # residential house
DOWNTOWN_ID = "2835be088c8011a4aee3dff5cabbcf13"     # containing neighborhood

NEAR_LAT, NEAR_LON = 40.6996, -73.9006


def test_place_id_resolves_to_places_theme():
    result = gers.gers_lookup(PLACE_ID)
    assert result["id"] == PLACE_ID
    assert result["theme"] == "places"
    assert result["type"] == "place"
    assert result["name"] == "Cluster Place 000"
    assert result["lat"] == pytest.approx(40.6996, abs=1e-3)
    assert result["lon"] == pytest.approx(-73.9006, abs=1e-3)
    assert result["summary"]["category"]
    assert result["summary"]["confidence"] is not None


def test_place_id_carries_containing_division_and_building():
    related = gers.gers_lookup(PLACE_ID)["related"]
    assert related["division_id"] == DOWNTOWN_ID
    assert related["division_name"] == "Downtown"
    assert related["division_type"] == "neighborhood"
    # The building join is places-only: the fixture buildings sit within
    # buildings.DEFAULT_NEAREST_RADIUS_M of the fixture places.
    assert related["building_id"]
    assert related["building_distance_m"] < 100


def test_division_id_resolves_with_subtype_and_country():
    result = gers.gers_lookup(DIVISION_ID)
    assert result["theme"] == "divisions"
    assert result["type"] == "division"
    assert result["name"] == "Brooklyn"
    assert result["summary"] == {"subtype": "locality", "country": "US", "region": "US-NY"}
    assert result["related"]["division_id"] == DOWNTOWN_ID
    # No building join for a division — that relation is meaningless.
    assert "building_id" not in result["related"]


def test_building_id_resolves_with_class_and_height():
    result = gers.gers_lookup(BUILDING_ID)
    assert result["theme"] == "buildings"
    assert result["type"] == "building"
    assert result["name"] is None
    assert result["summary"]["class"] == "house"
    assert result["summary"]["height_m"] == pytest.approx(3.2)
    assert result["related"]["division_id"] == DOWNTOWN_ID
    assert "building_id" not in result["related"]


def test_summary_omits_fields_the_row_has_no_value_for():
    # This fixture building has height/num_floors but a null subtype/class.
    result = gers.gers_lookup("61d9228976b850d4ba7d2433918fb0ab")
    assert result["summary"] == {"height_m": pytest.approx(12.8), "num_floors": 4}


def test_no_result_carries_geometry():
    for id_ in (PLACE_ID, DIVISION_ID, BUILDING_ID):
        result = gers.gers_lookup(id_)
        assert "geometry" not in result
        assert "geometry" not in result["summary"]


@pytest.mark.parametrize("id_", [PLACE_ID, DIVISION_ID, BUILDING_ID])
def test_near_hint_returns_the_same_entity_as_the_unhinted_lookup(id_):
    hinted = gers.gers_lookup(id_, near_lat=NEAR_LAT, near_lon=NEAR_LON)
    assert hinted == gers.gers_lookup(id_)


def test_hint_that_misses_still_resolves_via_the_full_scan_fallback():
    """A stale hint must never manufacture a false not_found (see module docstring)."""
    # São Paulo — nowhere near the fixture cluster, so every probe's 50km
    # box misses and each has to fall back to the unbounded scan.
    result = gers.gers_lookup(BUILDING_ID, near_lat=-23.55, near_lon=-46.63)
    assert result is not None
    assert result["theme"] == "buildings"


def test_unknown_id_returns_none():
    assert gers.gers_lookup("00000000000000000000000000000000") is None


@pytest.mark.parametrize("bad", ["", "   ", "x" * (gers.MAX_ID_LENGTH + 1)])
def test_malformed_id_raises_value_error(bad):
    with pytest.raises(ValueError):
        gers.gers_lookup(bad)


def test_non_string_id_raises_value_error():
    with pytest.raises(ValueError):
        gers.gers_lookup(None)


def test_id_is_trimmed_before_lookup():
    assert gers.gers_lookup(f"  {PLACE_ID}  ")["id"] == PLACE_ID


def test_place_probe_runs_before_the_building_probe(monkeypatch):
    """Probe order is places -> divisions -> buildings, short-circuiting on the first hit."""
    probed = []

    def record(name, fn):
        def wrapper(id, near_lat, near_lon):
            probed.append(name)
            return fn(id, near_lat, near_lon)
        return wrapper

    monkeypatch.setattr(gers, "_PROBES", tuple(
        record(n, f) for n, f in (
            ("places", gers._probe_places),
            ("divisions", gers._probe_divisions),
            ("buildings", gers._probe_buildings),
        )
    ))
    gers.gers_lookup(PLACE_ID)
    assert probed == ["places"]

    probed.clear()
    gers.gers_lookup(BUILDING_ID)
    assert probed == ["places", "divisions", "buildings"]


def test_related_joins_degrade_instead_of_failing_the_lookup(monkeypatch):
    """An outage in a related join costs the join, not the entity."""
    def boom(lat, lon):
        raise overture.UpstreamUnavailable("divisions theme down")

    monkeypatch.setattr(gers.divisions, "admin_lookup", boom)
    result = gers.gers_lookup(DIVISION_ID)
    assert result["name"] == "Brooklyn"
    assert result["related"] == {}


def test_upstream_failure_propagates(monkeypatch):
    def boom(id, near_lat, near_lon):
        raise overture.UpstreamUnavailable("s3 unreachable")

    monkeypatch.setattr(gers, "_PROBES", (boom,))
    with pytest.raises(overture.UpstreamUnavailable):
        gers.gers_lookup(PLACE_ID)


def test_schema_degraded_only_when_every_theme_is_degraded(monkeypatch):
    def degraded(id, near_lat, near_lon):
        raise overture.SchemaDegraded(["id"])

    def finds_it(id, near_lat, near_lon):
        return {"theme": "divisions", "type": "division", "name": "X", "lat": None,
                "lon": None, "summary": {}}

    monkeypatch.setattr(gers, "_PROBES", (degraded, finds_it))
    assert gers.gers_lookup(PLACE_ID)["name"] == "X"

    monkeypatch.setattr(gers, "_PROBES", (degraded, degraded))
    with pytest.raises(overture.SchemaDegraded):
        gers.gers_lookup(PLACE_ID)


# --- server tool boundary ---------------------------------------------------


def test_server_tool_returns_the_entity():
    result = server.gers_lookup(PLACE_ID)
    assert "error" not in result
    assert result["theme"] == "places"
    assert result["related"]["division_name"] == "Downtown"


def test_server_tool_unknown_id_is_not_found():
    result = server.gers_lookup("00000000000000000000000000000000")
    assert result["error"] == "not_found"
    assert "segments" in result["detail"]


def test_server_tool_malformed_id_is_bad_request():
    assert server.gers_lookup("")["error"] == "bad_request"


def test_server_tool_swapped_near_coords_is_bad_request():
    # Los Angeles with lat/lon swapped: -118.24 is not a valid latitude.
    result = server.gers_lookup(PLACE_ID, near_lat=-118.24, near_lon=34.05)
    assert result["error"] == "bad_request"
    assert "swap" in result["detail"]


def test_server_tool_upstream_error_is_structured(monkeypatch):
    def boom(id, near_lat=None, near_lon=None):
        raise overture.UpstreamUnavailable("s3 unreachable")

    monkeypatch.setattr(server.gers, "gers_lookup", boom)
    result = server.gers_lookup(PLACE_ID)
    assert result["error"] == "upstream_unavailable"
    assert result["retry_advised"] is True


def test_server_tool_schema_error_is_structured(monkeypatch):
    def boom(id, near_lat=None, near_lon=None):
        raise overture.SchemaDegraded(["id"])

    monkeypatch.setattr(server.gers, "gers_lookup", boom)
    result = server.gers_lookup(PLACE_ID)
    assert result["error"] == "schema_degraded"
    assert result["missing_columns"] == ["id"]


def test_gers_lookup_is_registered_over_mcp():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "gers_lookup" in names
