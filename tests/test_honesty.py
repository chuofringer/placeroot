"""Calibrated before-you-go trust notes (issue #308).

The formatter is string-only: these tests never touch the network, and the
fixture integration cases reuse the committed places parquet already wired
up by conftest.
"""

from placeroot import honesty, overture, server

from .conftest import CENTER_LAT, CENTER_LON


def test_high_confidence_uses_high_trust_wording():
    note = honesty.trust_note({
        "name": "Blue Bottle",
        "confidence": 0.92,
        "operating_status": "in business",
    })
    # Compact shape: no `sources` key, so no listing-confirmation claim.
    assert note == "High confidence"
    assert "call ahead" not in note.lower()
    assert "recently confirmed" not in note.lower()
    assert "unnamed source" not in note.lower()


def test_high_confidence_raw_open_status_is_the_same():
    """Callers may still pass the raw Overture value before relabeling."""
    assert honesty.trust_note({
        "confidence": 0.9, "operating_status": "open",
    }) == "High confidence"


def test_low_confidence_uses_call_ahead_wording():
    note = honesty.trust_note({
        "name": "Mystery Café",
        "confidence": 0.21,
        "operating_status": "in business",
    })
    # Compact shape: low band still asks to call ahead, without inventing
    # an "unnamed source" the row never carried.
    assert note == "Low confidence — call ahead."
    assert "high confidence" not in note.lower()
    assert "unnamed source" not in note.lower()


def test_unknown_confidence_uses_call_ahead_wording():
    note = honesty.trust_note({"name": "Unscored Stand", "operating_status": None})
    assert note == "Low confidence — call ahead."
    assert "unnamed source" not in note.lower()


def test_permanently_closed_dominates_high_confidence():
    note = honesty.trust_note({
        "name": "Shuttered Café",
        "confidence": 0.99,
        "operating_status": "permanently closed",
    })
    assert note == "Listed as permanently closed — verify before going."


def test_temporarily_closed_is_call_ahead():
    note = honesty.trust_note({
        "confidence": 0.88,
        "operating_status": "temporarily closed",
    })
    assert note == "Listed as temporarily closed — call ahead."


def test_high_confidence_empty_sources_is_call_ahead():
    note = honesty.trust_note({
        "confidence": 0.91,
        "operating_status": "in business",
        "sources": [],
    })
    assert note == "High confidence, unnamed source — call ahead."


def test_high_confidence_named_source_stays_high_trust():
    note = honesty.trust_note({
        "confidence": 0.91,
        "operating_status": "in business",
        "sources": [{"dataset": "meta", "record_id": "meta-001"}],
    })
    assert note == "High confidence, recently confirmed in listings"


def test_absent_sources_never_claim_listings_or_unnamed():
    """High and low bands treat a missing `sources` key the same way."""
    high = honesty.trust_note({"confidence": 0.9, "operating_status": "in business"})
    low = honesty.trust_note({"confidence": 0.2, "operating_status": "in business"})
    for note in (high, low):
        assert "recently confirmed" not in note.lower()
        assert "unnamed source" not in note.lower()
    assert high == "High confidence"
    assert low == "Low confidence — call ahead."


def test_low_confidence_empty_sources_keeps_unnamed_source():
    note = honesty.trust_note({
        "confidence": 0.2,
        "operating_status": "in business",
        "sources": [],
    })
    assert note == "Low confidence, unnamed source — call ahead."


def test_moderate_confidence_is_call_ahead_not_high():
    note = honesty.trust_note({"confidence": 0.62, "operating_status": "in business"})
    assert note == "Moderate confidence — call ahead."
    assert "high confidence" not in note.lower()


def test_attach_trust_note_sets_the_field():
    row = {"name": "Cafe", "confidence": 0.9, "operating_status": "in business"}
    out = honesty.attach_trust_note(row)
    assert out is row
    assert row["trust_note"] == "High confidence"


def test_verify_before_going_picks_the_weakest_stops():
    places = [
        {"name": "Solid Bakery", "confidence": 0.95, "operating_status": "in business"},
        {"name": "Shaky Cart", "confidence": 0.18, "operating_status": "in business"},
        {"name": "Old Diner", "confidence": 0.88, "operating_status": "permanently closed"},
        {"name": "Also Fine", "confidence": 0.80, "operating_status": "in business"},
    ]
    line = honesty.verify_before_going(places)
    assert line is not None
    assert line.startswith("Verify before going:")
    assert "Old Diner" in line
    assert "Shaky Cart" in line
    assert "Solid Bakery" not in line
    assert "Also Fine" not in line
    # Closed ranks ahead of merely low-confidence.
    diner_at = line.index("Old Diner")
    cart_at = line.index("Shaky Cart")
    assert diner_at < cart_at


