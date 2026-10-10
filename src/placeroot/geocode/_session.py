"""Per-session state: resolve LRU, last-good city memory, POI aliases, city hints."""

import json
import sys as _sys
import threading
from collections import OrderedDict
from importlib import resources

from placeroot import geo, session

_pkg = _sys.modules["placeroot.geocode"]


# --- #329: city hints, POI aliases, last-resolve LRU ----------------------

# Trailing tokens we will treat as a city= hint without a table lookup.
# Ambiguous US namesakes the ranking corpus pins (Springfield, Portland) stay
# off this list so a suffix parse cannot override #47.
_WELL_KNOWN_CITIES = {
    "amsterdam": "Amsterdam",
    "anaheim": "Anaheim",
    "barcelona": "Barcelona",
    "berlin": "Berlin",
    "brooklyn": "Brooklyn",
    "cambridge": "Cambridge",
    "casablanca": "Casablanca",
    "chicago": "Chicago",
    "dublin": "Dublin",
    "london": "London",
    "los angeles": "Los Angeles",
    "new york": "New York",
    "oakland": "Oakland",
    "palo alto": "Palo Alto",
    "paris": "Paris",
    "rio de janeiro": "Rio de Janeiro",
    "roma": "Rome",
    "rome": "Rome",
    "san francisco": "San Francisco",
    "seattle": "Seattle",
    "singapore": "Singapore",
    "stanford": "Stanford",
    "sydney": "Sydney",
    "tokyo": "Tokyo",
}


# Canonical coords for the Casablanca→Chile class: a well-known city query
# must not lose to a populated namesake 10,000 km away. Only cities with a
# documented wrong-hemisphere failure live here — adding Cambridge/Portland
# would fight the ranking corpus.
_WELL_KNOWN_CITY_COORDS = {
    "casablanca": (33.5731, -7.5898),
}


# When a city hint is in play, drop division (and far place) hits outside
# this radius. Same "same metro" idea as _PLACES_FALLBACK_RADIUS_M, a bit
# wider so a mall on the edge of town still counts.
_CITY_HINT_RADIUS_M = 50_000


_RESOLVE_LRU_MAX = 256

_resolve_lru: OrderedDict[tuple, list[dict]] = OrderedDict()

_resolve_lru_lock = threading.Lock()

# The last good (city, coords) a resolve pinned, per client session. Over
# --http one process serves every connected client, and a module global
# here made client A's city the inferred city for client B's POI-shaped
# query -- and, since the inferred city is part of the resolve cache key,
# handed B A's answer. Keyed by session.session_id() (the SDK's
# Mcp-Session-Id over HTTP, one constant over stdio), bounded as an LRU
# so a long-lived HTTP server never grows with the clients it has seen.
# One lock around the whole structure: #335 _resolve_pair runs two
# resolve_place calls concurrently, and two bare assignments can tear
# (city from one pin, coords from the other).
_LAST_GOOD_SESSIONS_MAX = 256

_last_good_lock = threading.Lock()

_last_good_by_session: OrderedDict[str, tuple[str | None, tuple[float, float] | None]] = (
    OrderedDict()
)

_POI_ALIASES: dict[str, dict] | None = None


def _fold_query_key(s: str) -> str:
    return " ".join(_pkg._normalize_for_match(s).split())


def _poi_aliases() -> dict[str, dict]:
    """Tiny landmark → city overlay on the bundled stage-0 index (#329)."""
    global _POI_ALIASES
    if _pkg._POI_ALIASES is None:
        try:
            src = resources.files("placeroot") / "data" / "geocode-index" / "aliases.json"
            _pkg._POI_ALIASES = json.loads(src.read_text(encoding="utf-8")) if src.is_file() else {}
        except (OSError, TypeError, json.JSONDecodeError) as e:
            _pkg.logger.warning("POI alias table unreadable (%s); continuing without it", e)
            _pkg._POI_ALIASES = {}
    return _pkg._POI_ALIASES


