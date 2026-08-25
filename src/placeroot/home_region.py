"""Home-region resolution bias (#406): explicit config only, a *bias* never a
*filter*, and always disclosed when it changes an answer.

Config sources, in precedence order:

  1. ``PLACEROOT_HOME=<free-text city/area>`` — an env var, read once.
  2. MCP roots carrying a ``placeroot.json`` ``{"home": "<text>"}`` in a
     root directory. STUBBED — see `resolve_home_from_roots` for why.

Resolution is lazy and cached: the home text is geocoded through the
existing name-ranking machinery (`geocode.geocode`) to a single point, and a
metro-scale radius (`HOME_RADIUS_M`) around it stands in for a "home bbox".
Resolution runs at most once per process; a failure (unset, empty, or the
geocoder finding nothing) logs once and leaves the bias permanently off for
that process — never an error surfaced to a tool caller.

`geocode.py`'s ranking (`_rank_key` / `_rank_score`) reads `in_home_region`
to nudge same-tier candidates toward home; see its own comments for how the
nudge is bounded. This module owns only the *resolution* of "where is
home", not the ranking math itself.
"""

from __future__ import annotations

import logging
import os
import threading

from placeroot import geo

logger = logging.getLogger(__name__)

HOME_ENV_VAR = "PLACEROOT_HOME"

# Metro-scale: wide enough to cover a metro's suburbs (the "single-metro
# install" the roadmap names), narrow enough that two nearby-but-distinct
# metros don't both fall inside it. Matches the geocode-address anchor
# radius class already used elsewhere in this module family for "this is
# roughly one metro" (see geocode.py's _CITY_HINT_RADIUS_M neighborhood).
HOME_RADIUS_M = 50_000.0

# Prefix a disclosure note is built from / detected by. Kept as one source
# of truth so a caller stitching a resolve_place note together (or a test)
# never has to restate the wording.
DISCLOSURE_PREFIX = "ranked toward your configured home region"

# Reentrant: resolving the home text itself calls geocode.geocode(), which
# runs through the same ranking code that reads get_home_region() for the
# bias — a plain Lock would deadlock a single-threaded call into itself.
_lock = threading.RLock()
_resolved = False  # True once resolution has been attempted (success or not)
_home: dict | None = None  # {"name", "lat", "lon"} once resolved, else None


def resolve_home_from_roots() -> str | None:
    """STUB (#406): MCP roots as a home-region source. Always returns None today.

    Investigated against the pinned SDK (``mcp[cli]>=2.0.0``,
    ``.venv/lib/python3.*/site-packages/mcp``): server-side access to a
    client's roots is `mcp.server.session.ServerSession.list_roots`, an
    *async* method that (a) is only reachable from inside an in-flight
    request's session — there is no server-level "current client roots" the
    way there is a current release or cache dir — and (b) is itself marked
    ``@deprecated("The roots capability is deprecated as of 2026-07-28
    (SEP-2577)", ...)`` in this SDK version. The framework's own
    request-scoped root-fetch marker (`mcp.server.mcpserver.resolve.ListRoots`)
    exists only as something a *resolver* can request while handling one
    tool call, with the response injected back into that same call.

    Home resolution has to run once, lazily, ahead of (and shared by) every
    geocode/resolve_place/find_near/from_to call — including calls a client
    makes before ever invoking a tool that could carry a request context.
    Threading an async, per-request, now-deprecated capability through that
    shared synchronous ranking path would mean either (a) adding a Context
    parameter to every ranking entry point for a capability most clients
    don't advertise, or (b) polling it opportunistically and accepting a
    bias that silently changes mid-session depending on which tool call
    happened to carry a session. Both contort the architecture for a
    capability the SDK itself is walking back.

    Left as a named stub — always None — so a client that *does* support
    roots and ships a ``placeroot.json`` ``{"home": ...}`` in one degrades
    to silent no-op today (never a wrong or stale bias) rather than
    pretending to read it. `PLACEROOT_HOME` is the supported path until a
    future SDK exposes this outside a request handler.
    """
    return None


def _home_text_from_env() -> str | None:
    text = os.environ.get(HOME_ENV_VAR)
    if text is None:
        return None
    text = text.strip()
    return text or None


def _home_text() -> str | None:
    """Env var beats roots (explicit-beats-inference); roots is a stub today."""
    return _home_text_from_env() or resolve_home_from_roots()


