"""admin_lookup confirm gate (#336).

Same needs_confirm protocol as route: peek cheaply, ask before a cold
>15s hop, confirm=true (or already loaded) does the work.

admin_lookup does not materialize a divisions spatial index, so a cheap
peek cannot see a 20–40s cold hop (DIVISIONS_INDEX_S is geocode's name
table). The production peek therefore reports ready and never false-asks.
Tests monkeypatch the peek to False to prove the wired cold path.
"""

import asyncio
import time

from placeroot import divisions, progress, server

from .conftest import CENTER_LAT, CENTER_LON


def test_peek_is_cheap_and_reports_ready():
    started = time.monotonic()
    ready = divisions.index_is_loaded()
    elapsed_ms = (time.monotonic() - started) * 1000
    assert ready is True
    assert elapsed_ms < 500


def test_already_warm_skips_confirm():
    result = server.admin_lookup(CENTER_LAT, CENTER_LON)
    assert result.get("error") != "needs_confirm"
    assert "error" not in result
    assert len(result["chain"]) == 5


def test_confirm_true_proceeds_when_already_warm():
    result = server.admin_lookup(CENTER_LAT, CENTER_LON, confirm=True)
    assert "error" not in result
    assert len(result["chain"]) == 5


def test_cold_peek_returns_needs_confirm_fast(monkeypatch):
    monkeypatch.setattr(divisions, "index_is_loaded", lambda: False)
    started = time.monotonic()
    result = server.admin_lookup(CENTER_LAT, CENTER_LON)
    elapsed_ms = (time.monotonic() - started) * 1000
    lo, hi = progress.DIVISIONS_INDEX_S
    assert result["error"] == "needs_confirm"
    assert result["eta"] == progress.format_eta(lo, hi)
    assert result["eta_s"] == [int(lo), int(hi)]
    assert result["eta"] == "about 20–40 seconds"
    assert result["eta_s"] == [20, 40]
    assert "confirm=true" in result["detail"]
    assert "divisions index" in result["detail"]
    assert elapsed_ms < 500


def test_cold_confirm_true_proceeds(monkeypatch):
    monkeypatch.setattr(divisions, "index_is_loaded", lambda: False)
    result = server.admin_lookup(CENTER_LAT, CENTER_LON, confirm=True)
    assert "error" not in result
    assert len(result["chain"]) == 5


def test_peek_does_not_run_admin_lookup(monkeypatch):
    called = []

    def boom(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("admin_lookup must not run during peek")

    monkeypatch.setattr(divisions, "admin_lookup", boom)
    assert divisions.index_is_loaded() is True
    assert called == []


def test_eta_matches_divisions_index_constant():
    payload = server._needs_confirm_admin()
    lo, hi = progress.DIVISIONS_INDEX_S
    assert payload["eta_s"] == [int(lo), int(hi)]
    assert payload["eta"] == progress.format_eta(lo, hi)
    assert payload["eta"] != "about 5–25 seconds"


def test_admin_lookup_schema_exposes_confirm_defaulting_to_false():
    tools = asyncio.run(server.mcp.list_tools())
    tool = next(t for t in tools if t.name == "admin_lookup")
    schema = tool.model_dump(mode="json", by_alias=True, exclude_none=True)["inputSchema"]
    prop = schema["properties"]["confirm"]
    assert prop.get("type") == "boolean"
    assert prop.get("default") is False
    assert "confirm" not in schema.get("required", [])
    desc = (tool.description or "").lower()
    assert "confirm=true after the user agreed" in desc