def test_verify_before_going_names_only_one_when_only_one_is_weak():
    places = [
        {"name": "Good Spot", "confidence": 0.9, "operating_status": "in business"},
        {"name": "Risky Spot", "confidence": 0.2, "operating_status": "in business"},
    ]
    line = honesty.verify_before_going(places)
    assert line == "Verify before going: Risky Spot (low confidence)."


def test_verify_before_going_omits_all_high_confidence_open_stops():
    places = [
        {"name": "A", "confidence": 0.9, "operating_status": "in business"},
        {"name": "B", "confidence": 0.85, "operating_status": "open"},
    ]
    assert honesty.verify_before_going(places) is None


def test_verify_before_going_empty_is_none():
    assert honesty.verify_before_going([]) is None
    assert honesty.verify_before_going(None) is None


def test_attach_verify_line_skips_error_payloads():
    payload = {"error": "not_found", "results": [
        {"name": "X", "confidence": 0.1},
    ]}
    assert "verify_before_going" not in honesty.attach_verify_line(payload)


def test_attach_verify_line_adds_field_when_a_stop_is_weak():
    payload = {"results": [
        {"name": "A", "confidence": 0.9, "operating_status": "in business"},
        {"name": "B", "confidence": 0.1, "operating_status": "in business"},
    ]}
    honesty.attach_verify_line(payload)
    assert payload["verify_before_going"] == "Verify before going: B (low confidence)."


def test_optimize_route_verify_line_uses_caller_supplied_stop_fields():
    """No extra lookup: only fields the caller already put on the stops."""
    stops = [
        {"lat": 40.702, "lon": -73.900, "name": "North", "confidence": 0.95,
         "operating_status": "in business"},
        {"lat": 40.698, "lon": -73.900, "name": "South", "confidence": 0.12,
         "operating_status": "in business"},
    ]
    # Two close fixture-grid points would need the routing fixture; this
    # test only checks the honesty wrapper, so call the helper directly
    # the same way server.optimize_route does after routing returns.
    line = honesty.verify_before_going(stops)
    assert line == "Verify before going: South (low confidence)."


# --- fixture integration: the attach path, still offline -------------------


def test_find_places_rows_carry_a_trust_note():
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=200, limit=10)
    assert results
    for row in results:
        assert "trust_note" in row
        assert row["trust_note"]
    for row in results:
        # Compact find_places omits `sources` — wording must not invent it.
        if "sources" not in row:
            assert "recently confirmed" not in row["trust_note"]
            assert "unnamed source" not in row["trust_note"]
    high = [r for r in results if (r.get("confidence") or 0) >= honesty.HIGH_CONFIDENCE
            and r.get("operating_status") == "in business"]
    if high:
        assert "High confidence" in high[0]["trust_note"]
    closed = [r for r in results if r.get("operating_status") == "permanently closed"]
    if closed:
        assert "permanently closed" in closed[0]["trust_note"].lower()


def test_server_find_places_surfaces_trust_note():
    payload = server.find_places(CENTER_LAT, CENTER_LON, radius_m=200, limit=5)
    assert "results" in payload
    assert payload["results"]
    assert all(r.get("trust_note") for r in payload["results"])


def test_plan_errands_prompt_asks_for_a_verify_line():
    from placeroot import prompts
    text = prompts._plan_errands("pharmacy, hardware store", "Union Square")
    assert "Verify before going:" in text
    assert "weakest-confidence" in text


def test_server_optimize_route_adds_verify_line_from_stop_fields():
    from ._routing_fixture import build_routing_fixture as fx
    a = fx.node_latlon(2, 2)
    b = fx.node_latlon(2, 5)
    result = server.optimize_route(
        [
            {"lat": a[0], "lon": a[1], "name": "Home", "confidence": 0.95,
             "operating_status": "in business"},
            {"lat": b[0], "lon": b[1], "name": "Shaky Café", "confidence": 0.11,
             "operating_status": "in business"},
        ],
        mode="walk",
        roundtrip=False,
    )
    assert "error" not in result
    assert result["verify_before_going"] == "Verify before going: Shaky Café (low confidence)."


def test_verify_before_going_skips_bare_coordinate_stops():
    """optimize_route's normal input is {lat, lon} — not a place lookup."""
    stops = [
        {"lat": 40.702, "lon": -73.900},
        {"lat": 40.698, "lon": -73.900},
    ]
    assert honesty.verify_before_going(stops) is None


