"""Tests for resolve_place (#22): free-text -> ranked, typed GERS ids."""

import json

from placeroot import geocode, overture, server

from .conftest import CENTER_LAT, CENTER_LON, DIVISIONS_FIXTURE_PATH


def test_division_resolution():
    results = geocode.resolve_place("Brooklyn", limit=5)
    assert results
    top = results[0]
    assert top["kind"] == "division"
    assert top["id"] == "gers-div-brooklyn"
    assert top["match"] == "exact"
    assert "admin_context" in top
    assert "category" not in top


def test_place_resolution_with_near_hint():
    results = geocode.resolve_place(
        "Blue Bottle Roastery", near_lat=CENTER_LAT, near_lon=CENTER_LON, limit=5
    )
    assert results
    top = results[0]
    assert top["kind"] == "place"
    assert top["name"] == "Blue Bottle Roastery"
    assert top["match"] == "exact"
    assert top["id"]
    assert "category" in top
    assert "admin_context" not in top


def test_geocode_place_hits_flow_through_without_a_reference():
    """No near hint and no division match — but geocode's own fallback can
    still resolve the place, and resolve_place must not lose to the plain
    geocoder on a place query (the "Shibuya Crossing Tokyo" bug: geocode
    could answer, resolve_place returned nothing). Its place-type hits are
    merged in as candidates; resolve_place's own reference-bounded places
    search still doesn't run without a reference."""
    results = geocode.resolve_place("Blue Bottle Roastery", limit=5)
    assert results
    top = results[0]
    assert top["kind"] == "place"
    assert top["name"] == "Blue Bottle Roastery"
    assert top["match"] == "exact"


def test_fallback_anchor_prefers_the_prominent_division(monkeypatch):
    """The trailing-token anchor ranks candidates like geocode's main path:
    _rank_key orders by each row's own population, so the 8M-person
    namesake beats the 100-person one regardless of the region map."""
    small = {"id": "d-small", "name": "Springfield", "subtype": "locality",
             "country": "PG", "region": None, "lat": -5.0, "lon": 142.0,
             "admin_context": [], "population": 100}
    big = dict(small, id="d-big", country="JP", lat=35.0, lon=139.0,
               population=8_000_000)
    monkeypatch.setattr(geocode, "_query_divisions", lambda *a, **k: [small, big])
    hit = geocode._fallback_anchor("Coffee Springfield", [], None, "unused")
    assert hit == (35.0, 139.0, "Coffee")


def test_fallback_anchor_searches_alternate_names_too(monkeypatch):
    """The trailing token is the city as the user writes it, which for many
    cities is an alternate spelling (Japan's Tokyo is primarily 東京都) — a
    primary-names-only lookup doesn't even contain the right row."""
    seen = {}

    def fake_query(candidate, region_code, local_table, alt_table=None):
        seen["alt_table"] = alt_table
        return []

    monkeypatch.setattr(geocode, "_query_divisions", fake_query)
    geocode._fallback_anchor("Coffee Springfield", [], None, "t", alt_table="alt-t")
    assert seen["alt_table"] == "alt-t"


def test_merged_ordering_is_kind_agnostic_by_match_tier(monkeypatch):
    # White-box: verify the merge/sort ranks candidates by match tier
    # regardless of kind, using controlled inputs so the test doesn't
    # depend on incidental overlap between fixture division/place names.
    def fake_geocode(query, limit=5, lang=None):
        return [
            {
                "name": "Example Town", "type": "locality", "lat": 1.0, "lon": 2.0,
                "id": "div-prefix", "admin_context": ["Testland"], "rank_score": 0.9,
            },
        ]

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
        return [
            {
                "id": "place-exact", "name": "Example", "category": "cafe",
                "basic_category": "cafe", "operating_status": "open",
                "confidence": 0.5, "lat": 1.0, "lon": 2.0, "distance_m": 10,
            },
        ]

    monkeypatch.setattr(geocode, "geocode", fake_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)

    results = geocode.resolve_place("Example", near_lat=1.0, near_lon=2.0, limit=5)
    assert [r["id"] for r in results] == ["place-exact", "div-prefix"]
    assert results[0]["match"] == "exact"
    assert results[1]["match"] == "prefix"


def test_budget_applied_via_server_tool():
    result = server.resolve_place("Brooklyn", limit=5)
    assert "results" in result
    assert isinstance(result["results"], list)


