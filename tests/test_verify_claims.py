"""Listing claim checker: verify_claims (#316).

Places lookups are mocked (same style as test_neighborhood_verdict.py) so
each verdict boundary is exact and deterministic. travel_time claims route
for real against the committed transportation fixture (same grid
test_route.py uses) so the routed-minutes math and the unroutable ->
"unverifiable" path are exercised against genuine Dijkstra output, not a
faked duration.
"""

import pytest

from placeroot import claims, overture, routing, server

from ._routing_fixture import build_routing_fixture as fx

# Known-connected pair on the routing fixture grid (same as test_route.py):
# a straight 3-edge run, 100m spacing -> 300m walked.
ORIGIN_LAT, ORIGIN_LON = fx.node_latlon(2, 2)
MATCH_LAT, MATCH_LON = fx.node_latlon(2, 5)
WALK_DISTANCE_M = 300.0
WALK_MINUTES = WALK_DISTANCE_M / routing.DEFAULT_SPEED_M_S / 60.0  # ~3.57


def _row(distance_m, **extra):
    row = {
        "id": "p-1",
        "name": "The Match",
        "category": "coffee_shop",
        "basic_category": "coffee_shop",
        "distance_m": distance_m,
        "lat": MATCH_LAT,
        "lon": MATCH_LON,
        "operating_status": "in business",
        "confidence": 0.9,
        "brand": None,
        "has_website": False,
        "has_phone": False,
    }
    row.update(extra)
    return row


def _patch_find_places(monkeypatch, rows_by_call=None, rows=None):
    """Stub the overture places lookups claims.py uses.

    Patches both find_places (name-only matches, count_nearby) and
    find_places_for_categories (the exact/hierarchy matcher category claims
    go through). Each recorded call carries which function it hit.
    rows_by_call pops one list per call if given.

    radius_m is claims.py's third positional argument (matching both
    functions' own signatures and the rest of the codebase's call
    convention, e.g. within_distance) — captured positionally here rather
    than assuming a kwarg so a call recorded here reflects the real call
    shape.
    """
    calls = []

    def _fake(fn_name):
        def fake(*args, **kwargs):
            calls.append({"fn": fn_name, "args": args, "kwargs": kwargs, "radius_m": args[2]})
            if rows_by_call is not None:
                return rows_by_call.pop(0)
            return rows if rows is not None else []

        return fake

    monkeypatch.setattr(overture, "find_places", _fake("find_places"))
    monkeypatch.setattr(
        overture, "find_places_for_categories", _fake("find_places_for_categories")
    )
    return calls


# --- travel_time -------------------------------------------------------


def test_travel_time_confirmed_uses_real_routing_fixture(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "coffee_shop", "claimed_minutes": 4.0}],
    )
    row = result["results"][0]
    assert row["verdict"] == "confirmed"
    assert row["measured"]["minutes"] == pytest.approx(WALK_MINUTES, abs=0.05)
    assert row["measured"]["match"]["name"] == "The Match"
    assert row["claim"]["claimed_minutes"] == 4.0


def test_travel_time_stretched(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "coffee_shop", "claimed_minutes": 2.7}],
    )
    assert result["results"][0]["verdict"] == "stretched"


def test_travel_time_false(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "coffee_shop", "claimed_minutes": 1.0}],
    )
    assert result["results"][0]["verdict"] == "false"


def test_travel_time_cycle_mode_routes_for_real(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [
            {
                "kind": "travel_time",
                "to_name": "match",
                "mode": "cycle",
                "claimed_minutes": 1.5,
            }
        ],
    )
    row = result["results"][0]
    expected_minutes = WALK_DISTANCE_M / routing.CYCLE_SPEED_M_S / 60.0
    assert row["measured"]["minutes"] == pytest.approx(expected_minutes, abs=0.05)
    assert row["verdict"] == "confirmed"


def test_travel_time_no_match_is_false_with_note(monkeypatch):
    _patch_find_places(monkeypatch, rows=[])
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "train_station", "claimed_minutes": 8}],
    )
    row = result["results"][0]
    assert row["verdict"] == "false"
    assert row["measured"]["minutes"] is None
    assert "train_station" in row["note"]
    assert "found within" in row["note"]


