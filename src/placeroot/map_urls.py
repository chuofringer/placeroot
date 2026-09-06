"""Pasted map link -> coordinate (issue #461).

People paste map links into chats ("meet here: https://maps.app.goo.gl/...").
This module turns Google Maps, Apple Maps, OpenStreetMap and `geo:` links back
into a coordinate, a zoom level and whatever place name the link carried,
without an API key. Everything here is stdlib and pure: `urllib.parse` and
`re` for the offline parse, `urllib.request` only for following a short
link's redirects — the one network hop, and only for the short-link hosts.

Two layers:

- `parse_map_url(url)` — offline. Returns {"lat", "lon", "zoom"?, "label"?,
  "note"?, "provider"} when the URL itself carries a coordinate, or an
  {"error": ...} dict ("no_location" with the `label` kept when the link
  named a place but no coordinate, "unsupported_url", "bad_request").
- `resolve(url, *, fetch=None)` — the same, plus short-link expansion via
  `follow_redirects`, tagging `resolved_via` ("url" | "redirect") and
  `final_url` when a redirect was followed.

The server-side tool wraps `resolve`, resolving a name-only link through the
place search and attaching a reverse-geocoded place row.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

SUPPORTED_PROVIDERS = ["google", "apple", "osm", "geo"]

# Hosts whose only job is to redirect to a full Google Maps URL. These are the
# ONLY hosts that ever trigger a network request; every other supported host
# is parsed offline.
SHORT_LINK_HOSTS = frozenset({"maps.app.goo.gl", "goo.gl", "g.co"})

_GOOGLE_HOST_RE = re.compile(
    r"^(?:www\.|maps\.)?google\.(?:com|[a-z]{2,3})(?:\.[a-z]{2})?$", re.IGNORECASE
)
_APPLE_HOSTS = frozenset({"maps.apple.com"})
_OSM_HOSTS = frozenset({"openstreetmap.org", "www.openstreetmap.org", "osm.org", "www.osm.org"})

_NUM = r"-?\d+(?:\.\d+)?"
# "@37.79,-122.40,17z" / "@37.79,-122.40,1234m" — viewport centre + zoom (z)
# or altitude in meters (m, which is not a zoom level).
_AT_RE = re.compile(rf"/@({_NUM}),({_NUM})(?:,({_NUM})([zm]))?")
# "!3d37.79!4d-122.40" inside the data= blob: the pin itself, preferred over
# the viewport centre when both exist.
_PIN_RE = re.compile(rf"!3d({_NUM})!4d({_NUM})")
_LATLON_RE = re.compile(rf"^\s*(?:loc:)?({_NUM})\s*,\s*({_NUM})\s*$", re.IGNORECASE)
_OSM_MAP_RE = re.compile(rf"map=(\d+(?:\.\d+)?)/({_NUM})/({_NUM})")
_GEO_RE = re.compile(rf"^geo:({_NUM}),({_NUM})(?:,{_NUM})?(?:;[^?]*)?(?:\?(.*))?$", re.IGNORECASE)
_OSM_ELEMENT_RE = re.compile(r"^/(node|way|relation)/(\d+)", re.IGNORECASE)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 placeroot"
)


# --- URL normalization -------------------------------------------------------


def normalize_url(raw: object) -> str | None:
    """Trim a pasted link into something `urllib.parse` will read.

    Strips whitespace, a surrounding `<...>` (chat/markdown autolink wrapper)
    and trailing sentence punctuation, and assumes https:// when no scheme was
    given ("maps.app.goo.gl/abc"). Returns None for a non-string or empty input.
    """
    if not isinstance(raw, str):
        return None
    url = raw.strip().rstrip(".,);")
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1].strip().rstrip(".,);")
    if not url:
        return None
    if url.lower().startswith("geo:"):
        return url
    if "://" not in url:
        url = "https://" + url
    return url


def provider_for(url: str) -> str | None:
    """Which supported provider a normalized URL belongs to, or None."""
    if url.lower().startswith("geo:"):
        return "geo"
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in SHORT_LINK_HOSTS or _GOOGLE_HOST_RE.match(host):
        return "google"
    if host in _APPLE_HOSTS:
        return "apple"
    if host in _OSM_HOSTS:
        return "osm"
    return None


def is_short_link(url: str) -> bool:
    """True for the redirect-only hosts (maps.app.goo.gl, goo.gl/maps..., g.co)."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in SHORT_LINK_HOSTS:
        return False
    if host == "goo.gl":
        return parts.path.startswith("/maps")
    return True


