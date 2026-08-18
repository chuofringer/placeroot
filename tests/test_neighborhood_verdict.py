"""Neighborhood verdict compose tool (issue #303).

Mocked internals: the product is the parse + score + one-pass compose, not
another live S3 scan. The fixture places theme has no grocery/playground
rows, so a live compose against it would only exercise the empty path.
"""

import pytest

from placeroot import categories, overture, routing, server, verdict

from .conftest import CENTER_LAT, CENTER_LON


def _place(need_slug, name, distance_m, **extra):
    row = {
        "id": f"p-{need_slug}",
        "name": name,
        "category": need_slug,
        "basic_category": need_slug,
        "distance_m": distance_m,
        "lat": CENTER_LAT,
        "lon": CENTER_LON,
        "operating_status": "in business",
        "confidence": 0.9,
        "brand": None,
        "has_website": False,
        "has_phone": False,
    }
    row.update(extra)
    return row


def _summary(total=40):
    return {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "radius_m": 1575.0,
        "total_places": total,
        "top_categories": [{"category": "restaurant", "count": 12}],
        "other_categories_count": 0,
        "uncategorized_count": 0,
    }


def _iso(max_radius_m=2000.0):
    return {
        "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
        "minutes": 15,
        "mode": "walk",
        "stats": {"reachable_nodes": 40, "max_radius_m": max_radius_m, "area_km2": 1.2},
        "polygon": {"type": "Polygon", "coordinates": []},
    }


def _toddler_places():
    # 4 min walk at 1.4 m/s ~= 336 m; 6 min ~= 504 m; 22 min ~= 1848 m
    return [
        _place("grocery_store", "Corner Market", 336),
        _place("pharmacy", "Day Pharmacy", 504),
        _place("playground", "Tot Lot", 700),
        _place("park", "Neighborhood Park", 400),
        _place("restaurant", "Noodle Shop", 250),
        _place("bus_station", "Far Bus", 1848),
    ]


def _patch_compose(monkeypatch, places=None, summary=None, iso=None, iso_error=None):
    calls = {"summarize": 0, "isochrone": 0, "places": 0, "find_places": 0}

    def fake_summarize(*_a, **_k):
        calls["summarize"] += 1
        return summary if summary is not None else _summary()

    def fake_places(*_a, **_k):
        calls["places"] += 1
        return places if places is not None else _toddler_places()

    def fake_iso(*_a, **_k):
        calls["isochrone"] += 1
        if iso_error is not None:
            raise iso_error
        return iso if iso is not None else _iso()

    def boom_find(*_a, **_k):
        calls["find_places"] += 1
        raise AssertionError("compose must not call find_places N times")

    monkeypatch.setattr(overture, "summarize_area", fake_summarize)
    monkeypatch.setattr(overture, "find_places_for_categories", fake_places)
    monkeypatch.setattr(overture, "find_places", boom_find)
    monkeypatch.setattr(routing, "isochrone", fake_iso)
    return calls


# --- parse -----------------------------------------------------------------


def test_parses_no_car_toddler_as_walk_plus_playground_checklist():
    parsed = verdict.parse_context("no car, one toddler")
    assert parsed["mobility"] == "walk"
    assert parsed["mobility_source"] == "context"
    assert "toddler" in parsed["household"]
    assert "grocery" in parsed["needs"]
    assert "pharmacy" in parsed["needs"]
    assert "playground" in parsed["needs"]
    assert "transit" in parsed["needs"]
    assert parsed["assumed"] is False


def test_bike_and_car_keywords_set_mode():
    assert verdict.parse_context("we bike everywhere")["mobility"] == "cycle"
    assert verdict.parse_context("two cars, commute by driving")["mobility"] == "drive"


def test_empty_context_assumes_walk_daily_needs():
    parsed = verdict.parse_context("")
    assert parsed["assumed"] is True
    assert parsed["mobility"] == "walk"
    assert parsed["household"] == []
    assert list(parsed["needs"])[:5] == list(verdict.DEFAULT_NEEDS)
    parsed_ws = verdict.parse_context("   \t  ")
    assert parsed_ws["assumed"] is True


def test_no_car_wins_over_bare_car_substring():
    assert verdict.parse_context("no car")["mobility"] == "walk"


# --- compose / score -------------------------------------------------------


def test_returns_verdict_strengths_weak_points_and_verify(monkeypatch):
    _patch_compose(monkeypatch)
    result = verdict.neighborhood_verdict(CENTER_LAT, CENTER_LON, "no car, one toddler")
    assert "error" not in result
    assert result["mode"] == "walk"
    assert result["minutes"] == 15
    assert "verdict" in result and result["verdict"]
    assert any(s["need"] == "grocery" for s in result["strengths"])
    assert any(s["need"] == "pharmacy" for s in result["strengths"])
    transit = next(w for w in result["weak_points"] if w["need"] == "transit")
    assert transit["walk_min"] >= 20
    assert result["verify_in_person"]
    assert "transit" in result["verify_in_person"] or "walk" in result["verify_in_person"]
    assert result["honesty"]
    by_need = {r["need"]: r for r in result["checklist"]}
    assert by_need["grocery"]["status"] == "covered"
    assert by_need["transit"]["status"] in {"weak", "unknown"}
    assert by_need["grocery"]["nearest"]["name"] == "Corner Market"