def _lookup_poi_alias(query: str) -> dict | None:
    row = _poi_aliases().get(_pkg._fold_query_key(query))
    if not row:
        return None
    try:
        return {"city": row["city"], "lat": float(row["lat"]), "lon": float(row["lon"])}
    except (KeyError, TypeError, ValueError):
        return None


def _alias_names_for(query: str) -> list[str]:
    """Other bundled spellings of the same landmark pin as `query`."""
    target = _pkg._lookup_poi_alias(query)
    if target is None:
        place_q, _city, _coords = _pkg._extract_city_hint(query)
        target = _pkg._lookup_poi_alias(place_q)
    if target is None:
        return []
    names = []
    for key, raw in _poi_aliases().items():
        try:
            if (
                abs(float(raw["lat"]) - target["lat"]) < 1e-3
                and abs(float(raw["lon"]) - target["lon"]) < 1e-3
            ):
                names.append(key)
        except (KeyError, TypeError, ValueError):
            continue
    return names


def _canonical_city(token: str) -> str | None:
    return _WELL_KNOWN_CITIES.get(_pkg._fold_query_key(token))


def _extract_city_hint(query: str) -> tuple[str, str | None, tuple[float, float] | None]:
    """Split a trailing well-known city or a POI alias off `query`.

    Returns (place_query, city_name, coords). coords come only from the
    bundled alias list — they bound the search, they are not an answer.
    """
    # #481: a whole-query alias wins over the trailing-city split. "Notre-Dame
    # de Paris" ends in a well-known city, and splitting first left the head
    # "Notre-Dame de" (no alias) with a bare city hint — the curated pin for
    # the landmark, keyed under the full phrase, was never consulted.
    alias = _pkg._lookup_poi_alias(query)
    if alias:
        return query, alias["city"], (alias["lat"], alias["lon"])
    tokens = query.strip().split()
    for n in (2, 1):
        if len(tokens) <= n:
            continue
        tail = " ".join(tokens[-n:])
        head = " ".join(tokens[:-n]).strip()
        if not head:
            continue
        if tail.lower().strip(".,") in _pkg._GENERIC_PLACE_WORDS:
            continue
        city = _canonical_city(tail)
        if city is None:
            continue
        alias = _pkg._lookup_poi_alias(head)
        if alias:
            return head, alias["city"] or city, (alias["lat"], alias["lon"])
        return head, city, None
    return query, None, None


def _query_is_poi_shaped(query: str) -> bool:
    """Whether `query` names a thing rather than a city — last-city applies."""
    if _pkg._lookup_poi_alias(query):
        return True
    if _pkg._names_a_feature(query):
        return True
    tokens = query.strip().split()
    if len(tokens) >= 3:
        return True
    if len(tokens) == 2:
        if _canonical_city(query):
            return False
        if tokens[0].lower().strip(".,") in _pkg._NAME_PREFIX_WORDS:
            return False
        return True
    return False


def _alias_anchor(query: str) -> tuple[float, float, str | None] | None:
    """(lat, lon, name_query) from a POI alias, or None."""
    place_q, _city, coords = _pkg._extract_city_hint(query)
    if coords is None:
        return None
    name_query = None if _pkg._nothing_but_generic(place_q) else place_q
    return (coords[0], coords[1], name_query)


def _well_known_city_near(row: dict, query: str) -> int:
    """0 if `row` sits on the canonical pin for a well-known city query."""
    pin = _WELL_KNOWN_CITY_COORDS.get(_pkg._fold_query_key(query))
    if pin is None:
        return 1
    return 0 if geo.haversine_m(pin[0], pin[1], row["lat"], row["lon"]) <= 80_000 else 1


