"""Auto-warm on city-scale resolve + persisted walk graphs (issue #330)."""

import threading
import time

from placeroot import autowarm, cache, routing, server

from ._routing_fixture import build_routing_fixture as fx
from .conftest import CENTER_LAT, CENTER_LON


def test_is_city_scale_accepts_locality_not_poi_or_region():
    assert autowarm.is_city_scale({"type": "locality", "lat": 1.0, "lon": 2.0})
    assert autowarm.is_city_scale({"type": "localadmin", "lat": 1.0, "lon": 2.0})
    assert autowarm.is_city_scale({"type": "neighborhood", "lat": 1.0, "lon": 2.0})
    assert not autowarm.is_city_scale({"type": "place", "lat": 1.0, "lon": 2.0})
    assert not autowarm.is_city_scale({"kind": "place", "type": "locality", "lat": 1, "lon": 2})
    assert not autowarm.is_city_scale({"type": "country", "lat": 1.0, "lon": 2.0})
    assert not autowarm.is_city_scale({"type": "region", "lat": 1.0, "lon": 2.0})
    assert not autowarm.is_city_scale({"type": "county", "lat": 1.0, "lon": 2.0})
    assert not autowarm.is_city_scale({"type": "postcode", "lat": 1.0, "lon": 2.0})
    assert not autowarm.is_city_scale({"type": "address", "lat": 1.0, "lon": 2.0})


def test_city_geocode_schedules_autowarm(monkeypatch):
    seen = []
    monkeypatch.setattr(autowarm, "schedule_autowarm", lambda lat, lon: seen.append((lat, lon)))
    result = server.geocode("Brooklyn")
    assert result["results"]
    top = result["results"][0]
    assert top["type"] in autowarm.CITY_SCALE_TYPES
    assert seen == [(top["lat"], top["lon"])]


def test_poi_resolve_does_not_schedule_autowarm(monkeypatch):
    seen = []
    monkeypatch.setattr(autowarm, "schedule_autowarm", lambda lat, lon: seen.append((lat, lon)))
    result = server.resolve_place(
        "Blue Bottle Roastery", near_lat=CENTER_LAT, near_lon=CENTER_LON, limit=5
    )
    assert result["results"]
    assert result["results"][0]["kind"] == "place"
    assert seen == []


def test_schedule_autowarm_is_noop_when_cache_is_off(monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    started = []
    monkeypatch.setattr(server, "_prewarm_region", lambda *a, **k: started.append(1))
    autowarm.clear_autowarm_state()
    autowarm.schedule_autowarm(CENTER_LAT, CENTER_LON)
    time.sleep(0.05)
    assert started == []


def test_schedule_autowarm_is_noop_when_autowarm_is_off(monkeypatch, tmp_path):
    """PLACEROOT_AUTOWARM=off (#471): cache on, no thread, no prewarm."""
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.setenv("PLACEROOT_AUTOWARM", "off")
    assert cache.enabled()
    assert not autowarm.enabled()
    started = []
    monkeypatch.setattr(server, "_prewarm_region", lambda *a, **k: started.append(1))
    before = {t.name for t in threading.enumerate()}
    autowarm.clear_autowarm_state()
    autowarm.schedule_autowarm(CENTER_LAT, CENTER_LON)
    spawned = {t.name for t in threading.enumerate()} - before
    assert not any(n.startswith("placeroot-autowarm-") for n in spawned)
    assert started == []
    # Not deduped as "in flight" either: turning it on later must still warm.
    assert autowarm.metro_key(CENTER_LAT, CENTER_LON) not in autowarm._inflight


def test_autowarm_env_is_on_by_default_and_off_is_case_insensitive(monkeypatch):
    monkeypatch.delenv("PLACEROOT_AUTOWARM", raising=False)
    assert autowarm.enabled()
    monkeypatch.setenv("PLACEROOT_AUTOWARM", "1")
    assert autowarm.enabled()
    monkeypatch.setenv("PLACEROOT_AUTOWARM", " OFF ")
    assert not autowarm.enabled()


def test_schedule_autowarm_returns_without_waiting(monkeypatch, tmp_path):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    # Opt in to the real thread (conftest's no_autowarm_threads sets it off).
    monkeypatch.delenv("PLACEROOT_AUTOWARM", raising=False)
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: None)
    entered = threading.Event()
    release = threading.Event()

    def slow(lat, lon, radius_m):
        entered.set()
        release.wait(timeout=2)
        return {
            "lat": lat, "lon": lon, "radius_m": radius_m,
            "status": "warmed", "themes": [], "note": "ok",
        }

    monkeypatch.setattr(server, "_prewarm_region", slow)
    autowarm.clear_autowarm_state()
    t0 = time.perf_counter()
    autowarm.schedule_autowarm(CENTER_LAT, CENTER_LON)
    assert time.perf_counter() - t0 < 0.5
    assert entered.wait(timeout=1)
    # Background prewarm must not hold conn_lock across the wait.
    got = server.db.conn_lock.acquire(timeout=0.3)
    assert got
    server.db.conn_lock.release()
    release.set()
    # Join: a thread that outlives its test runs against the next test's
    # environment (#471); conftest fails any test that leaks one.
    for t in threading.enumerate():
        if t.name.startswith("placeroot-autowarm-"):
            t.join(timeout=5)
    assert not [t for t in threading.enumerate() if t.name.startswith("placeroot-autowarm-")]


