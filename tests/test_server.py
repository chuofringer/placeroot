import os
import time

from placeroot import server
from placeroot.cache import parse_warm_region

from .conftest import CENTER_LAT, CENTER_LON


def test_find_places_tool_wraps_results_and_applies_budget():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    assert "results" in result
    assert len(result["results"]) == 10
    assert "truncated" not in result  # 10 small rows comfortably fit the default budget


def test_summarize_area_tool():
    result = server.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert "top_categories" in result
    assert result["total_places"] > 0


def test_find_places_tool_truncates_under_tiny_budget(monkeypatch):
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "10")
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert result["truncated"] is True
    assert result["omitted_count"] > 0


def test_warm_start_is_a_noop_without_warm_region(monkeypatch):
    monkeypatch.delenv("PLACEROOT_WARM_REGION", raising=False)
    server._warm_start()  # must not raise


def test_warm_start_logs_and_continues_on_malformed_region(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("PLACEROOT_WARM_REGION", "not-a-region")
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    with caplog.at_level(logging.WARNING, logger="placeroot.server"):
        server._warm_start()  # must not raise
    assert "malformed" in caplog.text


def test_warm_start_skipped_when_cache_disabled(monkeypatch):
    monkeypatch.setenv("PLACEROOT_WARM_REGION", f"{CENTER_LAT},{CENTER_LON},1000")
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    server._warm_start()  # must not raise, and must not attempt to query


def test_parse_warm_region_reexported_for_operators():
    # PLACEROOT_WARM_REGION's format is documented via this parser.
    assert parse_warm_region("40.7,-73.9,1000") == (40.7, -73.9, 1000.0)


def test_warm_start_forces_sync_cache_mode_and_restores_previous_value(monkeypatch):
    """Issue #31: _warm_start must materialize its tiles inline (it's a
    one-shot startup call, blocking is fine), and must not leak
    PLACEROOT_CACHE_SYNC into the rest of the process afterward.
    """
    monkeypatch.setenv("PLACEROOT_WARM_REGION", f"{CENTER_LAT},{CENTER_LON},1000")
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)

    seen_during_call = {}
    original_find_places = server.overture.find_places

    def spy_find_places(*args, **kwargs):
        seen_during_call["sync"] = os.environ.get("PLACEROOT_CACHE_SYNC")
        return original_find_places(*args, **kwargs)

    monkeypatch.setattr(server.overture, "find_places", spy_find_places)
    server._warm_start()
    assert seen_during_call["sync"] == "1"
    assert os.environ.get("PLACEROOT_CACHE_SYNC") is None  # restored


def test_warm_start_restores_prior_sync_value_if_one_was_set(monkeypatch):
    monkeypatch.setenv("PLACEROOT_WARM_REGION", f"{CENTER_LAT},{CENTER_LON},1000")
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.setenv("PLACEROOT_CACHE_SYNC", "0")
    server._warm_start()
    assert os.environ.get("PLACEROOT_CACHE_SYNC") == "0"


def test_warm_metadata_async_returns_without_waiting_for_the_probe(monkeypatch):
    """The pre-warm must be fire-and-forget: main() shouldn't stall startup
    waiting on a network round trip to the active dataset.
    """
    started = []

    def slow_warm_metadata():
        started.append(True)
        time.sleep(0.2)

    monkeypatch.setattr(server.overture, "warm_metadata", slow_warm_metadata)
    t0 = time.monotonic()
    server._warm_metadata_async()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1  # returned immediately, didn't wait for the 0.2s "probe"
    time.sleep(0.3)  # let the background thread finish before the test exits
    assert started == [True]


def test_release_attribution_reaches_instructions():
    """Regression: MCPServer.instructions is a read-only property; main()
    must write through the low-level server or it crashes at startup."""
    from placeroot import server

    original = server.mcp._lowlevel_server.instructions
    try:
        server.mcp._lowlevel_server.instructions = "probe"
        assert server.mcp.instructions == "probe"
    finally:
        server.mcp._lowlevel_server.instructions = original
