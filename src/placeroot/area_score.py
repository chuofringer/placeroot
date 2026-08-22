"""Score a candidate locality against amenity requirements (issue #349).

Sub-issue 2 of 3 breaking down #305 (suggest_areas: inverse area search).
#348 (divisions.py's divisions_in_polygon) turns a reachable shed into
candidate localities; this module turns "parks, groceries, quiet streets"
into a per-requirement 0-1 score plus a one-line reason for each, so the
future suggest_areas tool (#350) can rank candidates with evidence instead
of a bare list. No new MCP tool here — this is the internal scoring layer
and its tests.

Design:

- Each requirement is free text ("parks", "groceries", "coffee shop").
  It is resolved against the bundled category taxonomy the same way
  compare_areas' priorities are (#354): categories.search_categories for
  free-text -> slug resolution, then overture._place_categories_or_filter
  for exact/hierarchy matching against places rows (never substring ILIKE
  — "park" must never count parking, "school" must never count a driving
  school).
- Per measurable requirement, one bounded scan around the locality's
  centroid: an exact/hierarchy category count (bbox+radius filtered, no
  row cap — an aggregate, same bounded shape compare_areas' verdict
  uses) plus, when at least one match exists, a single nearest-place
  lookup (LIMIT 1) for its distance. Both normalize into one 0-1 score
  (see _requirement_score).
- HONESTY (matching verdict.py/compare_areas' conventions): a requirement
  that names a subjective quality Overture cannot measure ("quiet",
  "safe", "good schools") is never silently scored against whatever noun
  happens to sit in the phrase — it comes back measurable: false with a
  note, checked before any category resolution is even attempted. A
  requirement whose free text matches no taxonomy category at all (or
  whose dataset columns are too degraded to count) is likewise
  measurable: false rather than guessed.
"""

from __future__ import annotations

import math
import re

from placeroot import categories, geo, gers, overture

# score_locality accepts at most this many requirements — same cap and
# reasoning as compare_areas' priorities (#304): a scored comparison is a
# ranked shortlist, not a survey, and each requirement costs at least one
# bounded scan.
MAX_REQUIREMENTS = 8

# A requirement's raw place count saturates its count-component at this
# many matches within radius_m — enough to call an amenity "well covered"
# without rewarding a requirement that happens to sit in a dense retail
# strip far more than one that has a modest, genuinely walkable cluster.
SATURATION_COUNT = 5

# Words that name a subjective quality or judgment, not a thing Overture's
# taxonomy can count or locate — checked against the requirement text
# BEFORE any category resolution is attempted, so a phrase like "safe
# grocery store" or "good schools" never gets silently scored off whatever
# noun happens to sit in it. Deliberately small and literal (word-boundary
# matched, not substring) so it never eats an unrelated category word.
SUBJECTIVE_MARKERS = frozenset({
    "quiet", "quieter", "quietest",
    "safe", "safer", "safest", "safety", "unsafe", "crime", "sketchy",
    "good", "great", "best", "top", "quality", "reputable", "excellent",
    "nice", "pleasant", "clean", "cleaner", "tidy",
    "friendly", "welcoming", "family-friendly",
    "affordable", "cheap", "cheaper", "inexpensive",
    "expensive", "pricey", "upscale", "trendy", "hip",
    "diverse", "vibrant", "up-and-coming", "gentrifying", "gentrified",
    "walkable", "walkability",
})

HONESTY = (
    "Requirement scores are open-data place counts and nearest-distance "
    "near each locality's center — never crime, school quality, noise, "
    "rent, or demographics. A requirement naming a subjective quality "
    "('quiet', 'safe', 'good schools') or matching no Overture category "
    "comes back measurable: false rather than a guessed score."
)

_WORD = re.compile(r"[a-z][a-z-]*")


class LocalityNotFound(Exception):
    """division_id did not resolve to any known entity (issue #349).

    Distinct from gers.gers_lookup's own UpstreamUnavailable/SchemaDegraded
    (which mean "could not check"): this means the id was checked and
    confirmed absent.
    """

    def __init__(self, division_id: str):
        detail = f"division_id {division_id!r} did not resolve to a known division"
        super().__init__(detail)
        self.detail = detail
        self.division_id = division_id


def _check_coord(lat, lon) -> None:
    """Raise ValueError for an out-of-range or non-finite lat/lon.

    Mirrors server._invalid_coord's checks (issue #163) but raises rather
    than returning a bad_request dict — this module has no MCP tool yet
    (#350), so ValueError is what compare_areas-style internal validation
    raises and a future server wrapper would translate.
    """
    for name, val, lo, hi in (("lat", lat, -90.0, 90.0), ("lon", lon, -180.0, 180.0)):
        if (
            not isinstance(val, (int, float))
            or isinstance(val, bool)
            or not math.isfinite(val)
            or not (lo <= val <= hi)
        ):
            raise ValueError(
                f"{name}={val!r} is out of range; lat must be in [-90, 90] and "
                "lon in [-180, 180] (did you swap lat and lon?)"
            )