def _resolve_home_point(text: str) -> dict | None:
    """Geocode `text` to a home point, or None (logs once) on failure.

    Imported lazily: geocode.py's ranking reads this module, so a top-level
    import back the other way would cycle.
    """
    from placeroot import geocode as geocode_mod
    from placeroot import overture

    # Log lines below deliberately omit the configured text, resolved name,
    # and coordinates: the user's home location is sensitive data (CodeQL
    # py/clear-text-logging-sensitive-data), and PlaceRoot's privacy posture
    # is that nothing about the user's home belongs in logs — even local
    # ones. Do not "improve" these logs by adding the location back.
    try:
        hits = geocode_mod.geocode(text, limit=1)
    except (overture.UpstreamUnavailable, overture.SchemaDegraded) as e:
        logger.warning("home_region: failed to resolve %s: %s", HOME_ENV_VAR, e)
        return None
    except Exception:  # noqa: BLE001 - a bad home config must never break the server
        logger.warning(
            "home_region: unexpected error resolving %s", HOME_ENV_VAR, exc_info=True,
        )
        return None
    if not hits:
        logger.warning(
            "home_region: %s did not resolve to any place; bias disabled", HOME_ENV_VAR,
        )
        return None
    top = hits[0]
    logger.info("home_region: %s resolved; ranking bias enabled", HOME_ENV_VAR)
    return {"name": top["name"], "lat": top["lat"], "lon": top["lon"]}


def get_home_region() -> dict | None:
    """Lazily resolve and cache the configured home region, or None.

    Thread-safe; resolved at most once per process (until
    `reset_home_region_state` — tests only). Never raises: a missing config
    or an unresolvable home text disables the bias silently (one warning
    log), matching #406's "never an error to the user" contract.
    """
    global _resolved, _home
    with _lock:
        if _resolved:
            return _home
        # Marked resolved *before* the geocode() call below, not after: that
        # call re-enters this same module for its own ranking (home_bias
        # reads get_home_region()), and without this a self-resolving
        # PLACEROOT_HOME would recurse into resolving itself forever. A
        # reentrant call during resolution correctly sees "no home yet"
        # (_home is still None here) rather than the bias applying to the
        # very query used to establish it.
        _resolved = True
        text = _home_text()
        _home = _resolve_home_point(text) if text else None
        return _home


def home_bias_active() -> bool:
    """True once a home region has resolved. Cheap; safe to call per-row."""
    return get_home_region() is not None


def in_home_region(lat: float | None, lon: float | None) -> bool:
    """True when (lat, lon) sits within HOME_RADIUS_M of the configured home
    point. False whenever no home is configured/resolved, or coordinates are
    missing — never raises."""
    if lat is None or lon is None:
        return False
    home = get_home_region()
    if home is None:
        return False
    return geo.haversine_m(home["lat"], home["lon"], lat, lon) <= HOME_RADIUS_M


def disclosure_note(home: dict) -> str:
    """The one-line note attached when the bias changed an answer's top result."""
    return (
        f"{DISCLOSURE_PREFIX} ({home['name']}); pass a city/near hint to override"
    )


def kick_home_autowarm() -> None:
    """Best-effort: if a home region resolves, schedule its background tile
    warm the same way a city-scale resolve does (see autowarm.py). Never
    blocks, never raises. Intended to run on its own daemon thread at
    server startup (server.py's `_warm_home_async`), mirroring
    `_warm_divisions_async` — resolving the home text here also warms
    geocode's lazy cache for every ranking call that follows.
    """
    try:
        home = get_home_region()
    except Exception:  # noqa: BLE001 - startup warm must never break startup
        logger.warning("home_region: failed to resolve home for autowarm", exc_info=True)
        return
    if home is None:
        return
    try:
        from placeroot import autowarm

        autowarm.schedule_autowarm(home["lat"], home["lon"])
    except Exception:  # noqa: BLE001 - startup warm must never break startup
        logger.warning("home_region: failed to schedule autowarm", exc_info=True)


def reset_home_region_state() -> None:
    """Tests only: drop the cached resolution so the next `get_home_region()`
    call re-reads `PLACEROOT_HOME`."""
    global _resolved, _home
    with _lock:
        _resolved = False
        _home = None