def test_verify_before_going_mixed_bare_and_enriched_keeps_original_index():
    stops = [
        {"lat": 40.702, "lon": -73.900},
        {"lat": 40.698, "lon": -73.900, "confidence": 0.12,
         "operating_status": "in business"},
    ]
    line = honesty.verify_before_going(stops)
    assert line == "Verify before going: stop 2 (low confidence)."


def test_server_optimize_route_bare_coords_have_no_verify_line():
    from ._routing_fixture import build_routing_fixture as fx
    a = fx.node_latlon(2, 2)
    b = fx.node_latlon(2, 5)
    result = server.optimize_route(
        [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
        mode="walk",
        roundtrip=False,
    )
    assert "error" not in result
    assert "verify_before_going" not in result


def test_verify_line_uses_pre_truncation_rows_not_stripped_confidence():
    """Budget strips confidence first; the verify line must not re-read that as low."""
    from placeroot import budget as budget_mod

    row = {
        "name": "Solid Cafe",
        "confidence": 0.95,
        "operating_status": "in business",
        "trust_note": "High confidence",
        "category": "coffee_shop",
        "basic_category": "coffee_shop",
        "lat": 40.702,
        "lon": -73.900,
        "detour_m": 12,
        "along_m": 400,
    }
    payload = {
        "results": [dict(row)],
        "route": {"distance_m": 800, "duration_s": 600, "mode": "walk"},
    }
    honesty.attach_verify_line(payload)
    assert "verify_before_going" not in payload

    envelope = budget_mod.estimate_tokens({**payload, "results": []})
    row_full = budget_mod.estimate_tokens([row])
    row_no_conf = budget_mod.estimate_tokens(
        [{k: v for k, v in row.items() if k != "confidence"}]
    )
    assert row_full > row_no_conf
    budgeted = budget_mod.apply_budget(
        payload, "results", budget_tokens=envelope + row_no_conf
    )
    assert budgeted["results"]
    assert "confidence" not in budgeted["results"][0]
    assert "operating_status" in budgeted["results"][0]
    assert "verify_before_going" not in budgeted

    # Same rows after stripping *would* invent a low-confidence warning —
    # that's the inversion the review called out, and why we attach first.
    inverted = honesty.attach_verify_line(dict(budgeted))
    assert "verify_before_going" in inverted
    assert "Solid Cafe" in inverted["verify_before_going"]


def test_verify_line_for_low_confidence_is_kept_through_budget():
    from placeroot import budget as budget_mod

    row = {
        "name": "Shaky Cart",
        "confidence": 0.11,
        "operating_status": "in business",
        "trust_note": "Low confidence — call ahead.",
        "lat": 40.702,
        "lon": -73.900,
    }
    payload = {"results": [dict(row)]}
    honesty.attach_verify_line(payload)
    assert payload["verify_before_going"] == "Verify before going: Shaky Cart (low confidence)."
    budgeted = budget_mod.apply_budget(payload, "results", budget_tokens=10_000)
    assert budgeted["verify_before_going"] == payload["verify_before_going"]


def test_server_places_along_route_verify_line_uses_pre_budget_rows(monkeypatch):
    """server.places_along_route must attach the line before apply_budget."""
    from placeroot import budget as budget_mod
    from placeroot import routing

    row = {
        "name": "Solid Cafe",
        "confidence": 0.95,
        "operating_status": "in business",
        "trust_note": "High confidence",
        "category": "coffee_shop",
        "basic_category": "coffee_shop",
        "lat": 40.702,
        "lon": -73.900,
        "detour_m": 12,
        "along_m": 400,
    }
    raw = {
        "results": [dict(row)],
        "route": {"distance_m": 800, "duration_s": 600, "mode": "walk"},
    }

    monkeypatch.setattr(
        routing, "places_along_route",
        lambda *a, **k: {
            "results": [dict(row)],
            "route": {"distance_m": 800, "duration_s": 600, "mode": "walk"},
        },
    )
    envelope = budget_mod.estimate_tokens({**raw, "results": []})
    row_no_conf = budget_mod.estimate_tokens(
        [{k: v for k, v in row.items() if k != "confidence"}]
    )
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", str(envelope + row_no_conf))

    result = server.places_along_route(40.7, -73.9, 40.71, -73.91, mode="walk")
    assert "error" not in result
    assert result["results"]
    assert "confidence" not in result["results"][0]
    assert "operating_status" in result["results"][0]
    assert "verify_before_going" not in result
