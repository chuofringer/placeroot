"""ground_location: single-hop location grounding for agents (#362).

One compact payload combining reverse_geocode's answer for the point,
summarize_area's category mix at a fixed radius, isochrone stats (no
polygon — this tool never returns geometry), and the nearest few named
places. Each of the four sections is independent: an underlying call
raising, or coming back empty, degrades that section to a short note in
"notes" rather than failing the whole call. The call only fails outright
(a structured error) if every section failed.
"""

from __future__ import annotations

import math

from placeroot import geocode as geocoding
from placeroot import overture, routing

# Fixed radius for the surroundings summary — a sensible walk-around-the-
# block scale, not something the caller tunes per call (that's what
# summarize_area itself is for).
SURROUNDINGS_RADIUS_M = 500.0

# find_places' own default radius/limit are close to what "nearest 2-3
# named places" wants, so notable search stays independent of the
# surroundings radius above.
NOTABLE_RADIUS_M = 1000.0
NOTABLE_LIMIT = 3

TOP_CATEGORIES_LIMIT = 5

# Every exception a section's underlying call can raise that means "this
# section couldn't answer" rather than "the input was malformed" (bad
# coordinates/mode/minutes are validated by the caller before this runs).
_DEGRADE_EXC = (
    overture.UpstreamUnavailable,
    overture.SchemaDegraded,
    routing.UpstreamUnavailable,
    routing.SchemaDegraded,
    routing.NoGraphNearby,
    routing.RadiusTooLarge,
    routing.UnsupportedMode,
)


def _detail(e: Exception) -> str:
    return getattr(e, "detail", str(e))


def _where(lat: float, lon: float, notes: list[str]) -> dict | None:
    try:
        return geocoding.reverse_geocode(lat, lon)
    except _DEGRADE_EXC as e:
        notes.append(f"where: {_detail(e)}")
        return None


def _surroundings(lat: float, lon: float, notes: list[str]) -> dict | None:
    try:
        summary = overture.summarize_area(lat, lon, SURROUNDINGS_RADIUS_M)
    except _DEGRADE_EXC as e:
        notes.append(f"surroundings: {_detail(e)}")
        return None
    total = summary.get("total_places") or 0
    area_km2 = math.pi * (SURROUNDINGS_RADIUS_M / 1000.0) ** 2
    top_all = summary.get("top_categories") or []
    # Truncating to TOP_CATEGORIES_LIMIT must keep the counts reconcilable:
    # anything cut here is folded into other_categories_count so
    # total_places == sum(top) + other + uncategorized still holds.
    other_count = (summary.get("other_categories_count") or 0) + sum(
        row.get("count") or 0 for row in top_all[TOP_CATEGORIES_LIMIT:]
    )
    return {
        "radius_m": SURROUNDINGS_RADIUS_M,
        "total_places": total,
        "top_categories": top_all[:TOP_CATEGORIES_LIMIT],
        "other_categories_count": other_count,
        "uncategorized_count": summary.get("uncategorized_count") or 0,
        "density_per_km2": round(total / area_km2, 1) if area_km2 else None,
    }


def _reach(lat: float, lon: float, minutes: float, mode: str, notes: list[str]) -> dict | None:
    try:
        iso = routing.isochrone(lat, lon, minutes=minutes, mode=mode)
    except _DEGRADE_EXC as e:
        notes.append(f"reach: {_detail(e)}")
        return None
    reach = {"minutes": iso["minutes"], "mode": iso["mode"], "stats": iso["stats"]}
    if iso.get("speed_m_s") is not None:
        reach["speed_m_s"] = iso["speed_m_s"]
    if iso.get("truncated"):
        reach["truncated"] = True
    return reach


_NOTABLE_KEYS = ("id", "name", "category", "distance_m", "trust_note")


def _notable(lat: float, lon: float, notes: list[str]) -> list[dict] | None:
    try:
        rows = overture.find_places(lat, lon, radius_m=NOTABLE_RADIUS_M, limit=NOTABLE_LIMIT)
    except _DEGRADE_EXC as e:
        notes.append(f"notable: {_detail(e)}")
        return None
    if not rows:
        notes.append(f"notable: no named places found within {round(NOTABLE_RADIUS_M)} m")
        return None
    return [{k: row[k] for k in _NOTABLE_KEYS if row.get(k) is not None} for row in rows]


def ground_location(lat: float, lon: float, minutes: float, mode: str) -> dict:
    """Compose where/surroundings/reach/notable from existing internals.

    Returns a structured {"error": "upstream_unavailable", ...} only if
    every section failed; otherwise returns whichever sections succeeded
    plus a "notes" list explaining any that didn't.
    """
    notes: list[str] = []
    sections = {
        "where": _where(lat, lon, notes),
        "surroundings": _surroundings(lat, lon, notes),
        "reach": _reach(lat, lon, minutes, mode, notes),
        "notable": _notable(lat, lon, notes),
    }
    if all(value is None for value in sections.values()):
        return {
            "error": "upstream_unavailable",
            "detail": "every section failed: " + "; ".join(notes),
            "retry_advised": True,
        }
    result: dict = {"center": {"lat": lat, "lon": lon}, "minutes": minutes, "mode": mode}
    for key, value in sections.items():
        if value is not None:
            result[key] = value
    if notes:
        result["notes"] = notes
    return result
