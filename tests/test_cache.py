import threading
import time

import duckdb
import pytest

from placeroot import cache

from .conftest import FIXTURE_PATH

RELEASE = "2026-07-22.0"
THEME = "places"


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "placeroot-cache"
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(d))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    return d


@pytest.fixture
def con():
    return duckdb.connect()


@pytest.fixture
def sync_cache(monkeypatch):
    """PLACEROOT_CACHE_SYNC=1: local_paths_for_query materializes missing
    tiles inline instead of handing them to a background thread, so tests
    that need deterministic populate-then-hit behavior can rely on it.
    """
    monkeypatch.setenv("PLACEROOT_CACHE_SYNC", "1")


def test_tiles_for_bbox_single_tile():
    assert cache.tiles_for_bbox(-73.95, 40.65, -73.85, 40.75) == [(-74, 40)]


def test_tiles_for_bbox_spans_multiple_tiles():
    tiles = cache.tiles_for_bbox(-74.5, 40.5, -73.5, 41.5)
    assert set(tiles) == {(-75, 40), (-75, 41), (-74, 40), (-74, 41)}


def test_tiles_for_bbox_crosses_east_seam():
    """Issue #42: a bbox that overshoots +180 (xmax > 180) must enumerate
    tiles on both sides of the antimeridian — the east-of-seam column
    (179) and the wrapped west-of-seam column (-180), not a bogus tile at
    raw column 180."""
    tiles = cache.tiles_for_bbox(179.5, 9.0, 180.5, 11.0)
    xs = {tx for tx, _ty in tiles}
    assert 179 in xs
    assert -180 in xs
    assert 180 not in xs


def test_tiles_for_bbox_crosses_west_seam():
    tiles = cache.tiles_for_bbox(-180.5, 9.0, -179.5, 11.0)
    xs = {tx for tx, _ty in tiles}
    assert -180 in xs
    assert 179 in xs
    assert -181 not in xs


def test_tiles_for_bbox_non_crossing_box_unaffected_by_wrap():
    """Wrapping is the identity for an already in-range box — same tile ids
    as before the antimeridian fix."""
    assert cache.tiles_for_bbox(-73.95, 40.65, -73.85, 40.75) == [(-74, 40)]


def test_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    assert cache.enabled() is True


def test_enabled_false_when_off(monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    assert cache.enabled() is False


def test_ensure_tile_materializes_from_upstream_then_reuses_local_file(con, cache_dir):
    bbox = (-74.0, 40.0, -73.0, 41.0)  # covers the fixture's NYC-area cluster
    tiles = cache.tiles_for_bbox(*bbox)
    paths = [cache.ensure_tile(con, RELEASE, THEME, t, str(FIXTURE_PATH)) for t in tiles]
    assert all(p.exists() for p in paths)

    # Query the materialized local files directly; they must contain rows.
    joined = ", ".join(f"'{p}'" for p in paths)
    (n,) = con.execute(f"SELECT count(*) FROM read_parquet([{joined}])").fetchone()
    assert n > 0


def test_populate_then_hit_does_not_touch_upstream_again(con, cache_dir, sync_cache, tmp_path):
    # Copy the fixture somewhere we can delete, standing in for "upstream".
    upstream = tmp_path / "upstream.parquet"
    upstream.write_bytes(FIXTURE_PATH.read_bytes())

    bbox = (-74.0, 40.0, -73.0, 41.0)
    paths = cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(upstream), lambda: con)
    assert paths is not None and paths

    upstream.unlink()  # upstream is now gone

    # Same tiles, same bbox: must be served from the already-cached files,
    # not re-fetched (which would raise since upstream no longer exists).
    paths_again = cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(upstream), lambda: con)
    assert paths_again == paths


def test_local_paths_for_query_returns_none_when_disabled(con, cache_dir, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    bbox = (-74.0, 40.0, -73.0, 41.0)
    result = cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(FIXTURE_PATH), lambda: con)
    assert result is None


def test_local_paths_for_query_returns_none_on_cold_miss_by_default(con, cache_dir, monkeypatch):
    """Query-first, materialize-later (issue #31): a cold tile must not
    block the caller — the default (no PLACEROOT_CACHE_SYNC) hands the
    fetch to a background thread and returns None immediately so the
    caller falls back to scanning upstream directly for this query.
    """
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)
    bbox = (-74.0, 40.0, -73.0, 41.0)
    result = cache.local_paths_for_query(
        con, RELEASE, THEME, bbox, str(FIXTURE_PATH), duckdb.connect
    )
    assert result is None
    # Let the background fetch finish so it doesn't leak into other tests.
    time.sleep(0.3)


def test_cold_miss_does_not_block_even_when_background_fetch_is_broken(con, cache_dir, monkeypatch):
    """The background materialization is best-effort: if it fails (or would
    hang), the caller must still get its None-means-"use upstream" answer
    immediately rather than waiting on it.
    """
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)

    def broken_ensure_tile(*args, **kwargs):
        raise RuntimeError("simulated broken upstream fetch")

    monkeypatch.setattr(cache, "ensure_tile", broken_ensure_tile)
    bbox = (-74.0, 40.0, -73.0, 41.0)
    result = cache.local_paths_for_query(
        con, RELEASE, THEME, bbox, str(FIXTURE_PATH), duckdb.connect
    )
    assert result is None  # returned immediately, not raised
    time.sleep(0.3)  # let the doomed background thread finish quietly


def test_concurrent_cache_misses_on_same_tile_only_fetch_once(con, cache_dir, monkeypatch):
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)
    calls: list[tuple] = []
    lock = threading.Lock()

    def counting_ensure_tile(con_, release_, theme_, tile_, upstream_glob_):
        with lock:
            calls.append(tile_)
        time.sleep(0.1)  # widen the window so a second miss can race in
        return cache.tile_path(release_, theme_, tile_)

    monkeypatch.setattr(cache, "ensure_tile", counting_ensure_tile)

    bbox = (-74.0, 40.0, -73.0, 41.0)
    tile = cache.tiles_for_bbox(*bbox)[0]
    cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(FIXTURE_PATH), duckdb.connect)
    cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(FIXTURE_PATH), duckdb.connect)
    time.sleep(0.3)  # let both background attempts finish

    assert calls.count(tile) == 1


def test_lru_eviction_removes_oldest_tiles_when_over_cap(con, cache_dir, monkeypatch):
    # ~5000 bytes: room for a couple of small tiles but not the whole set,
    # forcing eviction of the least-recently-used ones. (Bumped from 2000
    # when the places fixture grew place_details columns, issue #9 — the
    # NYC-area tile alone is now ~20KB.)
    monkeypatch.setenv("PLACEROOT_CACHE_MAX_MB", str(5000 / 1024 / 1024))
    tiles = [(-74, 40), (-75, 40), (-76, 40), (15, 78)]
    paths = []
    for t in tiles:
        p = cache.ensure_tile(con, RELEASE, THEME, t, str(FIXTURE_PATH))
        paths.append(p)

    remaining = list(cache.cache_dir().rglob("*.parquet"))
    # The cap is tiny, so eviction must have run and left fewer files than fetched.
    assert len(remaining) < len(paths)
    # The most recently fetched tile should still be present (LRU keeps the newest).
    assert paths[-1].exists()


def test_parse_warm_region_valid():
    assert cache.parse_warm_region("40.7,-73.9,1000") == (40.7, -73.9, 1000.0)


def test_parse_warm_region_malformed_returns_none():
    assert cache.parse_warm_region("not,a,region,at,all") is None
    assert cache.parse_warm_region("abc,def") is None
    assert cache.parse_warm_region("abc,def,ghi") is None
