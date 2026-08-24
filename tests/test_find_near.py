"""Named-place compose: find_near (#328)."""

from placeroot import geocode, server


def test_find_near_returns_rows_with_a_name_near():
    result = server.find_near("coffee_shop", "Blue Bottle Roastery")
    assert "error" not in result
    assert result["near"]["name"] == "Blue Bottle Roastery"
    assert result["category"] == "coffee_shop"
    assert result["results"]
    assert all(r.get("name") for r in result["results"])
    assert any(r.get("trust_note") for r in result["results"])


def test_find_near_maps_free_text_category(monkeypatch):
    seen = {}

    def fake_find(**kwargs):
        seen.update(kwargs)
        return {
            "results": [
                {
                    "id": "p1",
                    "name": "Corner Playground",
                    "category": "playground",
                    "distance_m": 120,
                    "lat": 40.7,
                    "lon": -73.9,
                    "trust_note": "High confidence",
                    "operating_status": "in business",
                }
            ]
        }

    monkeypatch.setattr(server, "find_places", fake_find)
    monkeypatch.setattr(
        geocode,
        "resolve_named_place",
        lambda query: {
            "name": query,
            "lat": 40.7,
            "lon": -73.9,
            "id": "n1",
            "type": "place",
        },
    )
    result = server.find_near("playgrounds", "Stanford Shopping Center")
    assert "error" not in result
    assert result["near"]["name"] == "Stanford Shopping Center"
    assert result["results"][0]["name"] == "Corner Playground"
    assert result["results"][0]["trust_note"]
    assert server._category_slug("playgrounds") == "playground"
    assert server._category_slug("coffee shops") == "coffee_shop"
    assert seen["category"] == "playground"
    assert result["category"] == "playground"
    assert result["category_resolved_from"] == "playgrounds"


def test_find_near_empty_args_are_bad_request():
    assert server.find_near("  ", "Brooklyn")["error"] == "bad_request"
    assert server.find_near("coffee_shop", "   ")["error"] == "bad_request"


def test_find_near_unresolved_near(monkeypatch):
    monkeypatch.setattr(geocode, "resolve_named_place", lambda *_a, **_k: None)
    result = server.find_near("coffee_shop", "Not A Real Place 9z")
    assert result["error"] == "not_found"
    # Roadmap §4, next tier: not_found from name resolution names the next
    # move rather than leaving the caller to re-guess.
    assert result["try"]
    assert len(result["try"]) < 200
