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
    assert note == "High confidence, recently confirmed in listings"
    assert "call ahead" not in note.lower()


def test_high_confidence_raw_open_status_is_the_same():
    """Callers may still pass the raw Overture value before relabeling."""
    assert honesty.trust_note({
        "confidence": 0.9, "operating_status": "open",
    }) == "High confidence, recently confirmed in listings"


def test_low_confidence_uses_call_ahead_wording():
    note = honesty.trust_note({
        "name": "Mystery Café",
        "confidence": 0.21,
        "operating_status": "in business",
    })
    assert note == "Low confidence, unnamed source — call ahead."
    assert "high confidence" not in note.lower()


def test_unknown_confidence_uses_call_ahead_wording():
    note = honesty.trust_note({"name": "Unscored Stand", "operating_status": None})
    assert note == "Low confidence, unnamed source — call ahead."


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


def test_moderate_confidence_is_call_ahead_not_high():
    note = honesty.trust_note({"confidence": 0.62, "operating_status": "in business"})
    assert note == "Moderate confidence — call ahead."
    assert "high confidence" not in note.lower()


def test_attach_trust_note_sets_the_field():
    row = {"name": "Cafe", "confidence": 0.9, "operating_status": "in business"}
    out = honesty.attach_trust_note(row)
    assert out is row
    assert row["trust_note"] == "High confidence, recently confirmed in listings"


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
