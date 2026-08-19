"""Pocket handoff: maps deep links, GPX, and a printable stop list (#312).

String formatting only — no geocoding, no Maps API, no extra network.
Deep links carry the caller-chosen coordinates; the server never sends
them anywhere. Attached as `export` on successful `route` /
`optimize_route` responses so a Saturday plan leaves the chat as
something the user can open, not six addresses to retype.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as xml_escape

# Official Google Maps URLs directions scheme:
# https://developers.google.com/maps/documentation/urls/get-started
_GOOGLE_DIR = "https://www.google.com/maps/dir/"
_GOOGLE_TRAVELMODE = {
    "walk": "walking",
    "cycle": "bicycling",
    "drive": "driving",
}

# Apple Maps URL scheme (https://maps.apple.com). dirflg is documented for
# drive/walk/transit; Apple has no bicycle flag, so cycle omits it.
_APPLE_MAPS = "https://maps.apple.com/"
_APPLE_DIRFLG = {
    "walk": "w",
    "drive": "d",
}

_GPX_NS = "http://www.topografix.com/GPX/1/1"

# 6 dp is ~0.1 m — enough to pin a doorway, short enough to read.
_TEXT_COORD_DP = 6


def from_route_result(result: Mapping[str, object]) -> dict[str, object]:
    """Build the export object for a successful two-point `route` result."""
    origin_pt = result["from"]
    dest_pt = result["to"]
    origin = _point(origin_pt)
    dest = _point(dest_pt)
    stops = (
        (origin[0], origin[1], _endpoint_name(origin_pt, "Start")),
        (dest[0], dest[1], _endpoint_name(dest_pt, "End")),
    )
    distance_m = _as_float(result.get("distance_m"))
    duration_s = _as_float(result.get("duration_s"))
    legs = ()
    if distance_m is not None or duration_s is not None:
        legs = (
            {
                "distance_m": distance_m,
                "duration_s": duration_s,
            },
        )
    return build(
        stops,
        mode=_as_str(result.get("mode")) or "drive",
        legs=legs,
        path=_path_latlon(result.get("path")),
        total_distance_m=distance_m,
        total_duration_s=duration_s,
    )


def from_optimize_result(
    stops: Sequence[Mapping[str, object]],
    result: Mapping[str, object],
) -> dict[str, object]:
    """Build the export object for a successful `optimize_route` result.

    `stops` is the caller's original list (lat/lon plus optional name),
    indexed the same way `result["order"]` refers to it. Names ride through
    into the text list and GPX waypoints; they never go into a maps URL.
    """
    parsed = [_named_stop(stop, idx) for idx, stop in enumerate(stops)]
    order = result.get("order")
    if not isinstance(order, Sequence) or not order:
        ordered = parsed
    else:
        ordered = [parsed[int(i)] for i in order]
    legs = result.get("legs")
    leg_rows: tuple[Mapping[str, object], ...]
    if isinstance(legs, Sequence) and not isinstance(legs, (str, bytes)):
        leg_rows = tuple(leg for leg in legs if isinstance(leg, Mapping))
    else:
        leg_rows = ()
    return build(
        ordered,
        mode=_as_str(result.get("mode")) or "drive",
        legs=leg_rows,
        roundtrip=bool(result.get("roundtrip")),
        total_distance_m=_as_float(result.get("total_distance_m")),
        total_duration_s=_as_float(result.get("total_duration_s")),
    )


def build(
    stops: Sequence[tuple[float, float, str | None]],
    *,
    mode: str = "drive",
    legs: Sequence[Mapping[str, object]] = (),
    roundtrip: bool = False,
    path: Sequence[tuple[float, float]] = (),
    total_distance_m: float | None = None,
    total_duration_s: float | None = None,
) -> dict[str, object]:
    """Format maps links, GPX 1.1, and a printable stop list.

    `stops` is the visiting order: (lat, lon, name). A roundtrip repeats
    the first stop as the maps/GPX destination so the phone loop closes;
    the printable list names that return explicitly. `path`, when present,
    is extra track geometry (lat, lon) — typically a route() LineString.
    Missing geometry is fine: the links and GPX still connect the stops.
    """
    if len(stops) < 2:
        raise ValueError("export needs at least two stops")
    nav = list(stops)
    if roundtrip:
        nav.append(stops[0])
    return {
        "maps_link": {
            "google": google_maps_url(nav, mode),
            "apple": apple_maps_url(nav, mode),
        },
        "gpx": gpx_document(stops, nav, mode=mode, path=path, roundtrip=roundtrip),
        "text": text_list(
            stops,
            legs=legs,
            mode=mode,
            roundtrip=roundtrip,
            total_distance_m=total_distance_m,
            total_duration_s=total_duration_s,
        ),
    }


def google_maps_url(stops: Sequence[tuple[float, float, str | None]], mode: str) -> str:
    """Google Maps directions URL (api=1) for an ordered stop list.

    Intermediate stops become `waypoints`. Desktop and the Google Maps
    app honor up to 9; mobile browsers often honor only 3 and silently
    drop the rest. The printable list and GPX keep every stop.
    """
    origin = _ll(stops[0][0], stops[0][1])
    dest = _ll(stops[-1][0], stops[-1][1])
    params: list[tuple[str, str]] = [
        ("api", "1"),
        ("origin", origin),
        ("destination", dest),
        ("travelmode", _GOOGLE_TRAVELMODE.get(mode, "driving")),
    ]
    via = stops[1:-1]
    if via:
        params.append(("waypoints", "|".join(_ll(lat, lon) for lat, lon, _ in via)))
    return _GOOGLE_DIR + "?" + urlencode(params, safe=",|")


def apple_maps_url(stops: Sequence[tuple[float, float, str | None]], mode: str) -> str:
    """Apple Maps directions URL for an ordered stop list.

    Two stops use the documented saddr/daddr pair. Extra stops are
    appended with the undocumented `+to:` daddr chain (legacy Google
    syntax). Current Apple Maps often opens only the first destination,
    so the printable list and GPX carry the full itinerary; the Apple
    link is a best-effort extra for 3+ stops.
    """
    saddr = quote(_ll(stops[0][0], stops[0][1]), safe=",")
    # Quote each lat,lon on its own (comma stays literal). Join extras
    # with a raw `+to:` so the two-stop URL never contains `+`, and the
    # multi-stop chain keeps the legacy separator older parsers expect.
    dests = [quote(_ll(lat, lon), safe=",") for lat, lon, _ in stops[1:]]
    daddr = "+to:".join(dests)
    query = f"saddr={saddr}&daddr={daddr}"
    dirflg = _APPLE_DIRFLG.get(mode)
    if dirflg is not None:
        query += f"&dirflg={dirflg}"
    return _APPLE_MAPS + "?" + query


def gpx_document(
    waypoints: Sequence[tuple[float, float, str | None]],
    route_stops: Sequence[tuple[float, float, str | None]],
    *,
    mode: str,
    path: Sequence[tuple[float, float]] = (),
    roundtrip: bool = False,
) -> str:
    """GPX 1.1 document: waypoints for each stop, a route, and an optional track."""
    title = _gpx_title(mode, len(waypoints), roundtrip)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<gpx version="1.1" creator="placeroot" xmlns="{_GPX_NS}">',
    ]
    for idx, (lat, lon, name) in enumerate(waypoints, start=1):
        parts.append(_gpx_point("wpt", lat, lon, name or f"Stop {idx}"))
    parts.append("<rte>")
    parts.append(f"<name>{xml_escape(title)}</name>")
    for idx, (lat, lon, name) in enumerate(route_stops, start=1):
        label = name or f"Stop {idx}"
        if roundtrip and idx == len(route_stops) and name:
            label = f"{name} (return)"
        parts.append(_gpx_point("rtept", lat, lon, label))
    parts.append("</rte>")
    track = list(path) if path else [(lat, lon) for lat, lon, _ in route_stops]
    parts.append("<trk>")
    parts.append(f"<name>{xml_escape(title)}</name>")
    parts.append("<trkseg>")
    for lat, lon in track:
        parts.append(f'<trkpt lat="{_gpx_coord(lat)}" lon="{_gpx_coord(lon)}"></trkpt>')
    parts.append("</trkseg>")
    parts.append("</trk>")
    parts.append("</gpx>")
    return "\n".join(parts) + "\n"


def text_list(
    stops: Sequence[tuple[float, float, str | None]],
    *,
    legs: Sequence[Mapping[str, object]] = (),
    mode: str,
    roundtrip: bool = False,
    total_distance_m: float | None = None,
    total_duration_s: float | None = None,
) -> str:
    """Printable itinerary: names, coordinates, and per-leg times when present.

    Legs flagged `"estimated": true` (straight-line guesses from
    optimize_route) are marked `(estimated)` so they are not read as
    routed distances.
    """
    lines = [_text_header(mode, len(stops), total_distance_m, total_duration_s, roundtrip)]
    lines.append("")
    for idx, (lat, lon, name) in enumerate(stops, start=1):
        label = name or f"Stop {idx}"
        lines.append(f"{idx}. {label}")
        lines.append(f"   {_text_coord(lat)}, {_text_coord(lon)}")
        if idx >= 2:
            incoming = legs[idx - 2] if idx - 2 < len(legs) else None
            leg_line = _text_leg(incoming)
            if leg_line:
                lines.append(f"   {leg_line}")
    if roundtrip:
        lat, lon, name = stops[0]
        label = name or "Stop 1"
        lines.append(f"{len(stops) + 1}. {label} (return)")
        lines.append(f"   {_text_coord(lat)}, {_text_coord(lon)}")
        closing = legs[len(stops) - 1] if len(legs) >= len(stops) else None
        leg_line = _text_leg(closing)
        if leg_line:
            lines.append(f"   {leg_line}")
    if total_distance_m is not None or total_duration_s is not None:
        lines.append("")
        lines.append(f"Total: {_text_totals(total_distance_m, total_duration_s)}")
    return "\n".join(lines) + "\n"


def _named_stop(stop: Mapping[str, object], idx: int) -> tuple[float, float, str | None]:
    lat, lon = _point(stop)
    raw = stop.get("name")
    name: str | None
    if isinstance(raw, str) and raw.strip():
        name = raw.strip()
    else:
        name = f"Stop {idx + 1}"
    return (lat, lon, name)


def _endpoint_name(value: object, fallback: str) -> str:
    if isinstance(value, Mapping):
        raw = value.get("name")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return fallback



def _point(value: object) -> tuple[float, float]:
    if not isinstance(value, Mapping):
        raise TypeError("stop must be a mapping with lat and lon")
    return (float(value["lat"]), float(value["lon"]))


def _path_latlon(path: object) -> tuple[tuple[float, float], ...]:
    """GeoJSON LineString coordinates are (lon, lat); export wants (lat, lon)."""
    if not isinstance(path, Mapping) or path.get("type") != "LineString":
        return ()
    coords = path.get("coordinates")
    if not isinstance(coords, Sequence) or isinstance(coords, (str, bytes)):
        return ()
    out: list[tuple[float, float]] = []
    for pair in coords:
        if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) < 2:
            continue
        lon, lat = pair[0], pair[1]
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            out.append((float(lat), float(lon)))
    return tuple(out)


def _ll(lat: float, lon: float) -> str:
    """`lat,lon` in fixed-point — `str(float)` can emit `5e-05`, which Maps reject."""
    return f"{_text_coord(lat)},{_text_coord(lon)}"


def _gpx_coord(value: float) -> str:
    return f"{value:.7f}"


def _gpx_point(tag: str, lat: float, lon: float, name: str) -> str:
    return (
        f'<{tag} lat="{_gpx_coord(lat)}" lon="{_gpx_coord(lon)}">'
        f"<name>{xml_escape(name)}</name>"
        f"</{tag}>"
    )


def _gpx_title(mode: str, n_stops: int, roundtrip: bool) -> str:
    kind = "roundtrip" if roundtrip else "route"
    return f"placeroot {mode} {kind} · {n_stops} stops"


def _text_header(
    mode: str,
    n_stops: int,
    total_distance_m: float | None,
    total_duration_s: float | None,
    roundtrip: bool,
) -> str:
    bits = [mode, f"{n_stops} stops"]
    if roundtrip:
        bits.append("roundtrip")
    totals = _text_totals(total_distance_m, total_duration_s)
    if totals:
        bits.append(totals)
    return " · ".join(bits)


def _text_coord(value: float) -> str:
    return f"{value:.{_TEXT_COORD_DP}f}"


def _text_leg(leg: Mapping[str, object] | None) -> str:
    if leg is None:
        return ""
    totals = _text_totals(_as_float(leg.get("distance_m")), _as_float(leg.get("duration_s")))
    if totals and leg.get("estimated") is True:
        return f"{totals} (estimated)"
    return totals


def _text_totals(distance_m: float | None, duration_s: float | None) -> str:
    parts: list[str] = []
    if distance_m is not None:
        parts.append(_fmt_distance(distance_m))
    if duration_s is not None:
        parts.append(_fmt_duration(duration_s))
    return ", ".join(parts)


def _fmt_distance(distance_m: float) -> str:
    # Distances stay in meters — same unit every other tool reports.
    if abs(distance_m - round(distance_m)) < 0.05:
        return f"{int(round(distance_m))} m"
    return f"{distance_m:.1f} m"


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min" if secs == 0 else f"{hours} h {minutes} min {secs} s"
    return f"{minutes} min" if secs == 0 else f"{minutes} min {secs} s"


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
