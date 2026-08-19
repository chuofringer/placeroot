"""Background metro warm on first city-scale resolve (#330).

Users never say "warm up". The first successful geocode / resolve_place /
resolve_area of a city-scale hit (a locality or equivalent, not a POI,
street, or address) kicks the existing warmup path (`server._prewarm_region`
→ `cache.prewarm_bbox`) on a daemon thread, then tries to build and persist
a walk graph so a later route can load it instead of rebuilding.

This does not replace `warmup_city` or `PLACEROOT_WARM_REGION`. Those stay
the explicit / operator knobs. This is the "any question" path: kick and
return, never add latency to the resolve that triggered it.

Honesty: warming tiles is not a built street graph. Tiles make the extract
cheaper than a cold S3 scan; the first walk still builds (or loads) a
graph. Failures log and never fail the user-facing resolve.
"""

from __future__ import annotations

import logging
import math
import threading
from pathlib import Path

from placeroot import cache, release

logger = logging.getLogger(__name__)

# City or equivalent locality. Country/region/county centroids are too
# coarse for an 8 km prewarm. A POI, street, address, or postcode is not
# a metro resolve.
CITY_SCALE_TYPES = frozenset({
    "locality",
    "localadmin",
    "neighborhood",
    "macrohood",
    "borough",
})

_NOT_CITY_TYPES = frozenset({
    "place",
    "postcode",
    "address",
    "street",
    "country",
    "region",
    "county",
    "dependency",
})

# ~28 km cells: Shibuya and central Tokyo share a cell; Austin and Dallas
# do not. Used for in-flight + disk-marker dedup so the same metro is not
# re-COPY'd on every subsequent resolve (including after restart).
_METRO_TILE_DEG = 0.25

_inflight: set[tuple[int, int]] = set()
_inflight_lock = threading.Lock()


def is_city_scale(hit: dict) -> bool:
    """True for a city / locality / neighborhood; false for a POI or admin region."""
    if hit.get("kind") == "place":
        return False
    typ = (hit.get("type") or "").lower()
    if typ in _NOT_CITY_TYPES:
        return False
    if typ:
        return typ in CITY_SCALE_TYPES
    return False


def metro_key(lat: float, lon: float) -> tuple[int, int]:
    """Coarse tile identity for one metro's autowarm marker."""
    return (
        math.floor(lat / _METRO_TILE_DEG),
        math.floor(lon / _METRO_TILE_DEG),
    )


def _marker_path(key: tuple[int, int]) -> Path:
    ty, tx = key
    return cache.cache_dir() / release.resolve_release() / "autowarm" / f"{ty}_{tx}.warm"


def warm_marker_exists(key: tuple[int, int]) -> bool:
    return _marker_path(key).is_file()


def write_warm_marker(key: tuple[int, int]) -> None:
    path = _marker_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def clear_autowarm_state() -> None:
    """Drop in-flight keys. Tests only; does not delete disk markers."""
    with _inflight_lock:
        _inflight.clear()


def maybe_autowarm_hit(hit: dict | None) -> None:
    """Schedule background prewarm if `hit` is city-scale. Never raises."""
    try:
        if not hit or not is_city_scale(hit):
            return
        lat, lon = hit.get("lat"), hit.get("lon")
        if lat is None or lon is None:
            return
        schedule_autowarm(float(lat), float(lon))
    except Exception:  # noqa: BLE001 - resolve must not fail because warm failed
        logger.warning("autowarm: failed to schedule from resolve", exc_info=True)


def schedule_autowarm(lat: float, lon: float) -> None:
    """Kick tile prewarm + walk-graph persist in the background.

    Does not wait. Does not hold conn_lock. Deduped per metro (in-flight
    set + disk marker) so a session of resolves, or a restart, does not
    re-COPY the same tiles. No-op when the tile cache is off.

    Tiles ≠ built graph: this schedules both, but a warm tile cache alone
    does not make the first walk instant.
    """
    if not cache.enabled():
        return
    key = metro_key(lat, lon)
    with _inflight_lock:
        if key in _inflight or warm_marker_exists(key):
            return
        _inflight.add(key)
    threading.Thread(
        target=_run_autowarm,
        args=(lat, lon, key),
        name=f"placeroot-autowarm-{key[0]}-{key[1]}",
        daemon=True,
    ).start()


def _run_autowarm(lat: float, lon: float, key: tuple[int, int]) -> None:
    """Own thread: existing prewarm path, then a walk graph. Never raises out."""
    try:
        from placeroot import routing, server

        # Existing warmup path — own connections inside prewarm_bbox, and
        # conn_lock is not held across COPYs (see server._prewarm_region).
        server._prewarm_region(lat, lon, server.DEFAULT_WARMUP_RADIUS_M)
        try:
            write_warm_marker(key)
        except OSError as e:
            logger.warning("autowarm: could not write warm marker %s: %s", key, e)
        # Walk is the cliff (20–41s rebuild). Drive is a much larger extract
        # and is left to the first drive query. Tiles being warm only makes
        # this extract cheaper than cold S3; the graph still has to be
        # built (or loaded from disk) here.
        radius = min(server.DEFAULT_WARMUP_RADIUS_M, routing.WALK_MAX_RADIUS_M)
        routing._get_or_build_graph(
            lat, lon, radius, "walk", None, want_shapes=True,
        )
    except Exception as e:  # noqa: BLE001 - background warm must not surface
        logger.warning("autowarm: background warm failed for %s: %s", key, e)
    finally:
        with _inflight_lock:
            _inflight.discard(key)