def test_empty_context_states_assumptions(monkeypatch):
    _patch_compose(monkeypatch, places=_toddler_places())
    result = server.neighborhood_verdict(CENTER_LAT, CENTER_LON, context="  ")
    assert "error" not in result
    assert "Assuming" in result["verdict"]
    assert result["context_read"]["mobility"] == "walk"
    assert result["strengths"] or result["weak_points"]


def test_invalid_coords_are_bad_request():
    result = server.neighborhood_verdict(91.0, CENTER_LON, "no car")
    assert result["error"] == "bad_request"
    result = server.neighborhood_verdict(CENTER_LAT, 200.0, "no car")
    assert result["error"] == "bad_request"


def test_unsupported_mode_is_structured():
    result = server.neighborhood_verdict(CENTER_LAT, CENTER_LON, "", mode="hover")
    assert result["error"] == "unsupported_mode"
    assert "walk" in result["supported"]


def test_compose_calls_existing_internals_once_each(monkeypatch):
    calls = _patch_compose(monkeypatch)
    verdict.neighborhood_verdict(CENTER_LAT, CENTER_LON, "no car, one toddler")
    assert calls["summarize"] == 1
    assert calls["isochrone"] == 1
    assert calls["places"] == 1
    assert calls["find_places"] == 0


def test_isochrone_miss_degrades_instead_of_failing(monkeypatch):
    _patch_compose(
        monkeypatch,
        iso_error=routing.NoGraphNearby(CENTER_LAT, CENTER_LON, 1575, mode="walk"),
    )
    result = server.neighborhood_verdict(CENTER_LAT, CENTER_LON, "no car")
    assert "error" not in result
    assert "straight-line" in result.get("note", "")
    assert result["verdict"]


def test_upstream_failure_is_structured(monkeypatch):
    def boom(*_a, **_k):
        raise overture.UpstreamUnavailable("scan failed")

    monkeypatch.setattr(overture, "summarize_area", boom)
    result = server.neighborhood_verdict(CENTER_LAT, CENTER_LON, "no car")
    assert result["error"] == "upstream_unavailable"


def test_server_applies_budget(monkeypatch):
    _patch_compose(monkeypatch)
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "80")
    result = server.neighborhood_verdict(CENTER_LAT, CENTER_LON, "no car, one toddler")
    assert result.get("truncated") is True


def test_mode_override_beats_context(monkeypatch):
    captured = {}

    def fake_iso(lat, lon, minutes=15, mode="walk", **_k):
        captured["mode"] = mode
        return _iso()

    _patch_compose(monkeypatch)
    monkeypatch.setattr(routing, "isochrone", fake_iso)
    result = verdict.neighborhood_verdict(
        CENTER_LAT, CENTER_LON, "no car, one toddler", mode="cycle"
    )
    assert result["mode"] == "cycle"
    assert captured["mode"] == "cycle"


def test_same_radius_passed_to_each_internal(monkeypatch):
    seen = {"summarize": None, "places": None, "isochrone": None}

    def fake_summarize(lat, lon, radius_m=1000):
        seen["summarize"] = radius_m
        return _summary()

    def fake_places(lat, lon, radius_m, categories, limit=80):
        seen["places"] = radius_m
        return _toddler_places()

    def fake_iso(lat, lon, minutes=15, mode="walk", radius_m=None, **_k):
        seen["isochrone"] = radius_m
        return _iso()

    monkeypatch.setattr(overture, "summarize_area", fake_summarize)
    monkeypatch.setattr(overture, "find_places_for_categories", fake_places)
    monkeypatch.setattr(routing, "isochrone", fake_iso)
    verdict.neighborhood_verdict(CENTER_LAT, CENTER_LON, "no car")
    assert seen["summarize"] == seen["places"] == seen["isochrone"]
    assert seen["summarize"] > 0


def test_need_slugs_exist_in_taxonomy():
    from placeroot import categories

    missing = [
        slug
        for slugs in verdict.NEED_SLUGS.values()
        for slug in slugs
        if categories.hierarchy_for(slug) is None
    ]
    assert not missing, missing


def test_find_places_for_categories_is_one_scan_against_fixture():
    """Internal helper actually queries, and the union includes both slugs."""
    rows = overture.find_places_for_categories(
        CENTER_LAT, CENTER_LON, 1000, ["coffee_shop", "novelty_shop"], limit=25
    )
    cats = {r["basic_category"] for r in rows}
    assert "coffee_shop" in cats
    # novelty_shop is the circle/square regression pair in the fixture
    assert "novelty_shop" in cats


# --- attribution: exact / hierarchy, not substring ----------------------------