def test_travel_time_unroutable_is_unverifiable_not_false(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])

    def boom(*_a, **_k):
        raise routing.NoGraphNearby(MATCH_LAT, MATCH_LON, 500, mode="walk")

    monkeypatch.setattr(routing, "route", boom)
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "coffee_shop", "claimed_minutes": 4}],
    )
    row = result["results"][0]
    assert row["verdict"] == "unverifiable"
    assert row["measured"]["minutes"] is None
    assert row["measured"]["match"]["name"] == "The Match"
    assert "note" in row


def test_travel_time_no_route_result_is_unverifiable(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])

    def disconnected(*_a, **_k):
        return {"error": "no_route", "detail": "no path found", "mode": "walk"}

    monkeypatch.setattr(routing, "route", disconnected)
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "coffee_shop", "claimed_minutes": 4}],
    )
    assert result["results"][0]["verdict"] == "unverifiable"


def test_travel_time_category_uses_exact_matcher_not_substring(monkeypatch):
    calls = _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])
    claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "park", "claimed_minutes": 4}],
    )
    assert calls[0]["fn"] == "find_places_for_categories"
    assert calls[0]["args"][3] == ["park"]


def test_travel_time_name_only_still_uses_find_places(monkeypatch):
    calls = _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])
    claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_name": "match", "claimed_minutes": 4}],
    )
    assert calls[0]["fn"] == "find_places"


def test_category_plus_name_filters_exact_matches_by_name(monkeypatch):
    rows = [
        _row(50, name="Central Parking"),
        _row(80, name="Riverside Park"),
    ]
    _patch_find_places(monkeypatch, rows=rows)
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [
            {
                "kind": "distance",
                "to_category": "park",
                "to_name": "Riverside",
                "claimed_max_m": 100,
            }
        ],
    )
    row = result["results"][0]
    assert row["measured"]["match"]["name"] == "Riverside Park"
    assert row["measured"]["distance_m"] == 80


def test_travel_time_truncated_route_failing_threshold_is_unverifiable(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])

    def truncated_route(*_a, **_k):
        return {
            "distance_m": 3000.0,
            "duration_s": 3000.0 / routing.DEFAULT_SPEED_M_S,
            "mode": "walk",
            "truncated": True,
            "note": "the street graph hit its size cap",
        }

    monkeypatch.setattr(routing, "route", truncated_route)
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "coffee_shop", "claimed_minutes": 4}],
    )
    row = result["results"][0]
    assert row["verdict"] == "unverifiable"
    assert "size cap" in row["note"]


def test_travel_time_truncated_route_that_still_confirms_stays_confirmed(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(WALK_DISTANCE_M)])

    def truncated_route(*_a, **_k):
        return {
            "distance_m": WALK_DISTANCE_M,
            "duration_s": WALK_DISTANCE_M / routing.DEFAULT_SPEED_M_S,
            "mode": "walk",
            "truncated": True,
            "note": "the street graph hit its size cap",
        }

    monkeypatch.setattr(routing, "route", truncated_route)
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "travel_time", "to_category": "coffee_shop", "claimed_minutes": 4}],
    )
    row = result["results"][0]
    assert row["verdict"] == "confirmed"
    assert "size cap" in row["note"]


# --- count_nearby --------------------------------------------------------


def test_count_nearby_confirmed_stretched_false(monkeypatch):
    calls = _patch_find_places(
        monkeypatch,
        rows_by_call=[
            [_row(50)] * 5,
            [_row(50)] * 3,
            [_row(50)] * 1,
        ],
    )
    claim = {"kind": "count_nearby", "category": "shop", "claimed_at_least": 5}
    confirmed = claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [claim])["results"][0]
    stretched = claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [claim])["results"][0]
    false = claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [claim])["results"][0]
    assert confirmed["verdict"] == "confirmed"
    assert confirmed["measured"]["count"] == 5
    assert stretched["verdict"] == "stretched"
    assert stretched["measured"]["count"] == 3
    assert false["verdict"] == "false"
    assert false["measured"]["count"] == 1
    assert len(calls) == 3