def _subjective_note(text: str) -> str | None:
    """A note if `text` names a subjective quality, else None.

    Word-boundary matched against SUBJECTIVE_MARKERS so a real category
    word is never caught by accident ("cheap" would not, for instance,
    match "cheese_shop" — that's a whole-word compare, not a substring
    one).
    """
    words = set(_WORD.findall(text.lower()))
    hit = sorted(words & SUBJECTIVE_MARKERS)
    if not hit:
        return None
    return (
        f"{', '.join(hit)!s} is a subjective judgment Overture's open data "
        "cannot measure (no crime, noise, or quality-rating dataset) — "
        "not scored, even if part of the phrase names a real category."
    )


def _resolve_category(text: str) -> dict | None:
    """Best category-taxonomy match for free text, or None.

    Tries the raw text first, then a naive last-word singularization
    ("parks" -> "park") as a second candidate — plural amenity phrasing is
    extremely common in this tool's input and search_categories' whole-
    query tiers are slug-exact/prefix/substring, so "parks" alone often
    surfaces an unrelated sibling slug (e.g. "mountain_bike_parks") that
    happens to contain "parks" as a substring instead of the plain "park"
    a caller obviously means. Picking the higher-confidence of the two
    candidates fixes that without changing search_categories itself, which
    other callers (find_places' fallback, search_categories the MCP tool)
    still rely on to keep matching plurals via its own token-coverage
    fallback for phrases it can't singularize this cheaply.
    """
    candidates = [text]
    words = text.split()
    if words and len(words[-1]) > 3 and words[-1].endswith("s"):
        candidates.append(" ".join(words[:-1] + [words[-1][:-1]]))

    best = None
    for candidate in candidates:
        matches = categories.search_categories(candidate, limit=1)
        if matches and (best is None or matches[0]["confidence"] > best["confidence"]):
            best = matches[0]
    return best


def _requirement_score(count: int, nearest_distance_m: float | None, radius_m: float) -> float:
    """Count + nearest-distance -> one 0-1 score.

    Half the score is how many matches exist (saturating at
    SATURATION_COUNT, so a requirement isn't rewarded without bound for
    sitting in an unusually dense cluster); half is how close the nearest
    one is relative to radius_m (0 at the edge of the search radius, 1 at
    the center). A requirement with zero matches scores 0 outright —
    there is no distance to reward.
    """
    if count <= 0:
        return 0.0
    count_component = min(count / SATURATION_COUNT, 1.0)
    if nearest_distance_m is None or radius_m <= 0:
        proximity_component = 0.0
    else:
        proximity_component = max(0.0, 1.0 - (nearest_distance_m / radius_m))
    return round(0.5 * count_component + 0.5 * proximity_component, 3)


def _requirement_reason(
    label: str, measurable: bool, count: int | None, nearest_distance_m: float | None,
    score: float | None, note: str | None,
) -> str:
    if not measurable:
        return f"{label}: not measurable — {note}"
    if count == 0:
        return f"{label}: none found within the search radius (score 0.0)."
    nearest_bit = (
        f", nearest {nearest_distance_m:.0f}m" if nearest_distance_m is not None else ""
    )
    return f"{label}: {count} within the search radius{nearest_bit} (score {score})."


def _locate(
    division_id: str | None, lat: float | None, lon: float | None,
    near_lat: float | None, near_lon: float | None,
) -> tuple[float, float, dict]:
    """Resolve the locality's scoring center, and a compact locality descriptor.

    Exactly one of division_id or the (lat, lon) pair must be given. A
    division_id is resolved via gers.gers_lookup — same GERS-id-to-point
    machinery every other tool uses (gers.py); near_lat/near_lon narrow
    that lookup the same way it does everywhere else (bounded to a 50km
    box instead of an unhinted whole-theme scan) when the caller has one
    (e.g. the shed's own centroid, from the isochrone that produced the
    candidate localities).
    """
    if (division_id is None) == (lat is None and lon is None):
        raise ValueError("pass exactly one of division_id or both lat and lon")
    if division_id is not None:
        if not isinstance(division_id, str) or not division_id.strip():
            raise ValueError("division_id must be a non-empty string")
        entity = gers.gers_lookup(division_id, near_lat=near_lat, near_lon=near_lon)
        if entity is None:
            raise LocalityNotFound(division_id)
        rlat, rlon = entity.get("lat"), entity.get("lon")
        if rlat is None or rlon is None:
            raise LocalityNotFound(division_id)
        return float(rlat), float(rlon), {
            "division_id": division_id,
            "name": entity.get("name"),
            "lat": round(float(rlat), 6),
            "lon": round(float(rlon), 6),
        }
    if lat is None or lon is None:
        raise ValueError("pass exactly one of division_id or both lat and lon")
    _check_coord(lat, lon)
    return float(lat), float(lon), {"lat": round(float(lat), 6), "lon": round(float(lon), 6)}


