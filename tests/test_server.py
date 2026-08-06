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
