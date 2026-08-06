
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


def test_tiles_for_bbox_single_tile():
    assert cache.tiles_for_bbox(-73.95, 40.65, -73.85, 40.75) == [(-74, 40)]


def test_tiles_for_bbox_spans_multiple_tiles():
    tiles = cache.tiles_for_bbox(-74.5, 40.5, -73.5, 41.5)
    assert set(tiles) == {(-75, 40), (-75, 41), (-74, 40), (-74, 41)}


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


def test_populate_then_hit_does_not_touch_upstream_again(con, cache_dir, tmp_path):
    # Copy the fixture somewhere we can delete, standing in for "upstream".
    upstream = tmp_path / "upstream.parquet"
    upstream.write_bytes(FIXTURE_PATH.read_bytes())

    bbox = (-74.0, 40.0, -73.0, 41.0)
    paths = cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(upstream))
    assert paths is not None and paths

    upstream.unlink()  # upstream is now gone

    # Same tiles, same bbox: must be served from the already-cached files,
    # not re-fetched (which would raise since upstream no longer exists).
    paths_again = cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(upstream))
    assert paths_again == paths


def test_local_paths_for_query_returns_none_when_disabled(con, cache_dir, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    bbox = (-74.0, 40.0, -73.0, 41.0)
    result = cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(FIXTURE_PATH))
    assert result is None


def test_lru_eviction_removes_oldest_tiles_when_over_cap(con, cache_dir, monkeypatch):
    # ~2000 bytes: room for a couple of small tiles but not the whole set,
    # forcing eviction of the least-recently-used ones.
    monkeypatch.setenv("PLACEROOT_CACHE_MAX_MB", str(2000 / 1024 / 1024))
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