def test_not_found_returns_empty_results_not_an_error():
    result = server.resolve_place("Nonexistentplacexyz123", limit=5)
    assert result["results"] == []
    assert "error" not in result


def test_resolve_place_empty_query_returns_empty_list():
    assert geocode.resolve_place("", limit=5) == []
    assert geocode.resolve_place("   ", limit=5) == []


def test_resolve_place_limit_is_respected():
    results = geocode.resolve_place("e", limit=2)
    assert len(results) <= 2


def test_server_resolve_place_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(
        str(tmp_path / "does-not-exist" / "*.parquet"), theme="divisions", type_="division"
    )
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"), theme="places")
    result = server.resolve_place("Brooklyn", limit=5)
    assert result["error"] == "upstream_unavailable"
    overture.set_data_path(str(DIVISIONS_FIXTURE_PATH), theme="divisions", type_="division")


# ---------------------------------------------------------------------------
# #481: a bundled-alias pin outranks a string tier.


_PIN = {"city": "Brooklyn", "lat": 40.700, "lon": -73.900}
# ~4 km north of _PIN (0.036 deg of latitude) and ~30 m east of it.
_FAR_LAT, _NEAR_LON = 40.736, -73.89965


def _place(pid, name, lat, lon, confidence):
    return {
        "id": pid, "name": name, "category": "place_of_worship",
        "basic_category": "place_of_worship", "operating_status": "open",
        "confidence": confidence, "lat": lat, "lon": lon, "distance_m": 0,
    }


def _no_divisions(query, limit=5, lang=None):
    return []


def test_alias_pin_outranks_a_better_string_tier_four_km_away(monkeypatch):
    """#481: "notre dame paris" pinned 30 m from the cathedral and still
    answered a parish church 4.4 km north whose name *starts with* the
    query, because prefix beats substring and tier sorts before distance.
    The alias coordinate is curated; the row sitting on it must win."""
    prefix_far = _place("parish", "Landmark Parish Church", _FAR_LAT, _PIN["lon"], 0.99)
    substring_near = _place("cathedral", "Cathedral of the Landmark", _PIN["lat"], _NEAR_LON, 0.5)

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
        return [prefix_far, substring_near]

    monkeypatch.setattr(geocode, "_POI_ALIASES", {"landmark": _PIN})
    monkeypatch.setattr(geocode, "geocode", _no_divisions)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode.clear_resolve_session()

    results = geocode.resolve_place("Landmark Brooklyn", limit=5)
    assert [r["id"] for r in results] == ["cathedral", "parish"]
    assert results[0]["match"] == "substring"
    assert results[1]["match"] == "prefix"
    # The tag is bookkeeping for the sort, never part of the answer.
    assert all("_alias_pinned" not in r for r in results)
    assert all("_prominence" not in r for r in results)


