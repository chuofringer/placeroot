"""Listing claim checker: verify_claims (#316).

Apartment/office listings make spatial claims — "8 minutes to the metro",
"shops on the doorstep", "surrounded by green space" — that a prospective
tenant cannot check without doing the routing and counting themselves.
Free-text claim parsing needs an LLM, so this module takes
already-decomposed structured checks (the verify_listing_claims MCP prompt
teaches an agent how to turn listing text into these) and grades each one
against real routing and places data.

Three claim kinds, each built from internals this codebase already has:
- travel_time: nearest matching place (overture.find_places) + a routed
  leg (routing.route()) -> measured minutes vs claimed_minutes.
- count_nearby: how many matching places fall within radius_m
  (overture.find_places, capped at MAX_COUNT_RADIUS_M) -> measured count
  vs claimed_at_least.
- distance: nearest matching place -> straight-line meters (the same
  haversine find_places already computes, not a routed distance) vs
  claimed_max_m.

Verdict thresholds (see VERDICT_RULE): measured within claimed x
CONFIRMED_MULT (or count >= claimed) is "confirmed"; within claimed x
STRETCHED_MULT (or count >= half claimed, floor 1) is "stretched";
otherwise "false". A claim asserting a place exists at all, when the
search finds none within its bound, is "false" with a note — absence is
itself a verdict, not an error. A travel_time claim whose place is found
but cannot be routed to (no street graph nearby, or the network simply
doesn't connect the two points) is "unverifiable": an unroutable leg is
not evidence the claim is wrong.
"""

from __future__ import annotations

import math

from placeroot import overture, routing

CONFIRMED_MULT = 1.15
STRETCHED_MULT = 1.5

MAX_CLAIMS = 8
MAX_TRAVEL_TIME_CLAIMS = 5

DEFAULT_COUNT_RADIUS_M = 500.0
MAX_COUNT_RADIUS_M = 2000.0

# travel_time/distance search for the nearest match within a radius padded
# well past the claim itself, so a claim that's simply wrong still finds
# the real place rather than reporting "not found" for a place that exists
# just outside a tight window. travel_time's bound also never exceeds the
# mode's own graph-extraction cap (routing.MODE_CONFIG[mode]["max_radius_m"])
# so a found place is always inside the radius route() itself can reach —
# RouteTooLong should not fire in ordinary use.
_SEARCH_BUFFER = 3.0
_MIN_SEARCH_RADIUS_M = 300.0
_DISTANCE_SEARCH_MULT = 2.0  # same "search out to 2x the cap" convention as within_distance

VALID_KINDS = frozenset({"travel_time", "count_nearby", "distance"})
VALID_MODES = ("walk", "cycle", "drive")

VERDICT_RULE = (
    "confirmed: measured <= claimed x 1.15 (count_nearby: count >= claimed); "
    "stretched: measured <= claimed x 1.5 (count_nearby: count >= claimed/2, floor 1); "
    "else false. A travel_time claim that cannot be routed at all (no street "
    "graph nearby, or the network doesn't connect) is unverifiable instead."
)


class ClaimError(ValueError):
    """A claims[] entry failed validation. str(e) is the bad_request detail."""


def _mode_speed_m_s(mode: str) -> float:
    speed = routing.MODE_CONFIG[mode]["default_speed_m_s"]
    return speed if speed is not None else routing.ESTIMATED_DRIVE_SPEED_M_S


def _nearest_match(lat: float, lon: float, radius_m: float, category, name) -> dict | None:
    rows = overture.find_places(lat, lon, radius_m, category=category, name=name, limit=1)
    return rows[0] if rows else None


def _compact_place(place: dict) -> dict:
    return {
        "id": place.get("id"),
        "name": place.get("name"),
        "category": place.get("category") or place.get("basic_category"),
    }


def _thing_label(category, name) -> str:
    bits = [b for b in (category, name) if b]
    return " / ".join(bits) if bits else "matching place"


def _not_found_note(category, name, radius_m: float) -> str:
    return f"no {_thing_label(category, name)} found within {int(round(radius_m))}m search bound"


def _grade(measured: float, claimed: float) -> str:
    if claimed <= 0:
        return "confirmed" if measured <= 0 else "false"
    if measured <= claimed * CONFIRMED_MULT:
        return "confirmed"
    if measured <= claimed * STRETCHED_MULT:
        return "stretched"
    return "false"


def _grade_count(count: int, claimed_at_least: int) -> str:
    if count >= claimed_at_least:
        return "confirmed"
    if count >= max(1, claimed_at_least / 2.0):
        return "stretched"
    return "false"


def _require_number(value, field: str, index: int) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ClaimError(f"claims[{index}]: {field} is required and must be a number")
    if not math.isfinite(value):
        raise ClaimError(f"claims[{index}]: {field} must be a finite number")
    return float(value)


def _require_target(claim: dict, index: int, cat_field: str, name_field: str) -> tuple:
    category = claim.get(cat_field)
    name = claim.get(name_field)
    if not category and not name:
        raise ClaimError(f"claims[{index}]: needs {cat_field} or {name_field}")
    return category, name


