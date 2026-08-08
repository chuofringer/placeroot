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

# The containment chain over the fixture centre, smallest first. Downtown and
# Metropolis exist as division *entities* as well as division_area polygons,
# sharing one GERS id across the two fixtures the way real Overture data does
# — which is what lets a division's own entry appear in its own chain.
DOWNTOWN_ID = "2835be088c8011a4aee3dff5cabbcf13"     # neighborhood
METROPOLIS_ID = "9e34d836dceb18e1254bed9c0a40d455"   # locality, contains Downtown
FRANKLIN_COUNTY_ID = "e54421a2777c4e19bee68decbc66e8ee"  # county, contains Metropolis

NEAR_LAT, NEAR_LON = 40.6996, -73.9006
# Far from every fixture row: São Paulo. Any probe hinted here misses.
FAR_LAT, FAR_LON = -23.55, -46.63


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
    # No building join for a division — that relation is meaningless.
    assert "building_id" not in result["related"]


def test_division_relates_to_its_parent_not_to_a_child():
    """The containing division of a division is the chain entry AFTER it.

    Metropolis is a locality whose reference point also falls inside the
    Downtown neighborhood, so its chain is [Downtown, Metropolis, Franklin
    County, ...]. Skipping only the self entry and taking the next would
    hand back Downtown — a division Metropolis *contains*, reported as the
    division containing it.
    """
    related = gers.gers_lookup(METROPOLIS_ID)["related"]
    assert related["division_id"] == FRANKLIN_COUNTY_ID
    assert related["division_name"] == "Franklin County"
    assert related["division_type"] == "county"
    assert related["division_id"] != DOWNTOWN_ID


def test_smallest_division_in_the_chain_relates_to_the_next_one_up():
    related = gers.gers_lookup(DOWNTOWN_ID)["related"]
    assert related["division_id"] == METROPOLIS_ID
    assert related["division_type"] == "locality"


def test_division_absent_from_the_containment_chain_has_no_related_division():
    """No container we can name honestly beats naming the wrong one.

    gers-div-brooklyn is a division entity with no division_area polygon of
    its own, so it never appears in the chain over its own point — there is
    no "entry after self" to report.
    """
    assert gers.gers_lookup(DIVISION_ID)["related"] == {}


def test_top_of_the_chain_has_no_related_division(monkeypatch):
    """A country is the last chain entry; nothing contains it."""
    monkeypatch.setattr(gers.divisions, "admin_lookup", lambda lat, lon: {
        "chain": [{"id": DOWNTOWN_ID, "name": "Downtown", "type": "neighborhood"}]
    })
    assert gers.gers_lookup(DOWNTOWN_ID)["related"] == {}


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


def test_hint_that_misses_bounds_the_search_instead_of_scanning(monkeypatch):
    """A hint is a boundary, not a preference — see the module docstring.

    The old contract fell back to an unbounded scan per theme, which for
    buildings is a multi-billion-row read triggered by one stale coordinate.
    """
    scans = _count_unbounded_scans(monkeypatch)
    assert gers.gers_lookup(BUILDING_ID, near_lat=FAR_LAT, near_lon=FAR_LON) is None
    assert scans == []


def test_the_same_id_still_resolves_without_the_hint():
    """The escape hatch the hint-miss note points at actually works."""
    assert gers.gers_lookup(BUILDING_ID)["theme"] == "buildings"


def _count_unbounded_scans(monkeypatch) -> list[str]:
    """Records every id query issued without a bbox predicate, and returns the list.

    Two chokepoints cover all three probes: gers._run_id_query (divisions,
    buildings) and overture._run_place_details_query (places, reached via
    place_details' id path).
    """
    unbounded: list[str] = []

    real_id_query = gers._run_id_query

    def id_query_spy(select_sql, from_source, params, bbox_filter):
        if bbox_filter is None:
            unbounded.append(from_source)
        return real_id_query(select_sql, from_source, params, bbox_filter)

    real_place_query = overture._run_place_details_query

    def place_query_spy(from_source, filters, order_expr, params, missing):
        if not any("bbox" in f for f in filters):
            unbounded.append(from_source)
        return real_place_query(from_source, filters, order_expr, params, missing)

    monkeypatch.setattr(gers, "_run_id_query", id_query_spy)
    monkeypatch.setattr(overture, "_run_place_details_query", place_query_spy)
    return unbounded


def test_unknown_id_returns_none():
    assert gers.gers_lookup("00000000000000000000000000000000") is None


def test_repeat_unknown_id_is_served_from_the_negative_cache(monkeypatch):
    junk = "deadbeefdeadbeefdeadbeefdeadbeef"
    assert gers.gers_lookup(junk) is None

    def boom(id, near_lat, near_lon):
        raise AssertionError("a cached miss must not re-probe any theme")

    monkeypatch.setattr(gers, "_PROBES", (("places", boom),))
    assert gers.gers_lookup(junk) is None


def test_a_hinted_miss_is_not_cached_as_a_miss():
    """Otherwise the hint-miss note would send callers to a poisoned answer."""
    assert gers.gers_lookup(BUILDING_ID, near_lat=FAR_LAT, near_lon=FAR_LON) is None
    assert gers.gers_lookup(BUILDING_ID)["theme"] == "buildings"


@pytest.mark.parametrize("bad", ["", "   ", "x" * (gers.MAX_ID_LENGTH + 1)])
def test_malformed_id_raises_value_error(bad):
    with pytest.raises(ValueError):
        gers.gers_lookup(bad)


