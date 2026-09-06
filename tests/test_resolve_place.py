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


# --- #464: a split-derived anchor is a guess, and a guess needs corroboration


def _no_session_state(monkeypatch):
    """resolve_place reuses the last good city for POI-shaped queries (#329)
    and replays an LRU by (query, hint); both would let one test's run
    answer the next test's question. Start each #464 test cold."""
    monkeypatch.setattr(geocode, "_last_good_city", None)
    monkeypatch.setattr(geocode, "_last_good_coords", None)
    geocode._resolve_lru.clear()
    monkeypatch.setattr(geocode, "_local_divisions_table", lambda: None)
    monkeypatch.setattr(geocode, "geocode", lambda query, limit=5, lang=None: [])


def _division(name, lat, lon, **extra):
    row = {
        "id": f"d-{name.lower().replace(' ', '-')}", "name": name, "subtype": "locality",
        "country": "US", "region": "US-XX", "lat": lat, "lon": lon,
        "admin_context": [], "population": 5000,
    }
    row.update(extra)
    return row


def _place(name, lat, lon, **extra):
    row = {
        "id": f"p-{name.lower().replace(' ', '-')}", "name": name, "category": "shopping_mall",
        "basic_category": "shopping_mall", "operating_status": "open",
        "confidence": 0.9, "lat": lat, "lon": lon, "distance_m": 10,
    }
    row.update(extra)
    return row


def _divisions_by_candidate(mapping):
    def fake_query(candidate, *args, **kwargs):
        return [dict(r) for r in mapping.get(candidate, [])]
    return fake_query


def test_split_anchor_drops_a_place_that_only_shares_one_word(monkeypatch):
    """The #464 repro shape: no division matches "Mall of Westoria" as a
    whole, its trailing "of Westoria" is a *substring* of some unrelated
    division's name, and the anchored scan near that division finds a mall
    that shares the word "Mall" with the query and nothing else. Before
    #464 that row came back as a "substring" match; it is unrelated to the
    question and must be dropped, leaving nothing (so the server asks for a
    location) rather than a fast wrong answer."""
    _no_session_state(monkeypatch)
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "of Westoria": [_division("Traditions of Westoria", 37.37, -77.49)],
    }))
    seen = []

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
        seen.append((round(lat, 2), round(lon, 2), name))
        return [_place("Mercy Mall of VA", 37.398, -77.522)]

    monkeypatch.setattr(overture, "find_places", fake_find_places)
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [])

    assert geocode.resolve_place("Mall of Westoria", limit=5) == []
    # The anchor was derived (the scan ran near the Virginia division) — the
    # row was rejected by the whole-query rule, not by never being found.
    assert seen and seen[0][:2] == (37.37, -77.49)


def test_split_anchor_rejects_geocode_fallback_rows_far_from_the_anchor(monkeypatch):
    """geocode()'s own anchored fallback may have guessed a *different*
    namesake than resolve_place did (traced live for "Mall of America": a
    Bolivian hamlet named America here, a Virginia division there). A strong
    anchor excuses a nearby place from repeating the anchor word — a place
    6,000 km away is excused from nothing."""
    _no_session_state(monkeypatch)
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "Westoria": [_division("Westoria", -11.35, -66.34)],  # exact, city-level: strong
    }))
    monkeypatch.setattr(geocode, "geocode", lambda query, limit=5, lang=None: [{
        "id": "p-mercy", "type": "place", "name": "Mercy Mall of VA",
        "lat": 37.398, "lon": -77.522, "rank_score": 0.9, "category": "shopping_mall",
        "admin_context": [],
    }])
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [])

    assert geocode.resolve_place("Mall of Westoria", limit=5) == []


def test_weak_trailing_match_with_a_majority_residual_is_no_anchor(monkeypatch):
    """#464 rule one: a trailing word that is merely a fragment of a longer
    division name the query never mentions, with most of the query left
    unexplained, is not a location word at all — the query is name-only."""
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "Westoria": [_division("Traditions of Westoria", 37.37, -77.49)],
    }))
    assert geocode._fallback_anchor("Mall of Westoria", [], None, None) is None


def test_trailing_fragment_of_a_city_name_the_query_contains_still_anchors(monkeypatch):
    """The other kind of substring match: "de Janeiro" is a fragment of
    "Rio de Janeiro", but the caller typed the whole city — the split just
    landed a word short. That anchor is as good as an exact one and its
    words stay exempt from the coverage rule (a beach in Rio is not named
    after Rio)."""
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "de Janeiro": [_division("Rio de Janeiro", -22.91, -43.21, population=6_000_000)],
    }))
    details = geocode._fallback_anchor_details("Copacabana Rio de Janeiro", [], None, None)
    assert details and details[0]["name_query"] == "Copacabana Rio"
    assert details[0]["strong"] is True