def test_disk_marker_skips_a_second_autowarm(monkeypatch, tmp_path):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    key = autowarm.metro_key(CENTER_LAT, CENTER_LON)
    autowarm.write_warm_marker(key)
    started = []
    monkeypatch.setattr(server, "_prewarm_region", lambda *a, **k: started.append(1))
    autowarm.clear_autowarm_state()
    autowarm.schedule_autowarm(CENTER_LAT, CENTER_LON)
    time.sleep(0.05)
    assert started == []
    assert autowarm.warm_marker_exists(key)


def test_autowarm_reuses_prewarm_region(monkeypatch, tmp_path):
    """Background warm must call the existing path, not a second prewarm."""
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    calls = []

    def spy(lat, lon, radius_m):
        calls.append((lat, lon, radius_m))
        return {
            "lat": lat, "lon": lon, "radius_m": radius_m,
            "status": "already_warm", "themes": [], "note": "ok",
        }

    monkeypatch.setattr(server, "_prewarm_region", spy)
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: None)
    autowarm.clear_autowarm_state()
    autowarm._run_autowarm(CENTER_LAT, CENTER_LON, autowarm.metro_key(CENTER_LAT, CENTER_LON))
    assert calls == [(CENTER_LAT, CENTER_LON, server.DEFAULT_WARMUP_RADIUS_M)]


def test_walk_graph_survives_clear_graph_cache(tmp_path, monkeypatch):
    """clear_graph_cache is the in-process equivalent of a process restart."""
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    lat, lon = fx.node_latlon(10, 10)
    g1 = routing._get_or_build_graph(lat, lon, 400.0, "walk", None, want_shapes=True)
    assert g1.node_count() > 0
    graphs = list((tmp_path / "c").rglob("*.pkl"))
    assert graphs
    assert all("graphs" in p.parts for p in graphs)
    assert not any(p.name.startswith("tile_") for p in graphs)
    source = next(iter(g1.adjacency))
    reached1 = routing.dijkstra(g1, source, 600.0, routing.DEFAULT_SPEED_M_S)
    assert reached1
    routing.clear_graph_cache()
    assert len(routing._graph_cache) == 0
    builds = []
    real = routing.build_graph

    def spy(*args, **kwargs):
        builds.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", spy)
    g2 = routing._get_or_build_graph(lat, lon, 400.0, "walk", None, want_shapes=True)
    assert builds == []
    assert g2.node_count() == g1.node_count()
    reached2 = routing.dijkstra(g2, source, 600.0, routing.DEFAULT_SPEED_M_S)
    assert reached2 == reached1


def test_persisted_graphs_are_outside_the_tile_lru(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("PLACEROOT_CACHE_MAX_MB", "0.000001")
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    lat, lon = fx.node_latlon(10, 10)
    routing._get_or_build_graph(lat, lon, 200.0, "walk", None)
    graphs = list((tmp_path / "c").rglob("*.pkl"))
    assert graphs
    cache.evict_if_needed()
    assert all(p.exists() for p in graphs)


def test_tiles_vs_graph_honesty_in_copy():
    """Warming tiles must not be described as a 0.7s first walk."""
    blobs = [
        autowarm.__doc__ or "",
        autowarm.schedule_autowarm.__doc__ or "",
        server._prewarm_region.__doc__ or "",
        server.warmup_city.__doc__ or "",
        server.BASE_INSTRUCTIONS,
    ]
    text = "\n".join(blobs).lower()
    assert "graph" in text
    assert "tile" in text
    assert "0.7" not in text
    assert "street-graph neighborhood" not in text
    # The warmup tool itself still does not build the graph.
    assert "does not build the routing graph" in (server.warmup_city.__doc__ or "").lower()

def test_failed_prewarm_does_not_write_marker(monkeypatch, tmp_path):
    """Transient S3/upstream fail must stay retryable — no disk marker."""
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    key = autowarm.metro_key(CENTER_LAT, CENTER_LON)

    def fail(lat, lon, radius_m):
        return {
            "lat": lat, "lon": lon, "radius_m": radius_m,
            "status": "failed", "themes": [], "note": "upstream_unavailable",
        }

    monkeypatch.setattr(server, "_prewarm_region", fail)
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: None)
    autowarm.clear_autowarm_state()
    autowarm._run_autowarm(CENTER_LAT, CENTER_LON, key)
    assert not autowarm.warm_marker_exists(key)


def test_partial_prewarm_does_not_write_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    key = autowarm.metro_key(CENTER_LAT, CENTER_LON)

    def partial(lat, lon, radius_m):
        return {
            "lat": lat, "lon": lon, "radius_m": radius_m,
            "status": "partial", "themes": [], "note": "mixed",
        }

    monkeypatch.setattr(server, "_prewarm_region", partial)
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: None)
    autowarm.clear_autowarm_state()
    autowarm._run_autowarm(CENTER_LAT, CENTER_LON, key)
    assert not autowarm.warm_marker_exists(key)


def test_preferred_disk_name_uses_padded_radius():
    """Persist writes padded radius; exact-filename lookup must match."""
    mode = "walk"
    cap_m = routing.MODE_CONFIG[mode]["max_radius_m"]
    tile = (0, 0)
    raw = routing.WALK_MAX_RADIUS_M
    padded = min(raw * routing.GRAPH_CACHE_MARGIN, cap_m)
    raw_name = routing._graph_disk_name(mode, "default", tile, raw, True)
    padded_name = routing._graph_disk_name(mode, "default", tile, padded, True)
    persist_name = routing._graph_disk_name(
        mode, "default", tile,
        min(raw * routing.GRAPH_CACHE_MARGIN, cap_m),
        True,
    )
    assert padded_name == persist_name
    # The old list used raw radii and never matched a padded persist name
    # unless the cap already clamped them equal.
    if padded != raw:
        assert raw_name != persist_name