def test_count_nearby_radius_defaults_and_caps(monkeypatch):
    calls = _patch_find_places(monkeypatch, rows=[])
    claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "count_nearby", "name": "shop", "claimed_at_least": 1}],
    )
    assert calls[0]["radius_m"] == claims_module_default_radius()
    calls.clear()
    claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [
            {
                "kind": "count_nearby",
                "name": "shop",
                "radius_m": 999999,
                "claimed_at_least": 1,
            }
        ],
    )
    assert calls[0]["radius_m"] == claims.MAX_COUNT_RADIUS_M


def claims_module_default_radius():
    return claims.DEFAULT_COUNT_RADIUS_M


def test_count_nearby_zero_count_is_false(monkeypatch):
    _patch_find_places(monkeypatch, rows=[])
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "count_nearby", "category": "grocery_store", "claimed_at_least": 2}],
    )
    row = result["results"][0]
    assert row["verdict"] == "false"
    assert row["measured"]["count"] == 0


def test_count_nearby_capped_result_with_larger_claim_is_unverifiable(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(50)] * overture.MAX_ROWS)
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "count_nearby", "category": "restaurant", "claimed_at_least": 40}],
    )
    row = result["results"][0]
    assert row["verdict"] == "unverifiable"
    assert row["measured"]["count"] == overture.MAX_ROWS
    assert "capped" in row["note"]


def test_count_nearby_capped_result_meeting_claim_stays_confirmed(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(50)] * overture.MAX_ROWS)
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "count_nearby", "category": "restaurant", "claimed_at_least": 20}],
    )
    row = result["results"][0]
    assert row["verdict"] == "confirmed"
    assert "capped" in row["note"]


# --- distance --------------------------------------------------------


def test_distance_confirmed_stretched_false(monkeypatch):
    claim_100 = {"kind": "distance", "to_category": "park", "claimed_max_m": 100}
    for distance_m, expected in ((100, "confirmed"), (140, "stretched"), (200, "false")):
        _patch_find_places(monkeypatch, rows=[_row(distance_m)])
        result = claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [claim_100])
        assert result["results"][0]["verdict"] == expected
        assert result["results"][0]["measured"]["distance_m"] == distance_m


def test_distance_no_match_is_false_with_note(monkeypatch):
    _patch_find_places(monkeypatch, rows=[])
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "distance", "to_name": "Riverside Park", "claimed_max_m": 150}],
    )
    row = result["results"][0]
    assert row["verdict"] == "false"
    assert row["measured"]["distance_m"] is None
    assert "Riverside Park" in row["note"]


def test_distance_search_radius_is_capped(monkeypatch):
    calls = _patch_find_places(monkeypatch, rows=[])
    result = claims.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "distance", "to_category": "airport", "claimed_max_m": 250000}],
    )
    assert calls[0]["radius_m"] == claims.MAX_DISTANCE_SEARCH_RADIUS_M
    # The not-found note reports the radius actually searched, not the
    # uncapped 2x-claim bound.
    note = result["results"][0]["note"]
    assert f"{int(claims.MAX_DISTANCE_SEARCH_RADIUS_M)}m" in note


# --- shared shape ----------------------------------------------------


def test_claim_is_echoed_intact(monkeypatch):
    _patch_find_places(monkeypatch, rows=[_row(50)])
    claim = {"kind": "distance", "to_category": "park", "claimed_max_m": 100, "extra": "x"}
    result = claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [claim])
    assert result["results"][0]["claim"] == claim


def test_verdict_rule_is_present_and_nonempty():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(overture, "find_places_for_categories", lambda *_a, **_k: [_row(50)])
        result = claims.verify_claims(
            ORIGIN_LAT,
            ORIGIN_LON,
            [{"kind": "distance", "to_category": "park", "claimed_max_m": 100}],
        )
    assert result["verdict_rule"]
    assert isinstance(result["verdict_rule"], str)


