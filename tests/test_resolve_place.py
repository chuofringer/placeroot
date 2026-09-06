"""Tests for resolve_place (#22): free-text -> ranked, typed GERS ids."""

from placeroot import cache, geo, geocode, overture, server

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
    def fake_geocode(query, limit=5, lang=None, near=None):
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


# --- #476: a city pin bounds geocode's own search, not just its output -------

_SG_LAT, _SG_LON = 1.2899, 103.8519
_MARINA_CA = {
    "id": "d-marina-ca", "name": "Marina", "subtype": "locality", "country": "US",
    "region": "US-CA", "lat": 36.684402, "lon": -121.802185, "admin_context": [],
    "population": 22_000,
}


def _pinned_resolve_doubles(monkeypatch, fake_fallback=True):
    """The "Marina Bay Sands Singapore" shape, offline: no division is named
    for the whole place half, but the speculative split "Marina" matches a
    Californian locality outright — the row the live run anchored on. Returns
    the recorded division-query kwargs, places-fallback anchors and whether
    the speculative anchor machinery ran at all."""
    seen = {"division_kwargs": [], "fallback_anchors": [], "speculative_calls": 0}

    def fake_divisions(query, region_code, local_table, **kwargs):
        seen["division_kwargs"].append(kwargs)
        return [dict(_MARINA_CA)] if query == "Marina" else []

    def fake_places_fallback(query, anchor=None, also=None, schedule_tiles=True):
        seen["fallback_anchors"].append(anchor)
        return []

    real_candidates = geocode._fallback_anchor_candidates

    def counting_candidates(*a, **k):
        seen["speculative_calls"] += 1
        return real_candidates(*a, **k)

    monkeypatch.setattr(geocode, "_query_divisions", fake_divisions)
    if fake_fallback:
        monkeypatch.setattr(geocode, "_query_places_fallback", fake_places_fallback)
    monkeypatch.setattr(geocode, "_fallback_anchor_candidates", counting_candidates)
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [])
    return seen


def test_city_pin_is_passed_into_the_division_pass_as_a_constraint(monkeypatch):
    """The pin used to be applied only as a post-filter on geocode()'s rows,
    after the unconstrained division pass (and everything it triggered) had
    already run. Now every division query under a pin carries it."""
    seen = _pinned_resolve_doubles(monkeypatch)

    geocode.resolve_place("Marina Bay Sands", near_lat=_SG_LAT, near_lon=_SG_LON, limit=3)

    assert seen["division_kwargs"], "the division pass ran"
    for kwargs in seen["division_kwargs"]:
        assert kwargs.get("near") == (_SG_LAT, _SG_LON, geocode._CITY_HINT_RADIUS_M), kwargs


def test_far_division_never_anchors_the_places_fallback_under_a_pin(monkeypatch):
    """Under a pin the pin is the anchor. The trailing/leading-word splits
    that found Marina, California (15,000 km from Singapore) must not run,
    and no places scan may be aimed anywhere but the pin."""
    seen = _pinned_resolve_doubles(monkeypatch)

    geocode.resolve_place("Marina Bay Sands", near_lat=_SG_LAT, near_lon=_SG_LON, limit=3)

    assert seen["speculative_calls"] == 0
    assert seen["fallback_anchors"], "the anchored places fallback still runs, at the pin"
    for anchor in seen["fallback_anchors"]:
        assert anchor is not None
        assert geo.haversine_m(anchor[0], anchor[1], _SG_LAT, _SG_LON) < 1_000, anchor


def test_pinned_resolve_schedules_tiles_only_around_the_pin(geocode_cache, monkeypatch):
    """The user-visible half of #476: a Singapore query left a California
    places tile (and its base-theme siblings) queued for download. Every
    cache resolution a pinned resolve performs must be for a box around the
    pin, and for the places theme (the recreation layer is off in the
    offline suite, so nothing else has a reason to be touched)."""
    # The real places fallback runs here, so its cache resolution is observed.
    seen = _pinned_resolve_doubles(monkeypatch, fake_fallback=False)
    resolutions = []

    def recording_paths(con, release, theme, bbox, upstream_glob, *a, **k):
        resolutions.append((theme, bbox, k.get("schedule_missing", True)))
        return None  # "nothing cached" — the caller falls through to upstream

    monkeypatch.setattr(cache, "local_paths_for_query", recording_paths)

    geocode.resolve_place("Marina Bay Sands", near_lat=_SG_LAT, near_lon=_SG_LON, limit=3)

    assert resolutions, "the pinned fallback resolved the cache for the pin's box"
    for theme, (xmin, ymin, xmax, ymax), _scheduled in resolutions:
        assert theme == "places", theme
        assert xmin <= _SG_LON <= xmax and ymin <= _SG_LAT <= ymax, (theme, xmin, ymin, xmax, ymax)
    assert seen["speculative_calls"] == 0



def test_no_pin_query_runs_the_division_pass_unconstrained(monkeypatch):
    """No city, no near hint: today's global behaviour, byte-for-byte — the
    division pass is called with the pre-#476 signature."""
    seen = _pinned_resolve_doubles(monkeypatch)

    geocode.resolve_place("Marina Bay Sands", limit=3)

    assert seen["division_kwargs"]
    for kwargs in seen["division_kwargs"]:
        assert "near" not in kwargs, kwargs
    assert seen["speculative_calls"] >= 1
