"""#329: city-hint ranking, last-resolve LRU, shared-table geocode_batch."""

from placeroot import geocode, overture, server

from .conftest import CENTER_LAT, CENTER_LON


def test_trailing_city_is_parsed_off_a_landmark():
    place, city, coords = geocode._extract_city_hint("Colosseo Roma")
    assert place == "Colosseo"
    assert city == "Rome"
    assert coords is not None
    assert coords[0] == geocode._lookup_poi_alias("Colosseo")["lat"]


def test_notre_dame_paris_uses_the_paris_alias():
    place, city, coords = geocode._extract_city_hint("notre dame paris")
    assert place.lower() == "notre dame"
    assert city == "Paris"
    assert coords is not None
    assert abs(coords[0] - 48.853) < 0.01


def test_a_bare_city_is_not_a_poi_alias():
    place, city, coords = geocode._extract_city_hint("Paris")
    assert place == "Paris"
    assert city is None
    assert coords is None
    assert not geocode._query_is_poi_shaped("Paris")
    assert not geocode._query_is_poi_shaped("San Francisco")


def test_famous_poi_does_not_lose_to_an_obscure_exact_division(monkeypatch):
    """Colosseum used to exact-match a Queensland locality and win."""

    def fake_geocode(query, limit=5, lang=None, near=None):
        q = query.lower()
        if q in {"rome", "roma"}:
            return [{
                "name": "Rome", "type": "locality", "lat": 41.89, "lon": 12.49,
                "id": "rome", "admin_context": ["Italy"], "rank_score": 1.0,
            }]
        return [{
            "name": "Colosseum", "type": "locality", "lat": -23.4, "lon": 150.5,
            "id": "qld-colosseum", "admin_context": ["Australia", "Queensland"],
            "rank_score": 1.0,
        }]

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10, **kwargs):
        if abs(lat - 41.8902) < 0.5 and name and "coloss" in name.lower():
            return [{
                "id": "colosseum-rome", "name": "Colosseo", "category": "monument",
                "basic_category": "monument", "operating_status": "open",
                "confidence": 0.95, "lat": 41.8902, "lon": 12.4922, "distance_m": 8,
            }]
        return []

    monkeypatch.setattr(geocode, "geocode", fake_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    results = geocode.resolve_place("Colosseum")
    assert results
    assert results[0]["id"] == "colosseum-rome"
    assert results[0]["lat"] == 41.8902


def test_ebisu_alias_does_not_aim_at_shikoku(monkeypatch):
    def fake_geocode(query, limit=5, lang=None, near=None):
        if query.lower() == "tokyo":
            return [{
                "name": "Tokyo", "type": "locality", "lat": 35.68, "lon": 139.69,
                "id": "tokyo", "admin_context": ["Japan"], "rank_score": 1.0,
            }]
        return [{
            "name": "Ebisu", "type": "locality", "lat": 33.9, "lon": 133.2,
            "id": "shikoku-ebisu", "admin_context": ["Japan", "Ehime"],
            "rank_score": 1.0,
        }]

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10, **kwargs):
        if abs(lat - 35.6467) < 0.5:
            return [{
                "id": "ebisu-tokyo", "name": "Ebisu", "category": "neighbourhood",
                "basic_category": "neighbourhood", "operating_status": "open",
                "confidence": 0.8, "lat": 35.6467, "lon": 139.7101, "distance_m": 20,
            }]
        return []

    monkeypatch.setattr(geocode, "geocode", fake_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    results = geocode.resolve_place("Ebisu")
    assert results
    assert results[0]["id"] == "ebisu-tokyo"


def test_repeat_resolve_hits_the_lru():
    first = geocode.resolve_place("Brooklyn")
    assert first
    calls = {"n": 0}
    real = geocode.geocode

    def spy(query, limit=5):
        calls["n"] += 1
        return real(query, limit)

    geocode.geocode = spy
    try:
        second = geocode.resolve_place("Brooklyn")
    finally:
        geocode.geocode = real
    assert calls["n"] == 0
    assert [r["id"] for r in second] == [r["id"] for r in first]


def test_last_city_is_reused_for_the_next_poi(monkeypatch):
    geocode.resolve_place("Brooklyn")
    assert geocode._last_good_city

    seen = {}

    def fake_geocode(query, limit=5, lang=None, near=None):
        seen.setdefault("q", []).append(query)
        return [{
            "name": query, "type": "locality", "lat": CENTER_LAT, "lon": CENTER_LON,
            "id": "div", "admin_context": ["United States", "New York"],
            "rank_score": 0.5,
        }]

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10, **kwargs):
        seen["near"] = (lat, lon)
        return [{
            "id": "place-1", "name": "Some Tower", "category": "monument",
            "basic_category": "monument", "operating_status": "open",
            "confidence": 0.7, "lat": CENTER_LAT, "lon": CENTER_LON, "distance_m": 5,
        }]

    monkeypatch.setattr(geocode, "geocode", fake_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    # A feature-noun query with no city of its own should inherit Brooklyn.
    results = geocode.resolve_place("Observation Tower")
    assert results
    assert abs(seen["near"][0] - CENTER_LAT) < 0.01
    assert abs(seen["near"][1] - CENTER_LON) < 0.01


def test_observation_tower_does_not_replay_brooklyn_after_paris(monkeypatch):
    """Implicit last-city must be part of the resolve cache key.

    Brooklyn → Observation Tower caches a Brooklyn pin. After Paris
    updates last-city, the same query must resolve against Paris, not
    replay the Brooklyn cache entry.
    """
    geocode.resolve_place("Brooklyn")
    assert geocode._last_good_city

    def fake_geocode(query, limit=5, lang=None, near=None):
        q = query.lower()
        if q == "paris":
            return [{
                "name": "Paris", "type": "locality", "lat": 48.857, "lon": 2.351,
                "id": "paris", "admin_context": ["France"], "rank_score": 1.0,
            }]
        return [{
            "name": query, "type": "locality", "lat": CENTER_LAT, "lon": CENTER_LON,
            "id": "div", "admin_context": ["United States", "New York"],
            "rank_score": 0.5,
        }]

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10, **kwargs):
        if abs(lat - 48.857) < 1.0:
            return [{
                "id": "paris-tower", "name": "Observation Tower",
                "category": "monument", "basic_category": "monument",
                "operating_status": "open", "confidence": 0.8,
                "lat": 48.858, "lon": 2.294, "distance_m": 10,
            }]
        return [{
            "id": "bk-tower", "name": "Observation Tower",
            "category": "monument", "basic_category": "monument",
            "operating_status": "open", "confidence": 0.8,
            "lat": CENTER_LAT, "lon": CENTER_LON, "distance_m": 5,
        }]

    monkeypatch.setattr(geocode, "geocode", fake_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)

    first = geocode.resolve_place("Observation Tower")
    assert first and first[0]["id"] == "bk-tower"

    paris = geocode.resolve_place("Paris")
    assert paris and paris[0]["id"] == "paris"

    second = geocode.resolve_place("Observation Tower")
    assert second and second[0]["id"] == "paris-tower"


