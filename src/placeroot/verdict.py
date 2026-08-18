"""Neighborhood verdict: a life-decision report from existing tools (#303).

Composes summarize_area + a multi-slug places scan + isochrone into one
ranked verdict — "should I live here?" as strengths, weak points, and the
one thing to verify in person, not a table of counts. No new remote APIs;
recreation stays the layer already inside find_places.
"""

from __future__ import annotations

import re

from placeroot import categories, overture, routing

# Walk-first default: cheapest scan, and the life-decision mode when the
# asker did not say how they get around.
DEFAULT_MODE = "walk"
DEFAULT_MINUTES = 15

# One scan has to cover every checklist slug, nearest-first. Public
# find_places clamps to MAX_ROWS (25), which a dense grocery cluster would
# fill before a farther playground appears. 80 is 8 needs x 10 nearest.
CHECKLIST_PLACE_LIMIT = 80

# Keep the checklist small — the product is a verdict, not a survey.
MAX_CHECKLIST = 8

HONESTY = (
    "Overture has no rent, crime, school quality, opening hours, or "
    "demographics — this verdict is about what is on the map within reach."
)

# need -> Overture slugs that find_places will actually match. Slugs are
# checked against the bundled taxonomy; do not invent ones that will
# silently return zero rows.
NEED_SLUGS: dict[str, tuple[str, ...]] = {
    "grocery": ("grocery_store",),
    "pharmacy": ("pharmacy",),
    "playground": ("playground",),
    "park": ("park",),
    "transit": ("public_transportation", "bus_station", "train_station"),
    "school": ("school",),
    "restaurant": ("restaurant",),
    "cafe": ("coffee_shop",),
    "gym": ("gym",),
    "library": ("library",),
    "hospital": ("hospital",),
    "daycare": ("day_care_preschool",),
    "dog_park": ("dog_park",),
}

# Daily-needs baseline when the asker said nothing, or as the floor under
# a more specific household.
DEFAULT_NEEDS = ("grocery", "pharmacy", "park", "transit", "restaurant")

# Keyword -> extra need. Defaults stay; these only add.
_NEED_TRIGGERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("toddler", "baby", "infant", "stroller", "playground"), "playground"),
    (("kid", "child", "kids", "children", "school", "elementary", "preschool"), "school"),
    (("dog",), "dog_park"),
    (("gym", "fitness"), "gym"),
    (("library",), "library"),
    (("hospital", "clinic", "doctor", "medical"), "hospital"),
    (("cafe", "coffee"), "cafe"),
    (("daycare", "day care", "childcare", "child care"), "daycare"),
)

_HOUSEHOLD = (
    ("toddler", "toddler"),
    ("baby", "infant"),
    ("infant", "infant"),
    ("stroller", "toddler"),
    ("kid", "child"),
    ("child", "child"),
    ("kids", "child"),
    ("children", "child"),
    ("dog", "dog"),
)

_PRIORITY = (
    ("quiet", "quiet"),
    ("nightlife", "nightlife"),
    ("school", "schools"),
    ("transit", "transit"),
    ("walkable", "walkability"),
    ("park", "parks"),
)

_WORD = r"(?<![a-z]){0}(?![a-z])"


def _has(text: str, *phrases: str) -> bool:
    return any(re.search(_WORD.format(re.escape(p)), text) for p in phrases)


def _mode_speed_m_s(mode: str) -> float:
    speed = routing.MODE_CONFIG[mode]["default_speed_m_s"]
    return speed if speed is not None else routing.ESTIMATED_DRIVE_SPEED_M_S


def parse_context(context: str) -> dict:
    """Free-form life context -> household / mobility / priorities / needs.

    Empty or whitespace still returns the generic daily-needs checklist and
    marks mobility as assumed walk. Slugs are taxonomy-checked so a typo
    here cannot invent a category find_places will not match.
    """
    raw = (context or "").strip()
    text = raw.lower()
    assumed = not text

    household: list[str] = []
    for needle, label in _HOUSEHOLD:
        if _has(text, needle) and label not in household:
            household.append(label)

    if _has(text, "no car", "without a car", "car-free", "carless", "car free"):
        mobility = "walk"
        mobility_source = "context"
    elif _has(text, "bike", "cycle", "cycling", "bicycle", "biking"):
        mobility = "cycle"
        mobility_source = "context"
    elif _has(text, "car", "drive", "driving"):
        mobility = "drive"
        mobility_source = "context"
    elif _has(text, "walk", "walking", "on foot", "walk-first"):
        mobility = "walk"
        mobility_source = "context"
    else:
        mobility = DEFAULT_MODE
        mobility_source = "assumed"

    priorities: list[str] = []
    for needle, label in _PRIORITY:
        if _has(text, needle) and label not in priorities:
            priorities.append(label)

    needs = list(DEFAULT_NEEDS)
    for needles, need in _NEED_TRIGGERS:
        if _has(text, *needles) and need not in needs:
            needs.append(need)
    needs = [n for n in needs if n in NEED_SLUGS][:MAX_CHECKLIST]

    return {
        "household": household,
        "mobility": mobility,
        "mobility_source": mobility_source,
        "priorities": priorities,
        "needs": needs,
        "assumed": assumed,
        "raw": raw,
    }


