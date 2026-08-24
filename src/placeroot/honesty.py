"""Calibrated before-you-go trust notes from signals we already have (#308).

Open data's known gap is closures and hours. Users forgive missing data;
they do not forgive being sent to a shuttered café. This module turns the
per-place `confidence` and `operating_status` already on every place row
(and `sources`, when a details payload carried them) into one short clause.

No I/O. Other tools call `attach_trust_note` / `attach_trust_notes` on
actionable place rows, and `verify_before_going` on a composed itinerary
to name the 1–2 stops most worth checking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# Overture confidence is roughly 0–1. Bands are inclusive at the top of
# each range so a rounded 0.75 still reads as high, and a missing score
# falls through to the low/unknown call-ahead wording.
HIGH_CONFIDENCE = 0.75
LOW_CONFIDENCE = 0.50

_PERMANENTLY_CLOSED = frozenset({"permanently closed", "closed", "closed_permanently"})
_TEMPORARILY_CLOSED = frozenset({"temporarily closed", "closed_temporarily"})
# Present on a place lookup; absent on optimize_route's normal {lat, lon} stops.
_PLACE_FIELDS = ("confidence", "operating_status", "sources")


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status(place: Mapping) -> str:
    raw = place.get("operating_status")
    if raw is None:
        return ""
    return str(raw).strip().lower()


def _named_source(place: Mapping) -> bool | None:
    """Whether `sources` names a dataset.

    None means the row has no `sources` key (find_places compact shape) —
    we cannot tell listing attribution apart from its absence. True/False
    means the details payload included the field and it did / didn't name
    a dataset.
    """
    if "sources" not in place:
        return None
    sources = place.get("sources") or []
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        return False
    for src in sources:
        if isinstance(src, Mapping):
            dataset = str(src.get("dataset") or "").strip()
            if dataset:
                return True
        elif isinstance(src, str) and src.strip():
            return True
    return False


def _confidence_band(confidence: float | None) -> str:
    if confidence is None:
        return "low"
    if confidence >= HIGH_CONFIDENCE:
        return "high"
    if confidence >= LOW_CONFIDENCE:
        return "moderate"
    return "low"


def _flag_reason(place: Mapping) -> str | None:
    """Short reason a stop is worth double-checking, or None if it isn't."""
    status = _status(place)
    if status in _PERMANENTLY_CLOSED:
        return "listed as permanently closed"
    if status in _TEMPORARILY_CLOSED:
        return "listed as temporarily closed"
    band = _confidence_band(_as_float(place.get("confidence")))
    if band == "high":
        # Only an explicit empty `sources` list is "unnamed source".
        # Missing `sources` (compact find_places) is not a claim.
        if _named_source(place) is False:
            return "unnamed source"
        return None
    if band == "moderate":
        return "moderate confidence"
    return "low confidence"


def trust_note(place: Mapping) -> str:
    """One short before-you-go clause for a place row.

    Closed statuses win: a high-confidence permanently-closed listing is
    still a shuttered café. Otherwise the clause is a confidence band,
    plus a source hint only when the row actually carries `sources`.

    Compact find_places rows omit that field — they get the band (and
    call-ahead on non-high), never "recently confirmed in listings" or
    "unnamed source". Those phrases require an explicit sources list:
    named dataset vs present-but-empty.
    """
    status = _status(place)
    if status in _PERMANENTLY_CLOSED:
        return "Listed as permanently closed — verify before going."
    if status in _TEMPORARILY_CLOSED:
        return "Listed as temporarily closed — call ahead."

    band = _confidence_band(_as_float(place.get("confidence")))
    named = _named_source(place)

    if band == "high":
        if named is False:
            return "High confidence, unnamed source — call ahead."
        if named is True:
            return "High confidence, recently confirmed in listings"
        return "High confidence"
    if band == "moderate":
        if named is False:
            return "Moderate confidence, unnamed source — call ahead."
        return "Moderate confidence — call ahead."
    if named is False:
        return "Low confidence, unnamed source — call ahead."
    return "Low confidence — call ahead."