def _resolve_cache_key(
    query: str,
    city: str | None,
    near_lat: float | None,
    near_lon: float | None,
    lang: str | None = None,
    country: str | None = None,
) -> tuple:
    near = (
        (round(near_lat, 3), round(near_lon, 3))
        if near_lat is not None and near_lon is not None
        else (None, None)
    )
    # #410: lang is part of the key — otherwise a lang="de" call would replay
    # a cache entry another lang (or no lang at all) already populated.
    # #457: country too, for the same reason.
    return (
        _pkg._fold_query_key(query),
        _pkg._fold_query_key(city) if city else "",
        *near,
        lang or "",
        country or "",
    )


def _resolve_cache_get(
    query: str,
    city: str | None,
    near_lat: float | None,
    near_lon: float | None,
    lang: str | None = None,
    country: str | None = None,
) -> list[dict] | None:
    """The full ranked candidate list cached for this key, or None.

    `limit` is deliberately not part of the key: resolve_place caches the
    *un-truncated* list and slices on read, so a `limit=1` call followed
    by a `limit=3` one for the same query gets three rows, not one.
    """
    key = _resolve_cache_key(query, city, near_lat, near_lon, lang, country)
    with _resolve_lru_lock:
        rows = _pkg._resolve_lru.get(key)
        if rows is None:
            return None
        _pkg._resolve_lru.move_to_end(key)
        return [dict(r) for r in rows]


def _resolve_cache_put(
    query: str,
    city: str | None,
    near_lat: float | None,
    near_lon: float | None,
    rows: list[dict],
    lang: str | None = None,
    country: str | None = None,
) -> None:
    key = _resolve_cache_key(query, city, near_lat, near_lon, lang, country)
    stored = [dict(r) for r in rows]
    with _resolve_lru_lock:
        _pkg._resolve_lru[key] = stored
        _pkg._resolve_lru.move_to_end(key)
        while len(_pkg._resolve_lru) > _RESOLVE_LRU_MAX:
            _pkg._resolve_lru.popitem(last=False)


def _last_good() -> tuple[str | None, tuple[float, float] | None]:
    """The (city, coords) the current session last pinned; (None, None) if none."""
    sid = session.session_id()
    with _last_good_lock:
        state = _pkg._last_good_by_session.get(sid)
        if state is None:
            return None, None
        _pkg._last_good_by_session.move_to_end(sid)
        return state


def _remember_last_city(city: str | None, top: dict) -> None:
    name = (city or "").strip() or None
    if name is None:
        ctx = top.get("admin_context") or []
        name = ctx[-1] if ctx else top.get("name")
    coords = None
    if top.get("lat") is not None and top.get("lon") is not None:
        coords = (top["lat"], top["lon"])
    sid = session.session_id()
    if session.is_ephemeral(sid):
        # A one-request session: no later call can read this back, so
        # storing it would only evict a session that can.
        return
    with _last_good_lock:
        last_city, last_coords = _pkg._last_good_by_session.get(sid, (None, None))
        if name:
            last_city = name
        if coords is not None:
            last_coords = coords
        _pkg._last_good_by_session[sid] = (last_city, last_coords)
        _pkg._last_good_by_session.move_to_end(sid)
        while len(_pkg._last_good_by_session) > _pkg._LAST_GOOD_SESSIONS_MAX:
            _pkg._last_good_by_session.popitem(last=False)


def clear_resolve_session(*, clear_all: bool = False) -> None:
    """Drop the in-process resolve LRU and last-city memory (#329).

    Tests and a fresh conversation call this so one resolve cannot leak a
    city hint into the next. Not a second cache — the tile cache is
    cache.py's, and this is only the last-resolve dict in this module.

    The last-city memory is per client session (see session.py): the
    default drops the current session's; `clear_all=True` drops every
    session's. The resolve LRU is keyed by the resolved inputs rather than
    by session and is always dropped whole.
    """
    with _resolve_lru_lock:
        _pkg._resolve_lru.clear()
    with _last_good_lock:
        if clear_all:
            _pkg._last_good_by_session.clear()
        else:
            _pkg._last_good_by_session.pop(session.session_id(), None)
    _pkg._clear_table_derived_caches()
