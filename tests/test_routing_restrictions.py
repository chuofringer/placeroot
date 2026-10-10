"""Unit coverage for build_graph's per-segment rule helpers — _oneway_allowed
(access_restrictions) and _speed_limit_m_s (speed_limits) — against
fixture-shaped dicts, i.e. the row shapes Overture's transportation
segments actually carry (see scripts/build_routing_fixture.py's
build_oneway_rows for the canonical shape). No network, no graph build.
"""

import pytest

from placeroot import routing


def _rule(access_type="denied", heading=None, mode=None, between=None, **when_extra):
    when = {"heading": heading, "mode": mode or []}
    when.update(when_extra)
    return {"access_type": access_type, "when": when, "between": between}


# --- Mode scoping (travelMode enum) -----------------------------------------


def test_mode_tokens_follow_overture_travel_mode_enum():
    # schema/transportation/segment.yaml: motor_vehicle includes car, truck and
    # motorcycle; "vehicle" is every vehicle; the old camelCase "motorVehicle"
    # never matched a real row.
    assert routing.RESTRICTION_MODE_TOKENS["drive"] == {"vehicle", "motor_vehicle", "car"}
    assert routing.RESTRICTION_MODE_TOKENS["cycle"] == {"vehicle", "bicycle"}
    assert "motorVehicle" not in routing.RESTRICTION_MODE_TOKENS["drive"]


@pytest.mark.parametrize("token", ["vehicle", "motor_vehicle", "car"])
def test_drive_honours_car_group_tokens(token):
    rules = [_rule(heading="backward", mode=[token])]
    assert routing._oneway_allowed(rules, "drive") == (True, False)


@pytest.mark.parametrize("token", ["truck", "hgv", "motorcycle", "bus", "hov", "emergency"])
def test_drive_ignores_rules_scoped_to_other_vehicle_types(token):
    # A truck ban (the common residential-street case) must not drop or
    # direct the segment for cars.
    assert routing._oneway_allowed([_rule(mode=[token])], "drive") == (True, True)
    assert routing._oneway_allowed([_rule(heading="forward", mode=[token])], "drive") == (
        True,
        True,
    )


def test_drive_applies_mixed_list_when_any_token_is_a_car_token():
    rules = [_rule(mode=["hgv", "car"])]
    assert routing._oneway_allowed(rules, "drive") == (False, False)


@pytest.mark.parametrize("token", ["vehicle", "bicycle"])
def test_cycle_honours_bicycle_and_all_vehicle_tokens(token):
    assert routing._oneway_allowed([_rule(heading="forward", mode=[token])], "cycle") == (
        False,
        True,
    )


@pytest.mark.parametrize("token", ["motor_vehicle", "car", "foot", "hgv"])
def test_cycle_ignores_non_bicycle_rules(token):
    assert routing._oneway_allowed([_rule(mode=[token])], "cycle") == (True, True)


def test_rule_without_mode_list_applies_to_every_vehicle_mode():
    rules = [_rule(heading="backward")]
    assert routing._oneway_allowed(rules, "drive") == (True, False)
    assert routing._oneway_allowed(rules, "cycle") == (True, False)
    assert routing._oneway_allowed(rules, "walk") == (True, True)


def test_walk_never_consults_restrictions():
    assert routing._oneway_allowed([_rule()], "walk") == (True, True)


# --- allowed/designated exceptions ------------------------------------------


def test_contraflow_bicycle_lane_reopens_backward_for_cycle_only():
    rules = [
        _rule("denied", heading="backward"),
        _rule("allowed", heading="backward", mode=["bicycle"]),
    ]
    assert routing._oneway_allowed(rules, "cycle") == (True, True)
    assert routing._oneway_allowed(rules, "drive") == (True, False)


def test_exception_overrides_denied_regardless_of_entry_order():
    rules = [
        _rule("designated", heading="backward", mode=["bicycle"]),
        _rule("denied", heading="backward"),
    ]
    assert routing._oneway_allowed(rules, "cycle") == (True, True)


def test_exception_without_heading_reopens_both_directions():
    rules = [_rule("denied"), _rule("allowed", mode=["bicycle"])]
    assert routing._oneway_allowed(rules, "cycle") == (True, True)
    assert routing._oneway_allowed(rules, "drive") == (False, False)


def test_exception_only_reopens_the_direction_it_names():
    rules = [_rule("denied"), _rule("allowed", heading="forward", mode=["car"])]
    assert routing._oneway_allowed(rules, "drive") == (True, False)