@pytest.mark.parametrize("bad", [
    "not an id",                      # whitespace inside
    "41b764a2b9ea71e97088d733d7f5898c'; DROP TABLE",
    "s3://bucket/places/*.parquet",   # a path, not an id
    "café",                           # non-ASCII
    "id.with.dots",
])
def test_id_that_cannot_be_a_gers_id_raises_value_error(bad):
    with pytest.raises(ValueError, match="not a GERS id"):
        gers.gers_lookup(bad)


@pytest.mark.parametrize("ok", [PLACE_ID, DIVISION_ID, "A_B-c9"])
def test_id_gate_admits_real_and_fixture_id_shapes(ok):
    assert gers._validate_id(ok) == ok


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
        (theme, record(theme, fn)) for theme, fn in gers._PROBES
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
    assert result["related"] == {"note": gers.RELATED_UNAVAILABLE_NOTE}


def test_a_broken_related_join_is_distinguishable_from_no_containing_division():
    """{} means "in no division"; only an outage carries a note."""
    assert "note" not in gers.gers_lookup(DIVISION_ID)["related"]


def test_a_broken_building_join_notes_itself_without_losing_the_division(monkeypatch):
    def boom(lat, lon, radius_m=None, limit=None):
        raise overture.UpstreamUnavailable("buildings theme down")

    monkeypatch.setattr(gers.buildings, "buildings_at", boom)
    related = gers.gers_lookup(PLACE_ID)["related"]
    assert related["division_id"] == DOWNTOWN_ID
    assert related["note"] == gers.BUILDING_UNAVAILABLE_NOTE


def test_one_theme_being_down_does_not_stop_a_later_theme_resolving(monkeypatch):
    """Probe 1 failing must not mask an id probe 2 would have resolved."""
    def boom(id, near_lat, near_lon):
        raise overture.UpstreamUnavailable("places theme down")

    monkeypatch.setattr(gers, "_PROBES", tuple(
        (theme, boom if theme == "places" else fn) for theme, fn in gers._PROBES
    ))
    result = gers.gers_lookup(DIVISION_ID)
    assert result["theme"] == "divisions"
    assert result["name"] == "Brooklyn"


def test_a_miss_plus_a_failure_is_upstream_unavailable_not_not_found(monkeypatch):
    """An unchecked theme might have owned the id — that is not a not_found."""
    def boom(id, near_lat, near_lon):
        raise overture.UpstreamUnavailable("buildings theme down")

    monkeypatch.setattr(gers, "_PROBES", tuple(
        (theme, boom if theme == "buildings" else fn) for theme, fn in gers._PROBES
    ))
    with pytest.raises(overture.UpstreamUnavailable, match="buildings"):
        gers.gers_lookup("00000000000000000000000000000000")


def test_a_failed_lookup_is_not_negatively_cached(monkeypatch):
    """A miss we could not confirm must not answer the retry."""
    def boom(id, near_lat, near_lon):
        raise overture.UpstreamUnavailable("buildings theme down")

    monkeypatch.setattr(gers, "_PROBES", tuple(
        (theme, boom if theme == "buildings" else fn) for theme, fn in gers._PROBES
    ))
    with pytest.raises(overture.UpstreamUnavailable):
        gers.gers_lookup(BUILDING_ID)
    monkeypatch.undo()
    assert gers.gers_lookup(BUILDING_ID)["theme"] == "buildings"


def test_upstream_failure_propagates(monkeypatch):
    def boom(id, near_lat, near_lon):
        raise overture.UpstreamUnavailable("s3 unreachable")

    monkeypatch.setattr(gers, "_PROBES", (("places", boom),))
    with pytest.raises(overture.UpstreamUnavailable):
        gers.gers_lookup(PLACE_ID)


def test_schema_degraded_only_when_every_theme_is_degraded(monkeypatch):
    def degraded(id, near_lat, near_lon):
        raise overture.SchemaDegraded(["id"])

    def finds_it(id, near_lat, near_lon):
        return {"theme": "divisions", "type": "division", "name": "X", "lat": None,
                "lon": None, "summary": {}}

    monkeypatch.setattr(gers, "_PROBES", (("places", degraded), ("divisions", finds_it)))
    assert gers.gers_lookup(PLACE_ID)["name"] == "X"

    monkeypatch.setattr(gers, "_PROBES", (("places", degraded), ("divisions", degraded)))
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


def test_server_tool_hinted_miss_explains_the_bound():
    result = server.gers_lookup(BUILDING_ID, near_lat=FAR_LAT, near_lon=FAR_LON)
    assert result["error"] == "not_found"
    assert result["note"] == gers.HINT_MISS_NOTE


def test_server_tool_unhinted_miss_carries_no_hint_note():
    assert "note" not in server.gers_lookup("00000000000000000000000000000000")


def test_server_tool_malformed_id_is_bad_request():
    assert server.gers_lookup("")["error"] == "bad_request"


def test_server_tool_id_with_illegal_characters_is_bad_request():
    result = server.gers_lookup("SELECT * FROM places")
    assert result["error"] == "bad_request"
    assert "GERS id" in result["detail"]


def test_server_tool_reports_degraded_fields_for_the_resolved_theme(monkeypatch):
    monkeypatch.setattr(overture, "degraded_fields", lambda: ["websites"])
    monkeypatch.setattr(server.buildings, "degraded_fields", lambda: ["num_floors"])

    assert server.gers_lookup(PLACE_ID)["degraded_fields"] == ["websites"]
    assert server.gers_lookup(BUILDING_ID)["degraded_fields"] == ["num_floors"]
    # A division answer draws on neither dataset's optional columns.
    assert "degraded_fields" not in server.gers_lookup(DIVISION_ID)


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
