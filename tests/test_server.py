from placeroot import server

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