def test_other_access_types_are_not_interpreted():
    assert routing._oneway_allowed([_rule("private")], "drive") == (True, True)
    assert routing._oneway_allowed([{"access_type": None}], "drive") == (True, True)


# --- between (sub-range) rules ----------------------------------------------


def test_rule_with_sub_range_between_does_not_apply_to_whole_segment():
    rules = [_rule(between=[0.0, 0.3])]
    assert routing._oneway_allowed(rules, "drive") == (True, True)
    assert routing._oneway_allowed(rules, "cycle") == (True, True)


def test_rule_with_full_between_range_counts_as_whole_segment():
    rules = [_rule(heading="forward", between=[0.0, 1.0])]
    assert routing._oneway_allowed(rules, "drive") == (False, True)


def test_sub_range_exception_does_not_reopen_a_whole_segment_denial():
    rules = [_rule("denied"), _rule("allowed", mode=["car"], between=[0.2, 0.4])]
    assert routing._oneway_allowed(rules, "drive") == (False, False)


@pytest.mark.parametrize("between", [None, [], (0.0, 1.0), [0, 1]])
def test_covers_whole_segment_accepts_whole_segment_shapes(between):
    assert routing._covers_whole_segment(between) is True


@pytest.mark.parametrize("between", [[0.0, 0.5], [0.1, 1.0], [0.5], "bad", [None, 1.0]])
def test_covers_whole_segment_rejects_sub_ranges_and_malformed(between):
    assert routing._covers_whole_segment(between) is False


# --- conditional rules (during / using / ...) --------------------------------


def test_conditional_rule_ignored_when_unconditional_rule_exists():
    # "No entry backward, except deliveries 07:00-10:00" — the unconditional
    # rule is the all-hours truth; the time-windowed exception is ignored.
    rules = [
        _rule("denied", heading="backward"),
        _rule("allowed", heading="backward", mode=["car"], during="Mo-Fr 07:00-10:00"),
    ]
    assert routing._oneway_allowed(rules, "drive") == (True, False)


def test_conditional_denial_ignored_when_unconditional_rule_exists():
    rules = [
        _rule("allowed", mode=["car"]),
        _rule("denied", during="Sa-Su", mode=["car"]),
    ]
    assert routing._oneway_allowed(rules, "drive") == (True, True)


def test_conditional_rule_applied_when_it_is_the_only_rule():
    # Nothing less conditional to fall back on: treat the condition as
    # holding, so the router never suggests an illegal turn.
    rules = [_rule("denied", heading="forward", during="Mo-Fr 07:00-19:00")]
    assert routing._oneway_allowed(rules, "drive") == (False, True)
    rules = [_rule("denied", using=["as_customer"])]
    assert routing._oneway_allowed(rules, "drive") == (False, False)


def test_conditional_rule_for_another_mode_does_not_count_as_fallback():
    # The only unconditional rule is bicycle-scoped, so for drive the
    # conditional denial still stands.
    rules = [
        _rule("allowed", mode=["bicycle"]),
        _rule("denied", mode=["car"], during="Mo-Fr"),
    ]
    assert routing._oneway_allowed(rules, "drive") == (False, False)
    assert routing._oneway_allowed(rules, "cycle") == (True, True)


# --- speed_limits between -----------------------------------------------------


def _limit(value, unit="km/h", between=None):
    return {"max_speed": {"value": value, "unit": unit}, "between": between}


def test_speed_limit_with_full_between_range_counts_as_whole_segment():
    # The docstring always said [0.0, 1.0] is whole-segment; the code used
    # to skip any non-empty range.
    assert routing._speed_limit_m_s([_limit(36, between=[0.0, 1.0])]) == pytest.approx(10.0)


def test_speed_limit_with_sub_range_between_is_skipped():
    assert routing._speed_limit_m_s([_limit(36, between=[0.0, 0.5])]) is None
    assert routing._speed_limit_m_s(
        [_limit(36, between=[0.0, 0.5]), _limit(72, between=None)]
    ) == pytest.approx(20.0)


def test_speed_limit_slowest_whole_segment_entry_wins():
    limits = [_limit(72), _limit(36, between=[0.0, 1.0]), _limit(18, between=[0.5, 1.0])]
    assert routing._speed_limit_m_s(limits) == pytest.approx(10.0)