def derive_budget(
    parsed: dict,
    radius_m: float | None,
    minutes: float | None,
    mode: str | None,
) -> tuple[str, float, float]:
    """Resolve mode / minutes / radius, overrides winning over context."""
    resolved_mode = (mode or parsed["mobility"] or DEFAULT_MODE).strip().lower()
    if resolved_mode not in routing.MODE_CONFIG:
        raise routing.UnsupportedMode(resolved_mode)
    resolved_minutes = DEFAULT_MINUTES if minutes is None else minutes
    if not isinstance(resolved_minutes, (int, float)) or isinstance(resolved_minutes, bool):
        raise ValueError("minutes must be a number")
    if resolved_minutes <= 0:
        raise ValueError("minutes must be greater than 0")
    if radius_m is None:
        speed = _mode_speed_m_s(resolved_mode)
        cap = routing.MODE_CONFIG[resolved_mode]["max_radius_m"]
        radius_m = min(resolved_minutes * 60.0 * speed * routing.RADIUS_BUFFER, cap)
    if not isinstance(radius_m, (int, float)) or isinstance(radius_m, bool):
        raise ValueError("radius_m must be a number")
    if radius_m < 0:
        raise ValueError("radius_m must be non-negative")
    cap = routing.MODE_CONFIG[resolved_mode]["max_radius_m"]
    if radius_m > cap:
        raise routing.RadiusTooLarge(radius_m, cap)
    return resolved_mode, float(resolved_minutes), float(radius_m)


def _place_matches(place: dict, slugs: tuple[str, ...]) -> bool:
    fields = [
        (place.get("category") or "").lower(),
        (place.get("basic_category") or "").lower(),
    ]
    slugs_l = [s.lower() for s in slugs]
    if any(f in slugs_l for f in fields if f):
        return True
    return any(s in f for s in slugs_l for f in fields if f)


def _nearest_for_need(places: list[dict], slugs: tuple[str, ...]) -> dict | None:
    for place in places:
        if _place_matches(place, slugs):
            return place
    return None


def _travel_min(distance_m: float, mode: str) -> float:
    return distance_m / _mode_speed_m_s(mode) / 60.0


def _mode_adverb(mode: str) -> str:
    return {"walk": "walk", "cycle": "bike", "drive": "drive"}[mode]


def _detail(minutes: float, mode: str, name: str | None = None) -> str:
    whole = max(1, int(round(minutes)))
    label = f"{whole} min {_mode_adverb(mode)}"
    if name:
        return f"{label} ({name})"
    return label


def _score_need(need: str, nearest: dict | None, mode: str, minutes: float) -> dict:
    if nearest is None:
        return {
            "need": need,
            "status": "unknown",
            "nearest": None,
            "walk_min": None,
            "detail": "none mapped in range",
        }
    travel = _travel_min(float(nearest["distance_m"]), mode)
    status = "covered" if travel <= minutes else "weak"
    compact = {
        "id": nearest.get("id"),
        "name": nearest.get("name"),
        "category": nearest.get("category") or nearest.get("basic_category"),
        "distance_m": nearest.get("distance_m"),
    }
    return {
        "need": need,
        "status": status,
        "nearest": compact,
        "walk_min": round(travel, 1),
        "detail": _detail(travel, mode, nearest.get("name")),
    }


def _verify_in_person(parsed: dict, checklist: list[dict], mode: str) -> str:
    household = set(parsed["household"])
    by_need = {row["need"]: row for row in checklist}
    weak_or_unknown = [row for row in checklist if row["status"] != "covered"]

    if "toddler" in household or "infant" in household:
        play = by_need.get("playground")
        if play is not None and play["status"] != "covered":
            return "the playground at weekend morning — mapped coverage is the weak point"
    if mode == "walk":
        transit = by_need.get("transit")
        if transit is not None and transit["status"] != "covered":
            return "the walk to the nearest transit stop at rush hour"
    if "child" in household:
        school = by_need.get("school")
        if school is not None and school["status"] != "covered":
            return "the school-run at pickup — nearest mapped school is the weak point"
    if weak_or_unknown:
        # Most decision-relevant leftover: farthest weak, else first unknown.
        weak = [r for r in weak_or_unknown if r["status"] == "weak" and r["walk_min"] is not None]
        if weak:
            farthest = max(weak, key=lambda r: r["walk_min"])
            return f"the {farthest['need']} trip in person ({farthest['detail']})"
        return f"whether a {weak_or_unknown[0]['need']} is actually nearby — none mapped in range"
    return "walk the block at school pickup / evening"


