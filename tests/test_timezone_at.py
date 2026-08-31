"""timezone_at (issue #437): IANA timezone and local time at a point.

Fully offline (tzfpy for point->tzid, stdlib zoneinfo for offset/DST/local
time), so no fixture/monkeypatch of network calls is needed here, unlike
elevation_at (test_elevation_at.py) which fakes an HTTP range fetcher.
DST correctness is exercised via timezone.timezone_at's internal-only `at`
parameter (an injectable "now"), which the public server tool never
exposes — see timezone.py's docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from placeroot import server, timezone

# ---------------------------------------------------------------------------
# tzid correctness
# ---------------------------------------------------------------------------


def test_san_francisco_tzid():
    result = timezone.timezone_at(37.7749, -122.4194)
    assert result["tzid"] == "America/Los_Angeles"
    assert "error" not in result


def test_new_delhi_tzid_and_fixed_half_hour_offset():
    # India Standard Time is a fixed +05:30 offset with no DST -- a good
    # correctness check independent of any particular "now".
    result = timezone.timezone_at(28.6139, 77.2090)
    assert result["tzid"] == "Asia/Kolkata"
    assert result["utc_offset"] == "+05:30"
    assert result["dst_active"] is False


def test_berlin_tzid():
    result = timezone.timezone_at(52.5200, 13.4050)
    assert result["tzid"] == "Europe/Berlin"


# ---------------------------------------------------------------------------
# DST correctness (fixed injected "now")
# ---------------------------------------------------------------------------


def test_berlin_summer_is_cest_plus_two():
    at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    result = timezone.timezone_at(52.5200, 13.4050, at=at)
    assert result["tzid"] == "Europe/Berlin"
    assert result["utc_offset"] == "+02:00"
    assert result["dst_active"] is True
    assert result["abbreviation"] == "CEST"
    assert result["local_time"] == "2026-07-01T14:00:00+02:00"


def test_berlin_winter_is_cet_plus_one():
    at = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    result = timezone.timezone_at(52.5200, 13.4050, at=at)
    assert result["tzid"] == "Europe/Berlin"
    assert result["utc_offset"] == "+01:00"
    assert result["dst_active"] is False
    assert result["abbreviation"] == "CET"
    assert result["local_time"] == "2026-01-15T13:00:00+01:00"


# ---------------------------------------------------------------------------
# Ocean / no-zone points
# ---------------------------------------------------------------------------


def test_mid_pacific_ocean_point_never_raises():
    """Either an Etc/GMT nautical zone or a null+note answer -- never a crash."""
    result = timezone.timezone_at(0.0, -150.0)
    if result["tzid"] is None:
        assert "note" in result
    else:
        assert result["tzid"].startswith("Etc/GMT")
        assert result["dst_active"] is False
        assert "utc_offset" in result
        assert "local_time" in result


def test_empty_tzid_answers_null_not_error(monkeypatch):
    """Simulate a tzfpy release that returns '' for an unresolvable point."""
    monkeypatch.setattr(timezone, "get_tz", lambda lon, lat: "")
    result = timezone.timezone_at(0.0, -150.0)
    assert result == {"tzid": None, "note": timezone._NO_ZONE_NOTE}


# ---------------------------------------------------------------------------
# Server-tool level
# ---------------------------------------------------------------------------


def test_server_tool_out_of_range_coords_bad_request():
    result = server.timezone_at(lat=95.0, lon=6.0)
    assert result["error"] == "bad_request"


def test_server_tool_swapped_lat_lon_bad_request():
    result = server.timezone_at(lat=6.0, lon=200.0)
    assert result["error"] == "bad_request"


def test_server_tool_returns_timezone():
    result = server.timezone_at(lat=37.7749, lon=-122.4194)
    assert "error" not in result
    assert result["tzid"] == "America/Los_Angeles"
    assert result["abbreviation"] in ("PST", "PDT")


def test_server_tool_does_not_expose_at_parameter():
    with pytest.raises(TypeError):
        server.timezone_at(lat=0.0, lon=0.0, at=None)