def test_last_city_does_not_bind_a_bare_city_query(monkeypatch):
    geocode._last_good_city = "Palo Alto"
    geocode._last_good_coords = (37.44, -122.14)
    assert not geocode._query_is_poi_shaped("Paris")
    place, city, coords = geocode._extract_city_hint("Paris")
    assert city is None and coords is None


def test_casablanca_well_known_pin_outranks_a_namesake():
    morocco = {
        "id": "ma", "name": "Casablanca", "subtype": "locality",
        "lat": 33.57, "lon": -7.59, "population": 3_000_000,
        "admin_context": ["Morocco"], "region": "MA-CAS",
    }
    chile = {
        "id": "cl", "name": "Casablanca", "subtype": "locality",
        "lat": -33.32, "lon": -71.41, "population": 18_000,
        "admin_context": ["Chile"], "region": "CL-VS",
    }
    pop = {}
    assert geocode._rank_key(morocco, "Casablanca", pop) < geocode._rank_key(
        chile, "Casablanca", pop
    )


def test_geocode_batch_opens_the_name_table_once(geocode_cache, monkeypatch):
    n = {"t": 0}
    real = geocode._local_divisions_table

    def spy():
        n["t"] += 1
        return real()

    monkeypatch.setattr(geocode, "_local_divisions_table", spy)
    rows = geocode.geocode_batch(["Brooklyn", "Springfield"])
    assert n["t"] == 1
    assert [r["query"] for r in rows] == ["Brooklyn", "Springfield"]
    assert "error" not in rows[0]
    assert rows[0]["name"] == "Brooklyn"


def test_server_geocode_batch_still_uses_the_shared_table():
    result = server.geocode_batch(["Brooklyn", "Riverside"])
    assert [r["query"] for r in result["results"]] == ["Brooklyn", "Riverside"]
    assert result["results"][0]["name"] == "Brooklyn"


def test_clear_resolve_session_drops_lru_and_last_city():
    geocode.resolve_place("Brooklyn")
    assert geocode._resolve_lru
    geocode.clear_resolve_session()
    assert not geocode._resolve_lru
    assert geocode._last_good_city is None
