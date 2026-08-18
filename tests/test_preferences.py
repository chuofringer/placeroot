"""Local persistent preferences (issue #315)."""

import json

from placeroot import preferences, resources, server


def test_missing_file_is_an_empty_document():
    assert preferences.load() == {
        "mode": None,
        "pace": None,
        "household": [],
        "note": None,
    }


def test_update_round_trips_on_disk(tmp_path, monkeypatch):
    dest = tmp_path / "prefs.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    saved = preferences.update(
        mode="cycle", household=["dog"], note="I bike everywhere, I have a dog"
    )
    assert saved["mode"] == "cycle"
    assert saved["household"] == ["dog"]
    assert dest.is_file()
    on_disk = json.loads(dest.read_text())
    assert on_disk == saved
    assert preferences.load() == saved


def test_explicit_mode_wins_over_stored_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(tmp_path / "prefs.json"))
    preferences.update(mode="cycle")
    assert preferences.resolve_mode("drive", "walk") == "drive"
    assert preferences.resolve_mode(None, "walk") == "cycle"
    assert preferences.resolve_mode(None, "drive") == "cycle"


def test_missing_preference_falls_back_to_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(tmp_path / "prefs.json"))
    assert preferences.resolve_mode(None, "drive") == "drive"
    assert preferences.resolve_mode(None, "walk") == "walk"


def test_unsupported_mode_on_write_is_rejected():
    result = server.preferences(mode="hoverboard")
    assert result["error"] == "bad_request"
    assert "hoverboard" in result["detail"]
    assert preferences.load()["mode"] is None


def test_tool_read_matches_resource():
    assert server.preferences() == resources.preferences_payload()


def test_tool_merge_then_clear():
    updated = server.preferences(mode="cycle", household=["dog"])
    assert updated["mode"] == "cycle"
    assert server.preferences()["household"] == ["dog"]
    cleared = server.preferences(clear=True)
    assert cleared == {
        "mode": None,
        "pace": None,
        "household": [],
        "note": None,
    }
    assert server.preferences() == cleared


def test_nothing_is_written_outside_the_configured_path(tmp_path, monkeypatch):
    dest = tmp_path / "only-here.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    server.preferences(mode="walk")
    assert dest.is_file()
    # Default home path was not created by this write.
    assert not (tmp_path / "placeroot").exists()


def test_household_dedupes_and_strips():
    saved = preferences.update(household=[" dog ", "dog", "no_stairs", ""])
    assert saved["household"] == ["dog", "no_stairs"]



def test_isochrone_uses_stored_mode_when_omitted(monkeypatch):
    preferences.update(mode="cycle")
    seen: dict = {}

    def fake(lat, lon, minutes=15, mode="walk", speed_m_s=None, radius_m=None):
        seen["mode"] = mode
        return {"polygon": {}, "stats": {}}

    monkeypatch.setattr(server.routing, "isochrone", fake)
    server.isochrone(lat=37.0, lon=-122.0, minutes=5)
    assert seen["mode"] == "cycle"


def test_isochrone_explicit_mode_wins(monkeypatch):
    preferences.update(mode="cycle")
    seen: dict = {}

    def fake(lat, lon, minutes=15, mode="walk", speed_m_s=None, radius_m=None):
        seen["mode"] = mode
        return {"polygon": {}, "stats": {}}

    monkeypatch.setattr(server.routing, "isochrone", fake)
    server.isochrone(lat=37.0, lon=-122.0, minutes=5, mode="walk")
    assert seen["mode"] == "walk"


def test_route_uses_stored_mode_when_omitted(monkeypatch):
    preferences.update(mode="cycle")
    seen: dict = {}

    def fake(*args, **kwargs):
        seen["mode"] = kwargs.get("mode")
        return {"distance_m": 1, "duration_s": 1}

    monkeypatch.setattr(server.routing, "route", fake)
    server.route(from_lat=37.0, from_lon=-122.0, to_lat=37.1, to_lon=-122.1)
    assert seen["mode"] == "cycle"