def test_plain_city_split_does_not_pin_anything(monkeypatch):
    """A trailing well-known city with no alias behind it yields a search
    pin, not a curated landmark coordinate — tier-first ordering stands."""
    prefix_far = _place("parish", "Landmark Parish Church", _FAR_LAT, _PIN["lon"], 0.99)
    substring_near = _place("cathedral", "Cathedral of the Landmark", _PIN["lat"], _NEAR_LON, 0.5)

    def fake_geocode(query, limit=5, lang=None):
        if query == "Brooklyn":
            return [{
                "name": "Brooklyn", "type": "locality", "lat": _PIN["lat"], "lon": _PIN["lon"],
                "id": "div-brooklyn", "admin_context": ["New York"], "rank_score": 0.9,
            }]
        return []

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
        return [prefix_far, substring_near]

    monkeypatch.setattr(geocode, "_POI_ALIASES", {})
    monkeypatch.setattr(geocode, "geocode", fake_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode.clear_resolve_session()

    results = geocode.resolve_place("Landmark Brooklyn", limit=5)
    assert [r["id"] for r in results] == ["parish", "cathedral"]
    assert all("_alias_pinned" not in r for r in results)


def test_alias_pin_with_nothing_inside_its_radius_leaves_ordering_alone(monkeypatch):
    """The alias points at something the scans missed: no row is tagged,
    and the merged tier -> distance -> prominence sort is untouched."""
    prefix_far = _place("parish", "Landmark Parish Church", _FAR_LAT, _PIN["lon"], 0.99)
    # ~2 km north of the pin: closer than the prefix row, still well outside 400 m.
    substring_mid = _place("cathedral", "Cathedral of the Landmark", 40.718, _PIN["lon"], 0.5)

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
        return [prefix_far, substring_mid]

    monkeypatch.setattr(geocode, "_POI_ALIASES", {"landmark": _PIN})
    monkeypatch.setattr(geocode, "geocode", _no_divisions)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode.clear_resolve_session()

    results = geocode.resolve_place("Landmark Brooklyn", limit=5)
    assert [r["id"] for r in results] == ["parish", "cathedral"]
    assert all("_alias_pinned" not in r for r in results)


def test_alias_pin_radius_is_a_landmark_footprint():
    assert geocode._ALIAS_PIN_RADIUS_M == 400


def test_every_bundled_alias_key_round_trips():
    """Guards the JSON edit: every key must fold to itself and carry a
    usable pin, or _lookup_poi_alias silently returns None for it."""
    from importlib import resources

    raw = json.loads(
        (resources.files("placeroot") / "data" / "geocode-index" / "aliases.json")
        .read_text(encoding="utf-8")
    )
    assert raw
    for key, row in raw.items():
        assert geocode._fold_query_key(key) == key, key
        hit = geocode._lookup_poi_alias(key)
        assert hit is not None, key
        assert hit == {"city": row["city"], "lat": float(row["lat"]), "lon": float(row["lon"])}


def test_notre_dame_spellings_all_reach_the_paris_pin():
    """#481: the three query shapes that resolved elsewhere, plus the
    accented native form, all land on the cathedral's curated pin — via the
    whole-query alias for the forms that end in "de Paris", which the
    trailing-city split used to shadow."""
    for query in (
        "notre dame paris",
        "Notre-Dame de Paris",
        "Notre Dame de Paris",
        "Notre Dame Cathedral Paris",
        "Cathédrale Notre-Dame de Paris",
    ):
        _place_q, city, coords = geocode._extract_city_hint(query)
        assert city == "Paris", query
        assert coords is not None, query
        assert abs(coords[0] - 48.853) < 1e-3 and abs(coords[1] - 2.3499) < 1e-3, query


def test_alias_pin_scan_finds_the_landmark_the_name_scans_miss(monkeypatch):
    """#481 second half: the live cathedral row is "Cathédrale Notre-Dame de
    Paris" — no LIKE match for "notre dame" — so no name-filtered scan ever
    returned it. With an alias pin, one unfiltered nearest-first scan runs
    at the pin (radius _ALIAS_PIN_RADIUS_M); its rows still have to earn a
    label, and the one that does (via the alias spelling) tops the list."""
    prefix_far = _place("parish", "Landmark Parish Church", _FAR_LAT, _PIN["lon"], 0.99)
    cathedral = _place("cathedral", "Cathedrale Land-Mark", _PIN["lat"], _NEAR_LON, 0.5)
    bus_stop = _place("bus", "Arret Cite", _PIN["lat"], _NEAR_LON, 0.9)
    calls = []

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
        calls.append((lat, lon, radius_m, name))
        if name is None:
            return [cathedral, bus_stop]
        return [prefix_far]

    monkeypatch.setattr(
        geocode, "_POI_ALIASES", {"landmark": _PIN, "cathedrale land-mark": _PIN}
    )
    monkeypatch.setattr(geocode, "geocode", _no_divisions)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode.clear_resolve_session()

    results = geocode.resolve_place("Landmark Brooklyn", limit=5)
    assert [(r["id"], r["match"]) for r in results] == [
        ("cathedral", "exact"), ("parish", "prefix"),
    ]
    assert (_PIN["lat"], _PIN["lon"], geocode._ALIAS_PIN_RADIUS_M, None) in calls


def test_no_pin_scan_without_an_alias(monkeypatch):
    """A caller's near hint or a plain city split is a search bound, not a
    landmark coordinate — the unfiltered pin scan must not run for them."""
    calls = []

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
        calls.append(name)
        return []

    monkeypatch.setattr(geocode, "_POI_ALIASES", {})
    monkeypatch.setattr(geocode, "geocode", _no_divisions)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode.clear_resolve_session()

    geocode.resolve_place("Landmark", near_lat=_PIN["lat"], near_lon=_PIN["lon"], limit=5)
    assert calls and None not in calls
