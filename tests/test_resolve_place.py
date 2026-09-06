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


def test_place_match_label_separates_containment_from_a_shared_word():
    """#475: a name that holds the caller's whole query is "contains"; a
    name that merely shares one significant word with it is "substring".
    Before this they were both "substring" and an aesthetics clinic
    ("marina") tied with the hotel whose name contains "Marina Bay Sands"."""
    label = geocode._place_match_label
    assert label("Skypark#Marina Bay Sands Hotel,Singapore.", "Marina Bay Sands") == "contains"
    assert label("Freia Aesthetics | Marina Square", "Marina Bay Sands") == "substring"
    assert label("Marina Bay Sands Convention Centre", "Marina Bay Sands") == "prefix"
    assert label("Marina Bay Sands", "Marina Bay Sands") == "exact"
    # The #22 shape — the query carries context the name doesn't. With the
    # name leading the query it is a prefix, as before; with the name sitting
    # mid-query (the reverse-containment branch) it is whole-name containment
    # too, so "contains" — both directions beat a one-word coincidence.
    assert label("Mañana Coffee", "Mañana coffee Austin") == "prefix"
    assert label("Blue Bottle Roastery", "the Blue Bottle Roastery Austin") == "contains"
    assert label("Freia Aesthetics", "Marina Bay Sands") is None


def test_match_label_rank_orders_contains_between_prefix_and_substring():
    rank = geocode._MATCH_LABEL_RANK
    assert rank["exact"] > rank["prefix"] > rank["contains"] > rank["substring"] > rank["fuzzy"]
    assert set(rank) == {"exact", "prefix", "contains", "substring", "fuzzy"}


def test_containing_name_outranks_shared_word_name_nearer_the_pin(monkeypatch):
    """#475 end to end: the city pin sits 1.0 km from a clinic sharing one
    word with the query and 1.1 km from the hotel whose name contains all
    of it, and the clinic carries the higher confidence. Km-rounded distance
    ties (both "1"), so under a shared "substring" label prominence picked
    the clinic; "contains" decides before either tie-break is consulted."""
    pin_lat, pin_lon = 1.2899, 103.8519
    monkeypatch.setattr(geocode, "geocode", lambda query, limit=5, lang=None: [])

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
        return [
            {
                "id": "place-clinic", "name": "Freia Aesthetics | Marina Square",
                "category": "beauty_salon", "basic_category": "beauty_salon",
                "operating_status": "open", "confidence": 0.95,
                "lat": pin_lat + 0.0090, "lon": pin_lon, "distance_m": 1000,
            },
            {
                "id": "place-hotel", "name": "Skypark#Marina Bay Sands Hotel,Singapore.",
                "category": "hotel", "basic_category": "hotel",
                "operating_status": "open", "confidence": 0.5,
                "lat": pin_lat + 0.0099, "lon": pin_lon, "distance_m": 1100,
            },
        ]

    monkeypatch.setattr(overture, "find_places", fake_find_places)

    results = geocode.resolve_place(
        "Marina Bay Sands", near_lat=pin_lat, near_lon=pin_lon, limit=5
    )
    assert [r["id"] for r in results] == ["place-hotel", "place-clinic"]
    assert results[0]["match"] == "contains"
    assert results[1]["match"] == "substring"