# --- Offline parsing -------------------------------------------------------------


def _num(text: str) -> int | float:
    """Zoom as the URL gave it: an int when it was one, else a float."""
    return int(text) if re.fullmatch(r"-?\d+", text) else float(text)


def _first(query: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        vals = query.get(key)
        if vals and vals[0].strip():
            return vals[0].strip()
    return None


def _label(text: str | None) -> str | None:
    """URL-decode a place/search name; '+' is a space in map URLs."""
    if not text:
        return None
    label = urllib.parse.unquote_plus(text).strip()
    return label or None


def _validated(lat: float, lon: float, provider: str, **extra: object) -> dict:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return {
            "error": "no_location",
            "detail": (
                f"coordinates {lat},{lon} are out of range; lat must be in "
                "[-90, 90] and lon in [-180, 180]"
            ),
            "provider": provider,
            **{k: v for k, v in extra.items() if v is not None and k == "label"},
        }
    row: dict = {"lat": lat, "lon": lon}
    for key, val in extra.items():
        if val is not None:
            row[key] = val
    row["provider"] = provider
    return row


def _no_location(provider: str, detail: str, label: str | None = None) -> dict:
    out: dict = {"error": "no_location", "detail": detail, "provider": provider}
    if label:
        out["label"] = label
    return out


def _parse_google(url: str, parts: urllib.parse.SplitResult) -> dict:
    path = parts.path
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    label: str | None = None
    note: str | None = None
    zoom: int | float | None = None

    segments = [s for s in path.split("/") if s]
    # /maps/place/<name>/@... , /maps/search/<text>/@... , /maps/dir/<a>/<b>/@...
    if len(segments) >= 3 and segments[0] == "maps":
        kind = segments[1]
        if kind in ("place", "search") and not segments[2].startswith("@"):
            label = _label(segments[2])
        elif kind == "dir":
            note = "route viewport centre"

    # The pin (!3d!4d) beats the viewport centre (@lat,lon) when both exist.
    pin = _PIN_RE.search(url)
    at = _AT_RE.search(path) or _AT_RE.search(url)
    if at and at.group(3) and at.group(4).lower() == "z":
        zoom = _num(at.group(3))
    z_param = _first(query, "z")
    if z_param and re.fullmatch(_NUM, z_param):
        zoom = _num(z_param)

    if pin:
        return _validated(
            float(pin.group(1)),
            float(pin.group(2)),
            "google",
            zoom=zoom,
            label=label,
            note=note,
        )
    if at:
        return _validated(
            float(at.group(1)),
            float(at.group(2)),
            "google",
            zoom=zoom,
            label=label,
            note=note,
        )

    # ?q=lat,lon / ?ll=lat,lon / ?query=lat,lon (Maps URLs API); ?q=<text>
    for key in ("ll", "q", "query"):
        val = _first(query, key)
        if not val:
            continue
        m = _LATLON_RE.match(val)
        if m:
            return _validated(
                float(m.group(1)),
                float(m.group(2)),
                "google",
                zoom=zoom,
                label=label,
                note=note,
            )
        if key in ("q", "query") and label is None:
            label = _label(val)

    if label:
        return _no_location("google", "the link names a place but carries no coordinate", label)
    if _first(query, "cid"):
        return _no_location(
            "google",
            "a ?cid= Google place id carries no coordinate and cannot be "
            "resolved without Google's API",
        )
    return _no_location("google", "no coordinate or place name found in the Google Maps link")


def _parse_apple(url: str, parts: urllib.parse.SplitResult) -> dict:
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    label = _label(_first(query, "q", "name", "address"))
    zoom: int | float | None = None
    z_param = _first(query, "z")
    if z_param and re.fullmatch(_NUM, z_param):
        zoom = _num(z_param)

    # ll= is the pin; coordinate= is the newer /place?coordinate= form;
    # sll= is the search centre, a fallback only.
    for key in ("ll", "coordinate", "sll"):
        val = _first(query, key)
        if not val:
            continue
        m = _LATLON_RE.match(val)
        if m:
            return _validated(float(m.group(1)), float(m.group(2)), "apple", zoom=zoom, label=label)
    if label:
        return _no_location("apple", "the link names a place but carries no coordinate", label)
    return _no_location("apple", "no coordinate or place name found in the Apple Maps link")


def _parse_osm(url: str, parts: urllib.parse.SplitResult) -> dict:
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    zoom: int | float | None = None
    map_m = _OSM_MAP_RE.search(parts.fragment) or _OSM_MAP_RE.search(parts.query)
    if map_m:
        zoom = _num(map_m.group(1))
    z_param = _first(query, "zoom")
    if z_param and re.fullmatch(_NUM, z_param):
        zoom = _num(z_param)

    # A marker (?mlat=&mlon=) is the pin; #map= is only the viewport.
    mlat, mlon = _first(query, "mlat"), _first(query, "mlon")
    if mlat and mlon and re.fullmatch(_NUM, mlat) and re.fullmatch(_NUM, mlon):
        return _validated(float(mlat), float(mlon), "osm", zoom=zoom)
    if map_m:
        return _validated(float(map_m.group(2)), float(map_m.group(3)), "osm", zoom=zoom)
    lat, lon = _first(query, "lat"), _first(query, "lon")
    if lat and lon and re.fullmatch(_NUM, lat) and re.fullmatch(_NUM, lon):
        return _validated(float(lat), float(lon), "osm", zoom=zoom)

    el = _OSM_ELEMENT_RE.match(parts.path)
    if el:
        return _no_location(
            "osm",
            f"an OpenStreetMap {el.group(1).lower()}/{el.group(2)} link carries no "
            "coordinate; open it and copy the #map= link instead",
        )
    return _no_location("osm", "no coordinate found in the OpenStreetMap link")


def _parse_geo(url: str) -> dict:
    m = _GEO_RE.match(url.strip())
    if not m:
        return _no_location("geo", "malformed geo: URI; expected geo:<lat>,<lon>[?z=<zoom>]")
    zoom: int | float | None = None
    if m.group(3):
        z_param = _first(urllib.parse.parse_qs(m.group(3), keep_blank_values=True), "z")
        if z_param and re.fullmatch(_NUM, z_param):
            zoom = _num(z_param)
    return _validated(float(m.group(1)), float(m.group(2)), "geo", zoom=zoom)


def parse_map_url(url: object) -> dict:
    """Offline parse of one map link. Never raises; never touches the network.

    A short link (maps.app.goo.gl, ...) parses to no_location here — use
    `resolve` to follow it first.
    """
    normalized = normalize_url(url)
    if normalized is None:
        return {"error": "bad_request", "detail": "url must be a non-empty string"}
    provider = provider_for(normalized)
    if provider is None:
        return {
            "error": "unsupported_url",
            "detail": (
                f"{normalized!r} is not a Google Maps, Apple Maps, OpenStreetMap or geo: link"
            ),
            "supported": list(SUPPORTED_PROVIDERS),
        }
    if provider == "geo":
        return _parse_geo(normalized)
    parts = urllib.parse.urlsplit(normalized)
    if provider == "google":
        if is_short_link(normalized):
            return _no_location("google", "short link not expanded; follow its redirect first")
        return _parse_google(normalized, parts)
    if provider == "apple":
        return _parse_apple(normalized, parts)
    return _parse_osm(normalized, parts)


# --- Redirect following (short links only) ---------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow, so every 3xx surfaces as an HTTPError we read ourselves."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

Fetch = Callable[[str, float], tuple[int, str | None]]


def _fetch(url: str, timeout_s: float) -> tuple[int, str | None]:
    """One GET without following redirects -> (status, Location header or None).

    A GET rather than HEAD: Google's short-link service does not always
    honor HEAD. Only HTTPError for a 3xx is a normal outcome here; anything
    else propagates to follow_redirects, which turns it into an error dict.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:  # noqa: S310
            return resp.status, resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return e.code, e.headers.get("Location")
        raise


def follow_redirects(
    url: str,
    *,
    timeout_s: float = 5.0,
    max_hops: int = 5,
    fetch: Fetch | None = None,
) -> dict:
    """Expand a short link hop by hop -> {"final_url", "hops"} or {"error": ...}.

    Stops at the first response that is not a redirect, or as soon as the
    chain leaves the short-link hosts (so the final Google Maps page is never
    actually downloaded — only redirect-only hosts are ever fetched). `fetch(url, timeout_s)`
    returns (status, location) and defaults to a urllib GET; tests pass a
    scripted fake. Never raises.
    """
    do_fetch = fetch or _fetch
    current = url
    for hop in range(max_hops):
        if not is_short_link(current):
            # Only the redirect-only hosts are ever fetched; the moment the
            # chain lands on a real map page (or anything else) we stop and
            # let the offline parser judge it.
            return {"final_url": current, "hops": hop}
        try:
            status, location = do_fetch(current, timeout_s)
        except urllib.error.HTTPError as e:
            return {"error": "redirect_failed", "detail": str(e), "status": e.code}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"error": "redirect_failed", "detail": str(e)}
        if 300 <= status < 400 and location:
            current = urllib.parse.urljoin(current, location)
            continue
        if 200 <= status < 300:
            return {"final_url": current, "hops": hop + 1}
        return {
            "error": "redirect_failed",
            "detail": f"HTTP {status} while expanding the short link",
            "status": status,
        }
    return {"error": "redirect_failed", "detail": "too many redirects", "hops": max_hops}


# --- Orchestration ---------------------------------------------------------------


def resolve(
    url: object,
    *,
    timeout_s: float = 5.0,
    max_hops: int = 5,
    fetch: Fetch | None = None,
) -> dict:
    """Parse a pasted link, expanding a short link first when needed.

    Adds `resolved_via` ("url" | "redirect") and, after a redirect,
    `final_url`. A name-only link comes back as {"error": "no_location",
    "label": ...} for the caller to resolve by name. Never raises.
    """
    normalized = normalize_url(url)
    if normalized is None:
        return {"error": "bad_request", "detail": "url must be a non-empty string"}
    if provider_for(normalized) is None:
        return parse_map_url(normalized)

    if not is_short_link(normalized):
        result = parse_map_url(normalized)
        if "error" not in result:
            result["resolved_via"] = "url"
        return result

    expanded = follow_redirects(normalized, timeout_s=timeout_s, max_hops=max_hops, fetch=fetch)
    if "error" in expanded:
        expanded.setdefault("provider", "google")
        return expanded
    final_url = expanded["final_url"]
    if is_short_link(final_url):
        return {
            "error": "redirect_failed",
            "detail": "the short link did not redirect to a map page",
            "provider": "google",
            "final_url": final_url,
        }
    result = parse_map_url(final_url)
    result["final_url"] = final_url
    if "error" not in result:
        result["resolved_via"] = "redirect"
    return result
