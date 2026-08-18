"""Optional city warmup (issue #314): pre-cache a metro via the existing cache."""

from placeroot import cache, server

from .conftest import CENTER_LAT, CENTER_LON


def test_warmup_city_requires_city_or_point():
    result = server.warmup_city()
    assert result["error"] == "bad_request"
    assert "city" in result["detail"]


def test_warmup_city_rejects_city_and_point_together():
    result = server.warmup_city(city="Palo Alto", lat=CENTER_LAT, lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_warmup_city_rejects_swapped_coords():
    result = server.warmup_city(lat=200, lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_warmup_city_rejects_empty_city():
    result = server.warmup_city(city="   ")
    assert result["error"] == "bad_request"


def test_warmup_city_is_honest_when_cache_is_off(monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    result = server.warmup_city(lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000)
    assert result["status"] == "cache_disabled"
    assert result["themes"] == []
    note = result["note"].lower()
    assert "cache is off" in note or "nothing to pre-warm" in note


def test_warmup_city_pre_caches_via_existing_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)
    result = server.warmup_city(lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000)
    assert result["error"] if "error" in result else result["status"] in {
        "warmed",
        "already_warm",
        "partial",
    }
    assert result["lat"] == CENTER_LAT
    assert result["lon"] == CENTER_LON
    assert result["themes"]
    assert {row["theme"] for row in result["themes"]} == {"places", "transportation"}
    # Same cache directory every later query uses — no second cache.
    assert any(tmp_path.joinpath("c").rglob("tile_*.parquet"))


def test_warmup_city_clamps_oversized_radius(monkeypatch):
    seen = {}

    def spy(lat, lon, radius_m):
        seen["radius_m"] = radius_m
        return {
            "lat": lat,
            "lon": lon,
            "radius_m": min(radius_m, server.MAX_WARMUP_RADIUS_M),
            "status": "warmed",
            "themes": [],
            "note": "ok",
        }

    monkeypatch.setattr(server, "_prewarm_region", spy)
    server.warmup_city(lat=CENTER_LAT, lon=CENTER_LON, radius_m=1_000_000)
    assert seen["radius_m"] == 1_000_000  # clamp lives inside _prewarm_region
    # And the helper itself clamps:
    payload = server._prewarm_region(CENTER_LAT, CENTER_LON, 1_000_000)
    # cache may be on (fixtures); either way radius is capped
    assert payload["radius_m"] == server.MAX_WARMUP_RADIUS_M


def test_warmup_city_resolves_a_fixture_city_name():
    result = server.warmup_city(city="Brooklyn")
    assert "error" not in result
    assert result["city"]["name"] == "Brooklyn"
    assert result["status"] in {"warmed", "already_warm", "partial", "cache_disabled"}


def test_prewarm_region_uses_cache_prewarm_bbox(monkeypatch):
    calls = []

    def spy_prewarm(*args, **kwargs):
        calls.append((args, kwargs))
        return {"theme": args[2], "status": "already_warm", "tiles": 1, "cached": 1, "fetched": 0}

    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.setattr(cache, "prewarm_bbox", spy_prewarm)
    payload = server._prewarm_region(CENTER_LAT, CENTER_LON, 1000)
    assert payload["status"] == "already_warm"
    assert [c[0][2] for c in calls] == ["places", "transportation"]