def _verdict_sentence(
    parsed: dict,
    checklist: list[dict],
    mode: str,
    assumed: bool,
    total_places: int | None,
) -> str:
    covered = [r for r in checklist if r["status"] == "covered"]
    weak = [r for r in checklist if r["status"] == "weak"]
    unknown = [r for r in checklist if r["status"] == "unknown"]
    assume = (
        "Assuming a walk-first daily-needs check (no household or mobility given). "
        if assumed
        else ""
    )
    empty = (
        "Mapped places look sparse here. "
        if total_places is not None and total_places == 0
        else ""
    )

    def _bits(rows: list[dict], n: int = 3) -> str:
        return ", ".join(f"{r['need']} {r['detail']}" for r in rows[:n])

    if covered and not weak and not unknown:
        return (
            f"{assume}{empty}Daily needs are covered — {_bits(covered)} — "
            "nothing on the map is the weak point."
        )
    if covered and (weak or unknown):
        gap = weak[0] if weak else unknown[0]
        return (
            f"{assume}{empty}Daily needs are covered — {_bits(covered)} — "
            f"but {gap['need']} is the weak point ({gap['detail']})."
        )
    if weak or unknown:
        gaps = _bits(weak + unknown)
        return f"{assume}{empty}A thin daily-needs picture on foot of the data: {gaps}."
    return f"{assume}{empty}No daily-needs places were scored."


def neighborhood_verdict(
    lat: float,
    lon: float,
    context: str = "",
    radius_m: float | None = None,
    minutes: float | None = None,
    mode: str | None = None,
) -> dict:
    """Compose a ranked neighborhood verdict from existing internals.

    One summarize_area, one multi-slug places scan, one isochrone — same
    radius so the places tile cache hits. Isochrone failure degrades to
    straight-line times rather than failing the verdict; places/summarize
    failures propagate.
    """
    parsed = parse_context(context)
    resolved_mode, resolved_minutes, resolved_radius = derive_budget(
        parsed, radius_m, minutes, mode
    )
    slugs: list[str] = []
    for need in parsed["needs"]:
        slugs.extend(NEED_SLUGS[need])
    # Taxonomy guard: drop anything find_places cannot match.
    slugs = [s for s in slugs if categories.hierarchy_for(s) is not None]
    if not slugs:
        slugs = [s for n in DEFAULT_NEEDS for s in NEED_SLUGS[n]]

    summary = overture.summarize_area(lat, lon, resolved_radius)
    places = overture.find_places_for_categories(
        lat, lon, resolved_radius, slugs, limit=CHECKLIST_PLACE_LIMIT
    )

    iso_note = None
    try:
        iso = routing.isochrone(
            lat, lon, minutes=resolved_minutes, mode=resolved_mode, radius_m=resolved_radius
        )
        iso_max = (iso.get("stats") or {}).get("max_radius_m")
    except routing.NoGraphNearby as e:
        iso = None
        iso_max = None
        iso_note = e.detail

    checklist = [
        _score_need(
            need,
            _nearest_for_need(places, NEED_SLUGS[need]),
            resolved_mode,
            resolved_minutes,
        )
        for need in parsed["needs"]
    ]
    # A place beyond the isochrone's reached radius is not actually in
    # budget even if the straight-line time says it is.
    if iso_max is not None:
        for row in checklist:
            nearest = row.get("nearest") or {}
            dist = nearest.get("distance_m")
            if dist is not None and dist > iso_max and row["status"] == "covered":
                row["status"] = "weak"
                row["detail"] = f"{row['detail']}; beyond the {int(round(iso_max))} m reach"

    strengths = [
        {"need": r["need"], "detail": r["detail"], "walk_min": r["walk_min"]}
        for r in checklist
        if r["status"] == "covered"
    ]
    weak_points = [
        {"need": r["need"], "detail": r["detail"], "walk_min": r["walk_min"]}
        for r in checklist
        if r["status"] != "covered"
    ]

    context_read = {
        "household": parsed["household"],
        "mobility": resolved_mode,
        "priorities": parsed["priorities"],
    }
    result = {
        "verdict": _verdict_sentence(
            parsed, checklist, resolved_mode, parsed["assumed"], summary.get("total_places")
        ),
        "mode": resolved_mode,
        "minutes": resolved_minutes,
        "radius_m": round(resolved_radius, 1),
        "context_read": context_read,
        "strengths": strengths,
        "weak_points": weak_points,
        "verify_in_person": _verify_in_person(parsed, checklist, resolved_mode),
        "checklist": checklist,
        "honesty": HONESTY,
    }
    if iso_note:
        result["note"] = (
            f"travel times are straight-line; no street graph nearby ({iso_note})"
        )
    elif iso is not None and summary.get("total_places") is not None:
        # Tiny character hint, not a dump of top_categories.
        result["area_places"] = summary["total_places"]
    return result
