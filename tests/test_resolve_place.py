"""Tests for resolve_place (#22): free-text -> ranked, typed GERS ids."""

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


# --- #469: a shared generic type word is not relatedness ------------------


def test_gate_refuses_a_shared_type_word_alone():
    """"Snow Peak Land Station" shares only "station" with "Shibuya Station":
    that is the kind of thing asked for, not which one. Same for "Park Hotel
    Tokyo" against "Yoyogi Park"."""
    assert geocode._place_match_label("Snow Peak Land Station", "Shibuya Station") is None
    assert geocode._place_match_label("Park Hotel Tokyo", "Yoyogi Park") is None
    assert geocode._place_match_label("Tokyo Station Beer Stand", "Shibuya Station") is None


def test_gate_accepts_a_shared_distinctive_word():
    assert geocode._place_match_label("Gare de Shibuya", "Shibuya Station") is not None
    # #22's own case: containment in either direction is untouched.
    assert geocode._place_match_label("Mañana Coffee", "Mañana coffee Austin") in (
        "prefix", "substring",
    )
    # A query that is nothing but a type word has no distinctive word to
    # insist on — plain containment still matches.
    assert geocode._place_match_label("Snow Peak Land Station", "Station") == "substring"


def test_type_word_categories_are_real_overture_slugs():
    from placeroot import categories

    for word, slugs in geocode._TYPE_WORD_CATEGORIES.items():
        assert word in geocode._GENERIC_PLACE_WORDS, word
        for slug in slugs:
            assert categories.hierarchy_for(slug) is not None, (word, slug)


def _tokyo_geocode(query, limit=5, lang=None):
    if query.strip().lower() == "tokyo":
        return [{
            "name": "Tokyo", "type": "locality", "lat": 35.68, "lon": 139.76,
            "id": "div-tokyo", "admin_context": ["Japan"], "rank_score": 0.9,
        }]
    # geocode()'s own anchored fallback: businesses that merely carry the
    # type word, the rows that used to win.
    return [
        {
            "name": "Snow Peak Land Station", "type": "place", "lat": 35.6796,
            "lon": 139.7652, "id": "p-snow", "rank_score": 0.9,
            "category": "sporting_goods_store", "admin_context": [],
        },
        {
            "name": "Tokyo Station Beer Stand", "type": "place", "lat": 35.681,
            "lon": 139.7659, "id": "p-beer", "rank_score": 0.8,
            "category": "beer_bar", "admin_context": [],
        },
    ]


def _place(id_, name, category, lat, lon, confidence=0.8):
    return {
        "id": id_, "name": name, "category": category, "basic_category": category,
        "operating_status": "open", "confidence": confidence, "lat": lat, "lon": lon,
        "distance_m": 100,
    }


def test_type_word_query_resolves_by_category_on_the_distinctive_token(monkeypatch):
    """"Shibuya Station Tokyo": one extra scan, filtered to the station
    categories and to "Shibuya", and its row ranks first — graded exact,
    because the category answers the type word and the name answers the
    rest. The name-only businesses fall to the gate."""
    calls = []

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10,
                         categories=None):
        calls.append({"categories": categories, "name": name, "radius_m": radius_m})
        if categories:
            return [
                _place("p-gare", "Gare de Shibuya", "train_station", 35.6585, 139.7013, 0.84),
                _place("p-parking", "Shibuya Station Parking", "parking", 35.659, 139.702),
            ]
        return [
            _place("p-snow", "Snow Peak Land Station", "sporting_goods_store",
                   35.6796, 139.7652, 0.9),
            _place("p-cheese", "Shibuya Cheese Stand", "cheese_shop", 35.66, 139.70),
        ]

    monkeypatch.setattr(geocode, "geocode", _tokyo_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode._resolve_lru.clear()

    results = geocode.resolve_place("Shibuya Station Tokyo", limit=3)

    typed = [c for c in calls if c["categories"]]
    assert len(typed) == 1
    assert typed[0]["name"] == "Shibuya"
    assert set(typed[0]["categories"]) == set(geocode._TYPE_WORD_CATEGORIES["station"])
    assert typed[0]["radius_m"] == geocode._RESOLVE_PLACE_RADIUS_M

    assert [r["id"] for r in results] == ["p-gare", "p-cheese"]
    assert results[0]["match"] == "exact"
    assert results[0]["category"] == "train_station"
    assert results[1]["match"] == "substring"
    for r in results:
        assert not any(k.startswith("_") for k in r)


def test_type_scan_prefers_the_supported_row_over_the_lone_duplicate(monkeypatch):
    """Two rows say the station is at Shibuya; one, nearer the city pin and
    more confident, says it is 7 km east. Agreement wins."""
    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10,
                         categories=None):
        if categories:
            return [
                _place("p-lone", "Shibuya Station Tokyo. Japan", "train_station",
                       35.6882, 139.7815, 0.99),
                _place("p-gare", "Gare de Shibuya", "train_station", 35.6585, 139.7013, 0.84),
                _place("p-keio", "Inokashira Line Shibuya Sta.", "train_station",
                       35.6581, 139.6984, 0.77),
            ]
        return []

    monkeypatch.setattr(geocode, "geocode", _tokyo_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode._resolve_lru.clear()

    results = geocode.resolve_place("Shibuya Station, Tokyo", limit=3)
    assert [r["id"] for r in results] == ["p-gare", "p-keio", "p-lone"]


def test_query_without_a_type_word_never_runs_the_category_scan(monkeypatch):
    calls = []

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10,
                         categories=None):
        calls.append(categories)
        return [_place("p-1", "Manana Coffee", "coffee_shop", 35.66, 139.70)]

    monkeypatch.setattr(geocode, "geocode", _tokyo_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode._resolve_lru.clear()

    results = geocode.resolve_place("Manana Coffee Tokyo", limit=3)
    assert calls and all(c is None for c in calls)
    assert results[0]["id"] == "p-1"


def test_nothing_related_survives_the_gate_returns_empty(monkeypatch):
    """Only type-word businesses anywhere near — the answer is [], and the
    server turns that into need: location, never the nearest "...Station"."""
    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10,
                         categories=None):
        if categories:
            return []
        return [_place("p-snow", "Snow Peak Land Station", "sporting_goods_store",
                       35.6796, 139.7652, 0.9)]

    monkeypatch.setattr(geocode, "geocode", _tokyo_geocode)
    monkeypatch.setattr(overture, "find_places", fake_find_places)
    geocode._resolve_lru.clear()

    assert geocode.resolve_place("Zzyzx Station Tokyo", limit=3) == []
    geocode._resolve_lru.clear()
    payload = server.resolve_place("Zzyzx Station Tokyo", limit=3)
    assert payload["results"] == []
    assert payload["need"] == "location"


def test_gate_sets_the_split_off_city_word_aside():
    """"Tokyo Station Beer Stand" shares "tokyo" with "Shibuya Station Tokyo"
    — the word resolve_place split off as the city. Location context, not
    relatedness; containment of the whole name is still honoured."""
    ctx = frozenset({"tokyo"})
    assert geocode._place_match_label("Tokyo Station Beer Stand", "Shibuya Station Tokyo") \
        == "substring"
    assert geocode._place_match_label("Tokyo Station Beer Stand", "Shibuya Station Tokyo", ctx) \
        is None
    assert geocode._place_match_label("Park Hotel Tokyo", "Park Hotel Tokyo", ctx) == "exact"