def test_name_only_query_gets_the_whole_query_prefix_pass(monkeypatch):
    """#464 rule two: once the guessed anchor explained nothing, the query
    is treated as the bare name it is — one unanchored scan for the WHOLE
    query, keeping exact/prefix rows only, ranked exact before prefix. A
    row that merely shares a word never comes back from this pass."""
    _no_session_state(monkeypatch)
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "of Westoria": [_division("Traditions of Westoria", 37.37, -77.49)],
    }))
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_skip_unanchored_places_scan", lambda: False)
    calls = []

    def fake_scan(query, anchor=None, also=None):
        calls.append((query, anchor, also))
        return [
            {"id": "p-mercy", "name": "Mercy Mall of VA", "lat": 37.398, "lon": -77.522,
             "_confidence": 0.99, "_category": "charity_organization"},
            {"id": "p-moa-food", "name": "Mall of Westoria Food Court", "lat": 44.855,
             "lon": -93.242, "_confidence": 0.95, "_category": "food_court"},
            {"id": "p-moa", "name": "Mall of Westoria", "lat": 44.8549, "lon": -93.2422,
             "_confidence": 0.5, "_category": "shopping_mall"},
        ]

    monkeypatch.setattr(geocode, "_query_places_fallback", fake_scan)

    results = geocode.resolve_place("Mall of Westoria", limit=5)
    assert calls == [("Mall of Westoria", None, None)]
    assert [(r["id"], r["match"]) for r in results] == [
        ("p-moa", "exact"), ("p-moa-food", "prefix"),
    ]
    assert results[0]["kind"] == "place"
    assert results[0]["category"] == "shopping_mall"


def test_name_only_pass_returns_nothing_rather_than_a_one_word_coincidence(monkeypatch):
    _no_session_state(monkeypatch)
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "of Westoria": [_division("Traditions of Westoria", 37.37, -77.49)],
    }))
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_skip_unanchored_places_scan", lambda: False)
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [
        {"id": "p-mercy", "name": "Mercy Mall of VA", "lat": 37.398, "lon": -77.522,
         "_confidence": 0.99, "_category": "charity_organization"},
    ])
    assert geocode.resolve_place("Mall of Westoria", limit=5) == []


def test_name_only_pass_respects_the_remote_scan_gate(monkeypatch):
    """#105: against a remote dataset the unanchored scan is a full read of
    the places theme. It does not run; the caller is asked for a location
    (server.py's need: "location") instead."""
    _no_session_state(monkeypatch)
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "of Westoria": [_division("Traditions of Westoria", 37.37, -77.49)],
    }))
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_skip_unanchored_places_scan", lambda: True)

    def must_not_run(*a, **k):
        raise AssertionError("unanchored scan ran against a remote dataset")

    monkeypatch.setattr(geocode, "_query_places_fallback", must_not_run)
    assert geocode.resolve_place("Mall of Westoria", limit=5) == []
    payload = server.resolve_place("Mall of Westoria", limit=5)
    assert payload["results"] == [] and payload["need"] == "location"


def test_trailing_city_anchor_still_resolves_the_place(monkeypatch):
    """#83's own repro shape is untouched: a big-box name followed by the
    city it is in anchors on that city (exact, city-level: strong), and the
    place found there need not repeat the city's name — but a neighbour
    that shares only one word of the *place* name is still noise."""
    _no_session_state(monkeypatch)
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "San Jose": [_division("San Jose", 37.34, -121.89, population=1_000_000)],
    }))
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [
        _place("Westfield Valley Fair", 37.325, -121.946),
        _place("Westfield Oakridge", 37.253, -121.862),
    ])
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [])

    results = geocode.resolve_place("Westfield Valley Fair San Jose", limit=5)
    assert [r["name"] for r in results] == ["Westfield Valley Fair"]
    assert results[0]["match"] == "prefix"


def test_leading_location_word_anchor_requires_the_place_to_carry_it(monkeypatch):
    """#268's leading-word split rests on the location word being part of
    the place's own name ("Palo Alto Caltrain Station"). So a leading anchor
    is never strong: the genuine match contains the word, and the
    coincidence — "Grand Royal", 20 km from a district called Central — does
    not."""
    _no_session_state(monkeypatch)
    monkeypatch.setattr(geocode, "_local_divisions_table", lambda: "local-table")
    monkeypatch.setattr(geocode, "_local_alt_names_table", lambda t: None)
    # A fictional name: the real "Grand Central Terminal" now carries a
    # bundled alias pin (#329), which takes the city-hint path instead.
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "Westoria": [_division("Westoria", 55.1486, 61.3153, population=101_285)],
    }))
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [
        _place("Grand Royal", 55.1087, 61.4224, category="restaurant"),
        _place("Grand Westoria Terminal", 55.15, 61.32, category="train_station"),
    ])
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [])

    details = geocode._fallback_anchor_details(
        "Grand Westoria Terminal", [], None, "local-table"
    )
    assert details[0]["candidate"] == "Westoria" and details[0]["strong"] is False
    results = geocode.resolve_place("Grand Westoria Terminal", limit=5)
    assert [r["name"] for r in results] == ["Grand Westoria Terminal"]


def test_stopword_residual_gate_is_untouched(monkeypatch):
    """#216: an anchor with nothing but stopwords left over still comes back
    with name_query None, so the caller skips the places half entirely."""
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_by_candidate({
        "Met": [_division("Metropolis", 40.7, -73.9)],
    }))
    assert geocode._fallback_anchor("the Met", [], None, None) == (40.7, -73.9, None)