# --- validation --------------------------------------------------------


def test_unknown_kind_is_a_claim_error():
    with pytest.raises(claims.ClaimError, match="kind"):
        claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [{"kind": "teleport"}])


def test_too_many_claims_is_a_claim_error():
    good = {"kind": "distance", "to_category": "park", "claimed_max_m": 100}
    with pytest.raises(claims.ClaimError, match="8"):
        claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [good] * 9)


def test_too_many_travel_time_claims_is_a_claim_error():
    tt = {"kind": "travel_time", "to_category": "coffee_shop", "claimed_minutes": 5}
    with pytest.raises(claims.ClaimError, match="travel_time"):
        claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [tt] * 6)


def test_missing_claimed_minutes_is_a_claim_error():
    with pytest.raises(claims.ClaimError, match="claimed_minutes"):
        claims.verify_claims(
            ORIGIN_LAT,
            ORIGIN_LON,
            [{"kind": "travel_time", "to_category": "coffee_shop"}],
        )


def test_missing_claimed_at_least_is_a_claim_error():
    with pytest.raises(claims.ClaimError, match="claimed_at_least"):
        claims.verify_claims(
            ORIGIN_LAT,
            ORIGIN_LON,
            [{"kind": "count_nearby", "category": "shop"}],
        )


def test_missing_claimed_max_m_is_a_claim_error():
    with pytest.raises(claims.ClaimError, match="claimed_max_m"):
        claims.verify_claims(
            ORIGIN_LAT,
            ORIGIN_LON,
            [{"kind": "distance", "to_category": "park"}],
        )


def test_both_to_category_and_to_name_absent_is_a_claim_error():
    with pytest.raises(claims.ClaimError):
        claims.verify_claims(
            ORIGIN_LAT,
            ORIGIN_LON,
            [{"kind": "travel_time", "claimed_minutes": 5}],
        )
    with pytest.raises(claims.ClaimError):
        claims.verify_claims(
            ORIGIN_LAT,
            ORIGIN_LON,
            [{"kind": "distance", "claimed_max_m": 100}],
        )


def test_bad_mode_is_a_claim_error():
    with pytest.raises(claims.ClaimError, match="mode"):
        claims.verify_claims(
            ORIGIN_LAT,
            ORIGIN_LON,
            [
                {
                    "kind": "travel_time",
                    "to_category": "coffee_shop",
                    "mode": "teleport",
                    "claimed_minutes": 5,
                }
            ],
        )


def test_empty_claims_list_is_a_claim_error():
    with pytest.raises(claims.ClaimError):
        claims.verify_claims(ORIGIN_LAT, ORIGIN_LON, [])


# --- server wrapper ----------------------------------------------------


def test_server_wraps_claim_error_as_bad_request():
    result = server.verify_claims(ORIGIN_LAT, ORIGIN_LON, [{"kind": "nope"}])
    assert result["error"] == "bad_request"


def test_server_invalid_coords_is_bad_request():
    result = server.verify_claims(
        91.0,
        ORIGIN_LON,
        [{"kind": "distance", "to_category": "park", "claimed_max_m": 100}],
    )
    assert result["error"] == "bad_request"


def test_server_upstream_failure_is_structured(monkeypatch):
    def boom(*_a, **_k):
        raise overture.UpstreamUnavailable("scan failed")

    monkeypatch.setattr(overture, "find_places_for_categories", boom)
    result = server.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "distance", "to_category": "park", "claimed_max_m": 100}],
    )
    assert result["error"] == "upstream_unavailable"


def test_server_returns_results_and_verdict_rule(monkeypatch):
    monkeypatch.setattr(overture, "find_places_for_categories", lambda *_a, **_k: [_row(50)])
    result = server.verify_claims(
        ORIGIN_LAT,
        ORIGIN_LON,
        [{"kind": "distance", "to_category": "park", "claimed_max_m": 100}],
    )
    assert "error" not in result
    assert result["results"][0]["verdict"] == "confirmed"
    assert result["verdict_rule"]