TRUST_TIERS = ("strong", "ok", "weak", "unknown")

# One fixed legend line for find_places' detail="compact" rows (roadmap
# §4.5): compact rows carry a `trust` tier instead of `trust_note`'s prose,
# so this one payload-level line is what makes the tier self-explaining
# without repeating a sentence on every row.
TRUST_LEGEND = (
    "trust tiers: strong=high confidence; ok=moderate confidence, or high "
    "with no named source; weak=low confidence or closed; unknown=no "
    "confidence score"
)


def trust_tier(place: Mapping) -> str:
    """One of TRUST_TIERS, derived from the exact same signals as
    trust_note (#308) — status, confidence band, named source — so the
    tier and the prose can never disagree about the same row.

    Closed statuses always win, same as trust_note. Otherwise: no
    confidence score at all is "unknown" (distinct from an explicit low
    score, which is "weak"); high confidence is "strong" unless the row
    explicitly carries an empty sources list ("ok", mirroring trust_note's
    "unnamed source" call-ahead); moderate confidence is always "ok".
    """
    status = _status(place)
    if status in _PERMANENTLY_CLOSED or status in _TEMPORARILY_CLOSED:
        return "weak"
    confidence = _as_float(place.get("confidence"))
    if confidence is None:
        return "unknown"
    band = _confidence_band(confidence)
    if band == "high":
        return "ok" if _named_source(place) is False else "strong"
    if band == "moderate":
        return "ok"
    return "weak"


def attach_trust_note(place: dict) -> dict:
    """Set `trust_note` on `place` in place and return it."""
    place["trust_note"] = trust_note(place)
    return place


def attach_trust_notes(places: list[dict]) -> list[dict]:
    """Attach a `trust_note` to every place row. Mutates the rows."""
    for place in places:
        attach_trust_note(place)
    return places


def _stop_label(place: Mapping, index: int) -> str:
    name = place.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return f"stop {index + 1}"


def _weakness(place: Mapping) -> float:
    """Higher means more worth checking. 0 means skip the verify line."""
    status = _status(place)
    if status in _PERMANENTLY_CLOSED:
        return 4.0
    if status in _TEMPORARILY_CLOSED:
        return 3.5
    confidence = _as_float(place.get("confidence"))
    band = _confidence_band(confidence)
    if band == "high":
        return 1.0 if _named_source(place) is False else 0.0
    if band == "moderate":
        return 1.5 - (confidence or 0.0)
    if confidence is None:
        return 2.0
    return 2.0 - confidence


def _has_place_fields(place: Mapping) -> bool:
    """True when the row is a place lookup, not a bare {lat, lon} stop."""
    return any(field in place for field in _PLACE_FIELDS)


def verify_before_going(places: Sequence[Mapping] | None, *, limit: int = 2) -> str | None:
    """One line naming the 1–2 weakest-confidence stops on a plan.

    Returns None when there is nothing worth flagging — empty input,
    bare coordinate stops with no place fields, or every stop is
    high-confidence and in business (or status-unknown) with a
    named-or-implied listing. Always at most `limit` names. Stop
    indexes stay on the original list so a mixed bare+enriched input
    still says "stop 2", not "stop 1".
    """
    if not places:
        return None
    ranked = []
    for i, place in enumerate(places):
        if not _has_place_fields(place):
            continue
        score = _weakness(place)
        if score <= 0:
            continue
        ranked.append((score, _stop_label(place, i), _flag_reason(place) or "low confidence", i))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row[0], row[1].lower(), row[3]))
    picked = ranked[: max(1, limit)]
    parts = [f"{name} ({reason})" for _score, name, reason, _i in picked]
    return "Verify before going: " + "; ".join(parts) + "."


def attach_verify_line(payload: dict, key: str = "results") -> dict:
    """Add `verify_before_going` to a composed itinerary payload, if needed.

    No-ops on error payloads and on result lists with nothing to flag.
    Mutates `payload` and returns it.
    """
    if "error" in payload:
        return payload
    line = verify_before_going(payload.get(key) or [])
    if line:
        payload["verify_before_going"] = line
    return payload