def score_locality(
    requirements: list[str],
    division_id: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = 1000.0,
    near_lat: float | None = None,
    near_lon: float | None = None,
) -> dict:
    """Score a locality against a list of free-text amenity requirements.

    locality: pass either division_id (a GERS id, e.g. from
    divisions.divisions_in_polygon's results) or both lat and lon — never
    both, never neither. near_lat/near_lon are an optional location hint
    for the division_id path (see gers.gers_lookup); pass them (e.g. the
    shed's centroid) whenever available to keep that lookup bounded.

    requirements: 1-8 free-text strings ("parks", "groceries", "quiet
    streets"). Each resolves against the bundled category taxonomy the
    same way compare_areas' priorities do (#354) — exact/hierarchy match,
    never substring, so "park" never counts parking. A requirement naming
    a subjective quality Overture cannot measure, or matching no taxonomy
    category at all, comes back {"measurable": false, "note": ...} rather
    than a guessed score (see module docstring).

    Returns {"locality", "radius_m", "requirements": [{"label",
    "measurable", "category", "count", "nearest_distance_m", "score",
    "reason", "note"?}, ...], "overall_score" (mean of the measurable
    requirements' scores, or None if none were measurable), "honesty"}.

    Raises ValueError for a malformed locality/requirements argument;
    LocalityNotFound if division_id does not resolve; propagates
    UpstreamUnavailable/SchemaDegraded from the underlying scans (a
    partial score is never returned — same as compare_areas).
    """
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("requirements must be a non-empty list of strings")
    if len(requirements) > MAX_REQUIREMENTS:
        raise ValueError(
            f"requirements accepts at most {MAX_REQUIREMENTS}, got {len(requirements)}"
        )
    for req in requirements:
        if not isinstance(req, str) or not req.strip():
            raise ValueError("every requirement must be a non-empty string")

    rlat, rlon, locality = _locate(division_id, lat, lon, near_lat, near_lon)
    radius_m = geo.clamp_radius_m(radius_m)

    # First pass: subjective-language check, then taxonomy resolution.
    # Requirements that clear both share one batched count scan (issue
    # #354's pattern) rather than one full bbox+radius scan each.
    resolved: list[dict] = []
    for raw in requirements:
        label = raw.strip()
        subjective = _subjective_note(label)
        if subjective is not None:
            resolved.append({"label": label, "measurable": False, "note": subjective})
            continue
        match = _resolve_category(label)
        if match is None:
            resolved.append({
                "label": label,
                "measurable": False,
                "note": (
                    "no matching category in the Overture taxonomy — "
                    "try search_categories to find a phrasing it recognizes"
                ),
            })
            continue
        resolved.append({"label": label, "measurable": True, "category": match["slug"]})

    slugs = [r["category"] for r in resolved if r["measurable"]]
    counts = overture._count_places_by_category(rlat, rlon, radius_m, slugs) if slugs else {}
    if slugs and counts is None:
        # Dataset too degraded to count categories at all (issue #354's
        # honesty rule): every category-based requirement is unmeasured,
        # not fabricated as zero.
        note = (
            "the active dataset's category columns are degraded/missing, "
            "so this requirement could not be counted — an unfiltered "
            "place count would have fabricated a score"
        )
        for row in resolved:
            if row["measurable"]:
                row["measurable"] = False
                row["note"] = note
                del row["category"]
        counts = {}

    scored: list[dict] = []
    measurable_scores: list[float] = []
    for row in resolved:
        if not row["measurable"]:
            row["reason"] = _requirement_reason(
                row["label"], False, None, None, None, row["note"]
            )
            scored.append(row)
            continue
        slug = row["category"]
        count = counts.get(slug, 0)
        nearest_distance_m = None
        if count > 0:
            nearest = overture.find_places_for_categories(rlat, rlon, radius_m, [slug], limit=1)
            if nearest:
                nearest_distance_m = float(nearest[0]["distance_m"])
        score = _requirement_score(count, nearest_distance_m, radius_m)
        measurable_scores.append(score)
        scored.append({
            "label": row["label"],
            "measurable": True,
            "category": slug,
            "count": count,
            "nearest_distance_m": (
                round(nearest_distance_m, 1) if nearest_distance_m is not None else None
            ),
            "score": score,
            "reason": _requirement_reason(
                row["label"], True, count, nearest_distance_m, score, None
            ),
        })

    overall_score = (
        round(sum(measurable_scores) / len(measurable_scores), 3) if measurable_scores else None
    )
    return {
        "locality": locality,
        "radius_m": round(radius_m, 1),
        "requirements": scored,
        "overall_score": overall_score,
        "honesty": HONESTY,
    }
