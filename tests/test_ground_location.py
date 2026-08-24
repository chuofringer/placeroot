"""Single-hop location grounding: ground_location (#362)."""

import pytest

from placeroot import geocode, ground, overture, preferences, routing, server

from .conftest import CENTER_LAT, CENTER_LON
from .test_gers_lookup import PLACE_ID


def _fake_iso(*_a, **_k):
    return {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "minutes": 15,
        "mode": "walk",
        "speed_m_s": 1.4,
        "polygon": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
        "polygon_method": "concave_boundary",
        "stats": {"reachable_nodes": 40, "max_radius_m": 900.0, "area_km2": 1.2},
    }


def _no_geometry_keys(value, path="root") -> list[str]:
    """Every dict key path in `value` whose key is 'coordinates' or 'geometry'."""
    hits = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("coordinates", "geometry"):
                hits.append(f"{path}.{k}")
            hits.extend(_no_geometry_keys(v, f"{path}.{k}"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            hits.extend(_no_geometry_keys(v, f"{path}[{i}]"))
    return hits


def test_happy_path_has_all_four_sections(monkeypatch):
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")

    assert "error" not in result

    where = result["where"]
    assert where["admin_context"]
    assert "Brooklyn" in where["admin_context"]

    surroundings = result["surroundings"]
    assert surroundings["total_places"] > 0
    assert surroundings["top_categories"]
    assert len(surroundings["top_categories"]) <= 5
    assert surroundings["density_per_km2"] > 0

    reach = result["reach"]
    assert reach["stats"]["area_km2"] >= 0
    assert reach["stats"]["reachable_nodes"] >= 0
    assert "polygon" not in reach
    assert "polygon_method" not in reach

    notable = result["notable"]
    assert 1 <= len(notable) <= 3
    assert all(row.get("name") for row in notable)

    assert not _no_geometry_keys(result)


def test_no_geometry_anywhere_even_when_every_section_succeeds(monkeypatch):
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")
    assert _no_geometry_keys(result) == []


def test_reach_degrades_when_no_graph_nearby():
    """No mocking: the routing fixture's grid sits nowhere near CENTER, so
    isochrone naturally raises NoGraphNearby here — the real degrade path."""
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")
    assert "error" not in result
    assert "reach" not in result
    assert any(note.startswith("reach:") for note in result["notes"])
    assert "where" in result
    assert "surroundings" in result
    assert "notable" in result


def test_where_degrades_on_upstream_unavailable(monkeypatch):
    def boom(*_a, **_k):
        raise overture.UpstreamUnavailable("scan failed")

    monkeypatch.setattr(ground.geocoding, "reverse_geocode", boom)
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")

    assert "error" not in result
    assert "where" not in result
    assert any(note.startswith("where:") for note in result["notes"])
    assert "surroundings" in result
    assert "reach" in result
    assert "notable" in result


def test_surroundings_degrades_on_schema_degraded(monkeypatch):
    def boom(*_a, **_k):
        raise overture.SchemaDegraded(["bbox"])

    monkeypatch.setattr(ground.overture, "summarize_area", boom)
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")

    assert "error" not in result
    assert "surroundings" not in result
    assert any(note.startswith("surroundings:") for note in result["notes"])
    assert "where" in result
    assert "reach" in result
    assert "notable" in result


def test_notable_degrades_on_empty_results(monkeypatch):
    monkeypatch.setattr(ground.overture, "find_places", lambda *_a, **_k: [])
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")

    assert "error" not in result
    assert "notable" not in result
    assert any(note.startswith("notable:") for note in result["notes"])
    assert "where" in result
    assert "surroundings" in result
    assert "reach" in result


def test_notable_degrades_on_upstream_unavailable(monkeypatch):
    def boom(*_a, **_k):
        raise overture.UpstreamUnavailable("scan failed")

    monkeypatch.setattr(ground.overture, "find_places", boom)
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")

    assert "error" not in result
    assert "notable" not in result
    assert any(note.startswith("notable:") for note in result["notes"])


def test_all_sections_failing_returns_structured_error(monkeypatch):
    def boom_upstream(*_a, **_k):
        raise overture.UpstreamUnavailable("scan failed")

    def boom_no_graph(*_a, **_k):
        raise routing.NoGraphNearby(CENTER_LAT, CENTER_LON, 100.0, mode="walk")

    monkeypatch.setattr(ground.geocoding, "reverse_geocode", boom_upstream)
    monkeypatch.setattr(ground.overture, "summarize_area", boom_upstream)
    monkeypatch.setattr(ground.overture, "find_places", boom_upstream)
    monkeypatch.setattr(routing, "isochrone", boom_no_graph)

    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")
    assert result["error"] == "upstream_unavailable"
    assert result["retry_advised"] is True
    assert "detail" in result


def test_omitted_mode_uses_stored_preference(monkeypatch):
    preferences.update(mode="cycle")
    seen: dict = {}

    def fake(lat, lon, minutes=15, mode="walk", **_k):
        seen["mode"] = mode
        return {**_fake_iso(), "mode": mode}

    monkeypatch.setattr(routing, "isochrone", fake)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15)
    assert "error" not in result
    assert seen["mode"] == "cycle"


def test_explicit_mode_wins_over_stored_preference(monkeypatch):
    preferences.update(mode="cycle")
    seen: dict = {}

    def fake(lat, lon, minutes=15, mode="walk", **_k):
        seen["mode"] = mode
        return {**_fake_iso(), "mode": mode}

    monkeypatch.setattr(routing, "isochrone", fake)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")
    assert "error" not in result
    assert seen["mode"] == "walk"


def test_surroundings_counts_reconcile_after_truncation(monkeypatch):
    # 7 categorized rows from summarize_area; ground truncates to 5 and must
    # fold the two cut rows into other_categories_count so
    # total == sum(top) + other + uncategorized still holds.
    counts = [6, 5, 4, 3, 3, 2, 2]
    summary = {
        "total_places": 30,
        "top_categories": [
            {"category": f"cat_{i}", "count": n} for i, n in enumerate(counts)
        ],
        "other_categories_count": 2,
        "uncategorized_count": 3,
    }
    monkeypatch.setattr(ground.overture, "summarize_area", lambda *_a, **_k: summary)
    monkeypatch.setattr(routing, "isochrone", _fake_iso)

    surroundings = server.ground_location(CENTER_LAT, CENTER_LON)["surroundings"]
    assert len(surroundings["top_categories"]) == 5
    shown = sum(row["count"] for row in surroundings["top_categories"])
    assert surroundings["other_categories_count"] == 2 + 2 + 2
    assert surroundings["uncategorized_count"] == 3
    assert (
        shown
        + surroundings["other_categories_count"]
        + surroundings["uncategorized_count"]
        == surroundings["total_places"]
    )


def test_internal_valueerror_is_not_hidden_as_a_note(monkeypatch):
    # ValueError from a delegate is a programming error, not a degraded
    # section — it must propagate, not turn into "section unavailable".
    def boom(*_a, **_k):
        raise ValueError("internal bug")

    monkeypatch.setattr(ground.overture, "summarize_area", boom)
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    with pytest.raises(ValueError):
        server.ground_location(CENTER_LAT, CENTER_LON)


def test_rejects_out_of_range_coordinates():
    result = server.ground_location(91.0, CENTER_LON)
    assert result["error"] == "bad_request"


def test_rejects_zero_minutes():
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=0)
    assert result["error"] == "bad_request"