def _check_travel_time(lat: float, lon: float, claim: dict, index: int) -> dict:
    to_category, to_name = _require_target(claim, index, "to_category", "to_name")
    mode = claim.get("mode") or "walk"
    if mode not in VALID_MODES:
        raise ClaimError(f"claims[{index}]: mode must be one of {VALID_MODES}, got {mode!r}")
    claimed_minutes = _require_number(claim.get("claimed_minutes"), "claimed_minutes", index)
    if claimed_minutes < 0:
        raise ClaimError(f"claims[{index}]: claimed_minutes must be non-negative")

    speed = _mode_speed_m_s(mode)
    search_radius_m = min(
        max(claimed_minutes * 60.0 * speed * _SEARCH_BUFFER, _MIN_SEARCH_RADIUS_M),
        routing.MODE_CONFIG[mode]["max_radius_m"],
    )
    place = _nearest_match(lat, lon, search_radius_m, to_category, to_name)
    if place is None:
        return {
            "claim": claim,
            "verdict": "false",
            "measured": {"minutes": None},
            "note": _not_found_note(to_category, to_name, search_radius_m),
        }
    try:
        routed = routing.route(lat, lon, place["lat"], place["lon"], mode=mode)
    except (routing.NoGraphNearby, routing.RouteTooLong) as e:
        return {
            "claim": claim,
            "verdict": "unverifiable",
            "measured": {"minutes": None, "match": _compact_place(place)},
            "note": f"could not route to {place.get('name') or 'the match'}: {e.detail}",
        }
    if "error" in routed:
        # Both ends snapped to the graph but nothing connects them
        # (routing.route()'s structured no_route result) — still not
        # evidence the claim is false, just that it can't be checked.
        return {
            "claim": claim,
            "verdict": "unverifiable",
            "measured": {"minutes": None, "match": _compact_place(place)},
            "note": f"could not route to {place.get('name') or 'the match'}: {routed['detail']}",
        }
    measured_minutes = routed["duration_s"] / 60.0
    return {
        "claim": claim,
        "verdict": _grade(measured_minutes, claimed_minutes),
        "measured": {
            "minutes": round(measured_minutes, 1),
            "match": _compact_place(place),
        },
    }


def _check_count_nearby(lat: float, lon: float, claim: dict, index: int) -> dict:
    category, name = _require_target(claim, index, "category", "name")
    claimed_at_least = claim.get("claimed_at_least")
    if not isinstance(claimed_at_least, int) or isinstance(claimed_at_least, bool):
        raise ClaimError(f"claims[{index}]: claimed_at_least is required and must be an integer")
    radius_m = claim.get("radius_m", DEFAULT_COUNT_RADIUS_M)
    radius_m = _require_number(radius_m, "radius_m", index)
    if radius_m < 0:
        raise ClaimError(f"claims[{index}]: radius_m must be non-negative")
    radius_m = min(radius_m, MAX_COUNT_RADIUS_M)

    rows = overture.find_places(
        lat, lon, radius_m, category=category, name=name, limit=overture.MAX_ROWS
    )
    count = len(rows)
    result = {
        "claim": claim,
        "verdict": _grade_count(count, claimed_at_least),
        "measured": {"count": count, "radius_m": radius_m},
    }
    if count >= overture.MAX_ROWS:
        result["note"] = (
            f"count capped at {overture.MAX_ROWS} matches within {int(radius_m)}m; "
            "the true count may be higher"
        )
    return result


def _check_distance(lat: float, lon: float, claim: dict, index: int) -> dict:
    to_category, to_name = _require_target(claim, index, "to_category", "to_name")
    claimed_max_m = _require_number(claim.get("claimed_max_m"), "claimed_max_m", index)
    if claimed_max_m < 0:
        raise ClaimError(f"claims[{index}]: claimed_max_m must be non-negative")

    search_radius_m = max(claimed_max_m * _DISTANCE_SEARCH_MULT, _MIN_SEARCH_RADIUS_M)
    place = _nearest_match(lat, lon, search_radius_m, to_category, to_name)
    if place is None:
        return {
            "claim": claim,
            "verdict": "false",
            "measured": {"distance_m": None},
            "note": _not_found_note(to_category, to_name, search_radius_m),
        }
    distance_m = place["distance_m"]
    return {
        "claim": claim,
        "verdict": _grade(distance_m, claimed_max_m),
        "measured": {"distance_m": distance_m, "match": _compact_place(place)},
    }


_CHECKS = {
    "travel_time": _check_travel_time,
    "count_nearby": _check_count_nearby,
    "distance": _check_distance,
}


def verify_claims(lat: float, lon: float, claims: list) -> dict:
    """Grade a batch of structured listing claims against real map data.

    Raises ClaimError (a ValueError) for anything malformed — an unknown
    or missing kind, too many claims, too many travel_time claims, a
    missing/non-numeric claimed value, or neither target field given.
    Raises routing.UnsupportedMode/UpstreamUnavailable/SchemaDegraded or
    overture.UpstreamUnavailable/SchemaDegraded for a genuine data/upstream
    failure — the caller turns those into one structured error for the
    whole call, same as neighborhood_verdict.

    Returns {"results": [...], "verdict_rule": VERDICT_RULE}; see the
    module docstring for the per-kind measured shape and thresholds.
    """
    if not isinstance(claims, list) or not claims:
        raise ClaimError("claims must be a non-empty list")
    if len(claims) > MAX_CLAIMS:
        raise ClaimError(f"claims: at most {MAX_CLAIMS} claims per call, got {len(claims)}")
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise ClaimError(f"claims[{index}] must be an object")
        if claim.get("kind") not in VALID_KINDS:
            raise ClaimError(
                f"claims[{index}]: kind must be one of {sorted(VALID_KINDS)}, "
                f"got {claim.get('kind')!r}"
            )
    travel_time_count = sum(1 for c in claims if c["kind"] == "travel_time")
    if travel_time_count > MAX_TRAVEL_TIME_CLAIMS:
        raise ClaimError(
            f"claims: at most {MAX_TRAVEL_TIME_CLAIMS} travel_time claims per call "
            f"(each costs a routed call), got {travel_time_count}"
        )

    results = [_CHECKS[claim["kind"]](lat, lon, claim, index) for index, claim in enumerate(claims)]
    return {"results": results, "verdict_rule": VERDICT_RULE}
