"""Local persistent preferences (issue #315)."""

import json
import threading

import pytest

from placeroot import preferences, resources, server, session


def test_missing_file_is_an_empty_document():
    assert preferences.load() == {
        "mode": None,
        "pace": None,
        "household": [],
        "note": None,
        "lang": None,
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
        "lang": None,
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
        return {
            "distance_m": 1,
            "duration_s": 1,
            "mode": kwargs.get("mode") or "drive",
            "from": {"lat": 37.0, "lon": -122.0},
            "to": {"lat": 37.1, "lon": -122.1},
        }

    monkeypatch.setattr(server.routing, "route", fake)
    server.route(from_lat=37.0, from_lon=-122.0, to_lat=37.1, to_lon=-122.1, confirm=True)
    assert seen["mode"] == "cycle"



def test_torn_json_is_not_wiped(tmp_path, monkeypatch):
    dest = tmp_path / "prefs.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    dest.write_text("{not json", encoding="utf-8")
    result = server.preferences(mode="cycle")
    assert result["error"] == "corrupt"
    assert dest.read_text(encoding="utf-8") == "{not json"
    assert server.preferences()["error"] == "corrupt"


def test_clear_with_other_fields_is_rejected():
    result = server.preferences(clear=True, mode="walk")
    assert result["error"] == "bad_request"
    assert "clear" in result["detail"]


def test_save_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    dest = tmp_path / "prefs.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    preferences.update(mode="walk")
    assert dest.is_file()
    assert not dest.with_name(dest.name + ".tmp").exists()


def test_concurrent_updates_keep_both_fields(tmp_path, monkeypatch):
    dest = tmp_path / "prefs.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    errors: list[BaseException] = []

    def set_mode():
        try:
            preferences.update(mode="cycle")
        except BaseException as exc:
            errors.append(exc)

    def set_house():
        try:
            preferences.update(household=["dog"])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=set_mode), threading.Thread(target=set_house)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    doc = preferences.load()
    assert doc["mode"] == "cycle"
    assert doc["household"] == ["dog"]


def test_resolve_mode_falls_back_when_file_is_corrupt(tmp_path, monkeypatch):
    dest = tmp_path / "prefs.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    dest.write_text("{", encoding="utf-8")
    assert preferences.resolve_mode(None, "drive") == "drive"


def test_io_error_is_structured(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    dest = blocker / "prefs.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    result = server.preferences(mode="walk")
    assert result["error"] == "io_error"


# --- per-session overlay over --http (session.py) --------------------------


def test_http_sessions_overlay_the_file_and_do_not_see_each_other(tmp_path, monkeypatch):
    """Over HTTP one process serves every client: a stored mode must be
    this session's, layered over the host file, never another session's."""
    dest = tmp_path / "prefs.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    preferences.clear_overlays()
    preferences.update(mode="walk", note="host")  # the file, as stdio would write it
    on_disk = json.loads(dest.read_text())

    with session.bind_session("client-a"):
        doc = server.preferences(mode="cycle", household=["dog"])
        assert doc["mode"] == "cycle"
        assert doc["household"] == ["dog"]
        assert doc["note"] == "host"  # file shows through where the overlay is silent
        assert preferences.get("mode") == "cycle"
        assert preferences.resolve_mode(None, "drive") == "cycle"
    with session.bind_session("client-b"):
        assert server.preferences()["mode"] == "walk"
        assert server.preferences()["household"] == []
        assert preferences.get("mode") == "walk"
        assert preferences.resolve_mode(None, "drive") == "walk"
    # Nothing an HTTP session wrote reached the host file, and stdio is the file.
    assert json.loads(dest.read_text()) == on_disk
    assert preferences.load()["mode"] == "walk"
    preferences.clear_overlays()


def test_http_clear_empties_the_session_view_but_keeps_the_file(tmp_path, monkeypatch):
    dest = tmp_path / "prefs.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))
    preferences.clear_overlays()
    preferences.update(mode="walk", lang="de")
    with session.bind_session("client-a"):
        assert server.preferences(clear=True) == preferences.empty()
        assert server.preferences() == preferences.empty()
        assert preferences.get("lang") is None
        assert preferences.resolve_lang(None) is None
        # A later update on the cleared view sets just that field.
        assert server.preferences(mode="drive")["mode"] == "drive"
        assert preferences.get("lang") is None
    assert dest.is_file()
    assert preferences.load() == {"mode": "walk", "pace": None, "household": [],
                                  "note": None, "lang": "de"}
    preferences.clear_overlays()


def test_http_overlay_normalizes_like_a_save():
    preferences.clear_overlays()
    with session.bind_session("client-a"):
        doc = preferences.update(mode=" CYCLE ", household=["dog", " dog ", ""], lang=" DE ")
        assert doc["mode"] == "cycle"
        assert doc["household"] == ["dog"]
        assert doc["lang"] == "de"
    preferences.clear_overlays()


def _http_client_class():
    try:
        from mcp.client.client import Client
    except ImportError:
        pytest.skip("mcp.client streamable-HTTP client not available in this SDK build")
    return Client


async def _read_mode(client):
    result = await client.call_tool("preferences", {})
    assert result.is_error is False
    return json.loads(result.content[0].text)["mode"]


def test_two_http_clients_get_separate_preferences(running_http_server):
    """End to end over the real transport with the initialize handshake
    (mode="legacy"): the SDK's Mcp-Session-Id is the session, so client
    A's stored mode is neither client B's default nor the host file's."""
    Client = _http_client_class()
    import anyio

    preferences.clear_overlays()

    async def scenario():
        async with (
            Client(running_http_server, mode="legacy") as a,
            Client(running_http_server, mode="legacy") as b,
        ):
            set_a = await a.call_tool("preferences", {"mode": "cycle"})
            assert json.loads(set_a.content[0].text)["mode"] == "cycle"
            assert await _read_mode(a) == "cycle"
            assert await _read_mode(b) is None
            await b.call_tool("preferences", {"mode": "walk"})
            assert await _read_mode(a) == "cycle"
            assert await _read_mode(b) == "walk"

    anyio.run(scenario)
    # stdio (this process, unbound) still reads the untouched file.
    assert preferences.load()["mode"] is None
    preferences.clear_overlays()


def test_sessionless_http_client_never_sees_another_request(running_http_server):
    """The 2026-07-28 era (the client's default) has no handshake and no
    session: the SDK serves every request on a fresh connection, so a
    stored preference lives for that one call and no other request —
    this client's or another's — can read it back."""
    Client = _http_client_class()
    import anyio

    preferences.clear_overlays()

    async def scenario():
        async with Client(running_http_server) as a, Client(running_http_server) as b:
            set_a = await a.call_tool("preferences", {"mode": "cycle"})
            assert json.loads(set_a.content[0].text)["mode"] == "cycle"
            assert await _read_mode(a) is None
            assert await _read_mode(b) is None

    anyio.run(scenario)
    assert not preferences._overlays
    assert preferences.load()["mode"] is None