def test_rejects_minutes_over_sixty():
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=61)
    assert result["error"] == "bad_request"


def test_rejects_unknown_mode():
    result = server.ground_location(CENTER_LAT, CENTER_LON, mode="teleport")
    assert result["error"] == "bad_request"


# --- LocationRef (roadmap #4.1): `where`, mutually exclusive with lat/lon ---


def test_needs_lat_lon_or_where():
    result = server.ground_location()
    assert result["error"] == "bad_request"


def test_rejects_both_latlon_and_where():
    result = server.ground_location(
        lat=CENTER_LAT, lon=CENTER_LON, where={"lat": CENTER_LAT, "lon": CENTER_LON}
    )
    assert result["error"] == "bad_request"


def test_plain_latlon_has_no_resolved_key(monkeypatch):
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(CENTER_LAT, CENTER_LON, minutes=15, mode="walk")
    assert "error" not in result
    assert "resolved" not in result


def test_where_coordinate_dict_has_no_resolved_key(monkeypatch):
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(
        where={"lat": CENTER_LAT, "lon": CENTER_LON}, minutes=15, mode="walk"
    )
    assert "error" not in result
    assert "resolved" not in result


def test_where_gers_id_adds_resolved(monkeypatch):
    """Real fixture GERS id (see tests/test_gers_lookup.py) — no monkeypatch on gers."""
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(where=PLACE_ID, minutes=15, mode="walk")
    assert "error" not in result
    assert result["resolved"]["matched_by"] == "gers_id"
    assert result["resolved"]["id"] == PLACE_ID


def test_where_name_adds_resolved(monkeypatch):
    def fake_resolve(query):
        assert query == "Cluster Place 000"
        return {
            "name": query, "lat": CENTER_LAT, "lon": CENTER_LON,
            "id": "gers-cluster", "type": "place",
        }

    monkeypatch.setattr(geocode, "resolve_named_place", fake_resolve)
    monkeypatch.setattr(routing, "isochrone", _fake_iso)
    result = server.ground_location(where="Cluster Place 000", minutes=15, mode="walk")
    assert "error" not in result
    assert result["resolved"] == {
        "name": "Cluster Place 000", "id": "gers-cluster",
        "lat": CENTER_LAT, "lon": CENTER_LON, "matched_by": "name",
    }


def test_where_bad_ref_is_bad_request():
    result = server.ground_location(where={"lat": 91.0, "lon": 0.0})
    assert result["error"] == "bad_request"
