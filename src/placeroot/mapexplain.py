"""Explained-map payloads: composed tools' argument, ready for render_map (#369).

#367/#368 gave render_map its explanatory vocabulary — point `class` +
`legend`, shape `role`/`label`/`callout`. This module is the other half:
composed tools (compare_areas' verdict mode, meeting_point) build that
vocabulary FROM their own reasoning and attach it under a `map` key so an
agent can render the argument, not reassemble it, in one call:

    render_map(**result["map"])

`map` mirrors render_map's own keyword arguments exactly ("result",
"legend", "summary") so the splat above is literally correct — no
translation layer, no second schema to learn.

Precedent read before choosing the key name: route/optimize_route attach
their pocket-handoff payload under "export" (see export.py), built by a
`from_<tool>_result` helper that takes the tool's own result mapping (plus
whatever the result doesn't carry, e.g. optimize_route's caller-supplied
stop list) and returns a plain dict — no tool-specific type. `map` follows
the same shape of precedent (a `from_<tool>_result` builder, attached only
on success, never replacing anything) but a new key: `export` is a pocket
handoff (deep links/GPX/text) callers open outside the chat, while `map` is
an argument to another tool in *this* server — reusing "export" would
suggest the two are interchangeable when they aren't.

Kept cheap on purpose (#369's budget guard): every payload here is
markers plus small parametric shapes (a circle from a center + radius the
caller already gave, points already in the result) — never a duplicated
heavy geometry blob. A shape/point close to render_map's char caps
(mapview.SHAPE_LABEL_MAX_CHARS / SHAPE_CALLOUT_MAX_CHARS) is truncated
downstream by render_map itself; callers here just keep callouts short.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from placeroot import geo
from placeroot.geometry_ops import destination

# Cheap parametric circle standing in for an area's search radius — not a
# real boundary (compare_areas never fetches one), just a legible "here's
# roughly the disc that was measured" outline. 16 points is plenty smooth
# at map scale and keeps the shape far under any vertex budget.
_CIRCLE_POINTS = 16


def _circle_ring(lat: float, lon: float, radius_m: float) -> list[list[float]]:
    """A closed GeoJSON ring (lon, lat pairs) approximating a radius_m circle around (lat, lon)."""
    if radius_m <= 0:
        radius_m = 1.0
    ring = []
    for i in range(_CIRCLE_POINTS):
        bearing_deg = (360.0 / _CIRCLE_POINTS) * i
        pt = destination({"lat": lat, "lon": lon}, bearing_deg, radius_m)["point"]
        ring.append([pt["lon"], pt["lat"]])
    ring.append(ring[0])
    return ring


def _point_feature(lat: float, lon: float, name: str, cls: str) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"name": name, "class": cls},
    }


def _circle_feature(lat: float, lon: float, radius_m: float, *, label: str, callout: str) -> dict:
    ring = _circle_ring(lat, lon, radius_m)
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"role": "outline", "label": label, "callout": callout},
    }


def _feature_collection(features: Sequence[dict]) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


# --- meeting_point (#306) ---------------------------------------------------

_MEETING_LEGEND = {
    "origin": {"label": "Participant"},
    "fair_center": {"label": "Fair meeting point (seed)"},
    "best_candidate": {"label": "Fairest venue", "color": "#2e8b57"},
    "candidate": {"label": "Other candidate"},
}


def from_meeting_point_result(
    result: Mapping[str, object], origins: Sequence[Mapping[str, object]]
) -> dict[str, object] | None:
    """Build the `map` payload for a successful meeting_point call.

    Only built when there's something to show: at least one candidate.
    Pins: one per origin (class "origin"), the fairest candidate
    (class "best_candidate"), the rest (class "candidate"), and the
    fairness seed center (class "fair_center") — no shapes, nothing here
    is cheap to draw as a boundary.
    """
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    center = result.get("center")
    if not isinstance(center, Mapping):
        return None

    features: list[dict] = []
    for idx, origin in enumerate(origins):
        try:
            olat, olon = float(origin["lat"]), float(origin["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        features.append(_point_feature(olat, olon, f"Participant {idx + 1}", "origin"))

    for idx, c in enumerate(candidates):
        if not isinstance(c, Mapping):
            continue
        name = c.get("name") or f"Candidate {idx + 1}"
        cls = "best_candidate" if idx == 0 else "candidate"
        features.append(_point_feature(float(c["lat"]), float(c["lon"]), str(name), cls))

    features.append(
        _point_feature(
            float(center["lat"]), float(center["lon"]), "Fair meeting point (seed)", "fair_center"
        )
    )

    best = candidates[0]
    n_people = len(origins)
    summary = (
        f"{best.get('name') or 'The top candidate'} is the fairest place to meet — "
        f"{best.get('max_travel_time_min')} min max travel time, "
        f"{best.get('spread_min')} min spread across {n_people} participants."
    )

    return {
        "result": _feature_collection(features),
        "legend": dict(_MEETING_LEGEND),
        "summary": summary,
    }


# --- compare_areas verdict/priorities mode (#304/#354) ----------------------

_AREA_LEGEND = {
    "winner": {"label": "Winner", "color": "#2e8b57"},
    "area": {"label": "Compared area"},
}


def from_compare_areas_result(
    result: Mapping[str, object], radius_m: float
) -> dict[str, object] | None:
    """Build the `map` payload for a compare_areas call scored with `priorities`.

    Only built when a verdict was actually computed (priorities given).
    Pins: one per area center, the overall winner picked out by class.
    Shapes: one circle per area — a cheap parametric stand-in for "roughly
    this disc was measured", not a real boundary — labeled with the area
    number and a callout restating that area's score/role in the verdict.
    """
    verdict = result.get("verdict")
    areas = result.get("areas")
    if not isinstance(verdict, Mapping) or not isinstance(areas, list) or not areas:
        return None

    winner_idx = verdict.get("winner_idx")
    scores = verdict.get("scores")
    degraded = bool(verdict.get("degraded"))
    circle_radius = geo.clamp_radius_m(radius_m) or 1.0

    features: list[dict] = []
    for idx, area in enumerate(areas):
        if not isinstance(area, Mapping):
            continue
        center = area.get("center")
        if not isinstance(center, Mapping):
            continue
        try:
            alat, alon = float(center["lat"]), float(center["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        label = f"Area {idx + 1}"
        is_winner = winner_idx is not None and idx == winner_idx
        cls = "winner" if is_winner else "area"
        features.append(_point_feature(alat, alon, label, cls))

        if degraded:
            callout = "Score not measured (degraded data)"
        elif isinstance(scores, list) and idx < len(scores):
            role = "winner" if is_winner else "trails"
            callout = f"score {scores[idx]:g} · {role}"
        else:
            callout = "score unavailable"
        features.append(
            _circle_feature(alat, alon, circle_radius, label=label, callout=callout)
        )

    if not features:
        return None

    if degraded:
        summary = str(
            verdict.get("measured_note")
            or "Verdict not scored — the dataset's category columns are degraded."
        )
    elif winner_idx is not None and isinstance(scores, list):
        margin = verdict.get("margin")
        margin_bit = f" (margin {margin:g})" if isinstance(margin, (int, float)) else ""
        summary = f"Area {winner_idx + 1} wins with score {scores[winner_idx]:g}{margin_bit}."
    else:
        summary = "No single winner — the areas tied on the given priorities."

    return {
        "result": _feature_collection(features),
        "legend": dict(_AREA_LEGEND),
        "summary": summary,
    }
