"""Error envelopes and coordinate checks shared by the PlaceRoot tool handlers.

Moved verbatim out of server.py (no behaviour change): the structured error dicts
handlers return, the @_upstream_errors decorator, the lat/lon bad_request check, and
the point-list bad_request builders. server.py re-exports every name.
"""

import functools
import math
from collections.abc import Callable

from placeroot import (
    geometry_ops,
    overture,
)


def _upstream_error(e: Exception) -> dict:
    """Structured, agent-readable error for a failed remote scan — never a raw traceback."""
    return {"error": "upstream_unavailable", "detail": e.detail, "retry_advised": True}


def _schema_error(e: overture.SchemaDegraded) -> dict:
    return {"error": "schema_degraded", "detail": e.detail, "missing_columns": e.missing}


def _upstream_errors(fn: Callable) -> Callable:
    """The upstream-failure except ladder, once, around a whole handler.

    Nearly every handler repeats `except UpstreamUnavailable: return
    _upstream_error(e)` / `except SchemaDegraded: return _schema_error(e)`
    after each query call it makes. routing.UpstreamUnavailable and
    overture.UpstreamUnavailable are the same class (errors.py; likewise
    SchemaDegraded), so a handler that lists both catches one thing twice.
    Put this between @_tool and the def and the handler can drop its copies:
    the same two classes become the same two envelopes. Applied to
    find_places, compare_areas and verify_claims so far; the other handlers
    still carry the ladder inline and behave identically.
    """

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except overture.UpstreamUnavailable as e:
            return _upstream_error(e)
        except overture.SchemaDegraded as e:
            return _schema_error(e)

    return guarded


def _invalid_coord(lat, lon) -> dict | None:
    """bad_request dict if lat/lon are out of range or non-finite, else None.

    Issue #163 (A2): bbox_around only clamps pole-*overshoot*, so an
    out-of-range lat (e.g. 91.0, or the common LLM mistake of swapping
    lat/lon) produced an inverted ymin>ymax box that silently matched zero
    rows instead of erroring. Every coordinate-taking tool calls this at
    its boundary and returns the error before doing any work. bool is
    checked separately from (int, float) because bool is a subclass of int
    (isinstance(True, int) is True) and a stray True/False would otherwise
    pass the range check.
    """
    for name, val, lo, hi in (("lat", lat, -90.0, 90.0), ("lon", lon, -180.0, 180.0)):
        if (
            not isinstance(val, (int, float))
            or isinstance(val, bool)
            or not math.isfinite(val)
            or not (lo <= val <= hi)
        ):
            return {
                "error": "bad_request",
                "detail": (
                    f"{name}={val!r} is out of range; lat must be in [-90, 90] and "
                    "lon in [-180, 180] (did you swap lat and lon?)"
                ),
            }
    return None


def _point_coord_error(point, label: str) -> dict | None:
    """bad_request dict if point isn't a well-formed, in-range {"lat","lon"}, else None."""
    if not isinstance(point, dict):
        return {
            "error": "bad_request",
            "detail": f"{label} must be a {{'lat': ..., 'lon': ...}} object",
        }
    try:
        lat, lon = float(point.get("lat")), float(point.get("lon"))
    except (TypeError, ValueError):
        return {"error": "bad_request", "detail": f"{label} needs numeric 'lat' and 'lon'"}
    coord_error = _invalid_coord(lat, lon)
    if coord_error is not None:
        coord_error["detail"] = f"{label}: {coord_error['detail']}"
        return coord_error
    return None


def _points_list_coord_error(points, label: str) -> dict | None:
    """bad_request dict if points isn't a non-empty, in-range, within-cap point list, else None."""
    if not isinstance(points, list) or not points:
        return {"error": "bad_request", "detail": f"{label} must be a non-empty list of points"}
    if len(points) > geometry_ops.MAX_BATCH_POINTS:
        return {
            "error": "bad_request",
            "detail": (
                f"{label} accepts at most {geometry_ops.MAX_BATCH_POINTS} points, got {len(points)}"
            ),
        }
    for i, p in enumerate(points):
        coord_error = _point_coord_error(p, f"{label}[{i}]")
        if coord_error is not None:
            return coord_error
    return None