def _park_is_ancestor(slug: str) -> bool:
    path = categories.hierarchy_for(slug)
    return bool(path and "park" in [seg.lower() for seg in path])


def test_lookalike_slugs_do_not_satisfy_park_unless_hierarchy_says_so():
    for slug in ("parking", "water_park", "rv_park", "dog_park"):
        place = _place(slug, slug.replace("_", " ").title(), 50)
        matches = verdict._place_matches(place, ("park",))
        assert matches is _park_is_ancestor(slug), (slug, categories.hierarchy_for(slug), matches)
        nearest = verdict._nearest_for_need([place], ("park",))
        if _park_is_ancestor(slug):
            assert nearest is place
        else:
            assert nearest is None


def test_driving_school_does_not_satisfy_school():
    path = categories.hierarchy_for("driving_school")
    assert path is not None
    assert "school" not in [seg.lower() for seg in path]
    place = _place("driving_school", "A1 Driving", 80)
    assert verdict._place_matches(place, ("school",)) is False
    assert verdict._nearest_for_need([place], ("school",)) is None


def test_true_park_and_school_and_real_descendants_still_match():
    assert verdict._place_matches(_place("park", "Neighborhood Park", 40), ("park",))
    assert verdict._place_matches(_place("school", "PS 1", 40), ("school",))
    elem_path = categories.hierarchy_for("elementary_school")
    assert elem_path and "school" in [seg.lower() for seg in elem_path]
    assert verdict._place_matches(_place("elementary_school", "Lincoln", 90), ("school",))
    # A closer lookalike must not steal nearest from a real park.
    places = [
        _place("parking", "Garage", 10),
        _place("park", "Real Park", 400),
    ]
    assert verdict._nearest_for_need(places, ("park",))["name"] == "Real Park"


def test_compose_parking_and_driving_school_do_not_cover_park_or_school(monkeypatch):
    _patch_compose(
        monkeypatch,
        places=[
            _place("parking", "City Garage", 80),
            _place("driving_school", "A1 Driving", 120),
        ],
    )
    result = verdict.neighborhood_verdict(CENTER_LAT, CENTER_LON, "kids, no car")
    by_need = {r["need"]: r for r in result["checklist"]}
    assert "park" in by_need
    assert "school" in by_need
    assert by_need["park"]["status"] != "covered"
    assert by_need["school"]["status"] != "covered"
    assert by_need["park"]["nearest"] is None
    assert by_need["school"]["nearest"] is None
    strengths = {s["need"] for s in result["strengths"]}
    assert "park" not in strengths
    assert "school" not in strengths


def test_place_categories_or_filter_is_exact_not_ilike():
    params = {}
    clauses = overture._place_categories_or_filter(set(), ["park", "school"], params)
    joined = " ".join(clauses)
    assert "ILIKE" not in joined.upper()
    assert "%" not in "".join(str(v) for v in params.values())
    values = {str(v).lower() for v in params.values()}
    assert "park" in values
    assert "parking" not in values
    assert "water_park" not in values
    assert "school" in values
    assert "driving_school" not in values
    assert "elementary_school" in values
    expanded = overture._expand_need_slugs(["park"])
    assert "park" in expanded
    assert "parking" not in expanded
    assert "dog_park" in expanded  # real descendant in the bundled taxonomy


# --- NaN budget / iso_max downgrade ------------------------------------------


def test_derive_budget_rejects_nan_and_inf():
    parsed = verdict.parse_context("")
    with pytest.raises(ValueError, match="minutes"):
        verdict.derive_budget(parsed, None, float("nan"), None)
    with pytest.raises(ValueError, match="minutes"):
        verdict.derive_budget(parsed, None, float("inf"), None)
    with pytest.raises(ValueError, match="radius"):
        verdict.derive_budget(parsed, float("nan"), 15, None)
    with pytest.raises(ValueError, match="radius"):
        verdict.derive_budget(parsed, float("inf"), 15, None)


def test_nan_minutes_override_is_bad_request():
    result = server.neighborhood_verdict(
        CENTER_LAT, CENTER_LON, "", minutes=float("nan")
    )
    assert result["error"] == "bad_request"
    result = server.neighborhood_verdict(
        CENTER_LAT, CENTER_LON, "", radius_m=float("nan")
    )
    assert result["error"] == "bad_request"


def test_iso_max_downgrades_covered_place_beyond_reach(monkeypatch):
    # Grocery at 336 m is ~4 min walk (covered by the 15 min budget) but
    # outside a 200 m isochrone reach, so the checklist row becomes weak.
    _patch_compose(monkeypatch, iso=_iso(max_radius_m=200.0))
    result = verdict.neighborhood_verdict(CENTER_LAT, CENTER_LON, "no car")
    grocery = next(r for r in result["checklist"] if r["need"] == "grocery")
    assert grocery["status"] == "weak"
    assert grocery["nearest"]["distance_m"] == 336
    assert "200" in grocery["detail"] and "reach" in grocery["detail"]

