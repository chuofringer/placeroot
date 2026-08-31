"""IANA timezone and local time at a point (issue #437).

Point -> tzid lookup is `tzfpy.get_tz` (note: lon, lat — the reverse of this
module's own lat/lon argument order), a small Rust-backed package built on
timezone-boundary-builder's polygons, itself derived from the IANA tzdb.
Fully offline: no network call, no external service, same open-data-only
posture as every other tool here.

tzfpy covers the whole globe, including open ocean, by falling back to the
15-degree-wide nautical "Etc/GMT+N" zones where no real tzdb zone applies
(sign inverted from common usage: Etc/GMT+10 is UTC-10). Those are real
zoneinfo entries with a fixed offset and no DST, so they flow through the
same stdlib zoneinfo path as any named zone below. Some tzfpy releases can
still yield an empty string for a genuinely unresolvable point; that is
handled as a null-tzid answer rather than an error, mirroring
elevation_at's null-coverage note (elevation.py).

Offset/DST/local-time/abbreviation all come from stdlib `zoneinfo` +
`datetime.now(tz)` (or an injected `at`, used only by tests) — no second
timezone database to keep in sync with tzfpy's.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tzfpy import get_tz

_NO_ZONE_NOTE = "no timezone boundary resolved at this point"


def timezone_at(lat: float, lon: float, at: datetime | None = None) -> dict:
    """Timezone and local time at (lat, lon).

    Caller is responsible for range-validating lat/lon first (see
    server._invalid_coord); this function assumes valid coordinates.

    `at` is an internal testing hook (an aware or naive datetime to treat
    as "now"); the public tool always resolves the current instant, so it
    never exposes this parameter.

    Returns {"tzid", "utc_offset", "dst_active", "local_time",
    "abbreviation"} on success. A point with no resolvable zone (some tzfpy
    releases can return one for a handful of edge points despite the
    Etc/GMT ocean grid) answers {"tzid": None, "note": "..."} instead of
    raising.
    """
    tzid = get_tz(lon, lat)
    if not tzid:
        return {"tzid": None, "note": _NO_ZONE_NOTE}

    zone = ZoneInfo(tzid)
    now = at if at is not None else datetime.now(zone)
    local = now.astimezone(zone)

    offset = local.utcoffset()
    dst = local.dst()
    total_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)

    return {
        "tzid": tzid,
        "utc_offset": f"{sign}{hh:02d}:{mm:02d}",
        "dst_active": bool(dst),
        "local_time": local.isoformat(timespec="seconds"),
        "abbreviation": local.tzname(),
    }
