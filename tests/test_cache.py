import os
import threading
import time

import duckdb
import pytest

from placeroot import cache, db

from .conftest import FIXTURE_PATH

RELEASE = "2026-07-22.0"
THEME = "places"


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "placeroot-cache"
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(d))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    # #142's claim registry is module-global; clear it so a claim left by an
    # earlier test can't shield a later test's tiles from eviction.
    with cache._claims_lock:
        cache._claims.clear()
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


def test_offline_fallback_picks_most_recently_used_not_most_recently_created(
    cache_dir, monkeypatch
):
    # #141: a cache hit bumps tile-*file* mtimes but not the containing
    # dir's mtime, so ranking fingerprint dirs by dir mtime picks the
    # most-recently-created dir, not the most-recently-used one. During an
    # outage that can drop the whole active cache for a barely-populated
    # newer dir. Rank by newest contained tile instead.
    base = cache_dir / RELEASE / THEME
    fp_active = base / "aaaaaaaaaaaa"   # the real working set
    fp_stray = base / "bbbbbbbbbbbb"    # a later one-off, near-empty
    fp_active.mkdir(parents=True)
    for i in range(5):
        (fp_active / f"tile_{i}_0.parquet").write_bytes(b"x")

    # fp_stray is created LATER, so its *directory* mtime is newer than
    # fp_active's — the old (buggy) code would pick it.
    time.sleep(0.01)
    fp_stray.mkdir(parents=True)
    (fp_stray / "tile_9_9.parquet").write_bytes(b"x")

    now = time.time()
    # Simulate: fp_active's tiles were just used (recent file mtimes);
    # fp_stray's single tile is older than that.
    for t in fp_active.glob("*.parquet"):
        os.utime(t, (now, now))
    os.utime(fp_stray / "tile_9_9.parquet", (now - 3600, now - 3600))
    # Sanity: the buggy dir-mtime ranking would prefer fp_stray.
    assert fp_stray.stat().st_mtime >= fp_active.stat().st_mtime

    # Force the offline branch (upstream unreachable → no fresh fingerprint).
    monkeypatch.setattr(cache, "schema_fingerprint", lambda _glob: None)
    assert cache.resolve_fingerprint(RELEASE, THEME, "unused-glob") == "aaaaaaaaaaaa"


def test_conn_lock_is_reentrant_for_the_cache_path_probe(cache_dir):
    # #145: the cache path holds db.conn_lock (in _from_source) and then, on
    # an lru-miss, re-enters it via probe_schema's own `with conn_lock`. A
    # plain (non-reentrant) Lock self-deadlocks that thread; RLock allows the
    # same-thread re-acquire. Force the inner probe to MISS (clear its cache),
    # then reproduce the nesting and assert it completes rather than hangs.
    db._probe_schema_cached.cache_clear()
    glob = str(FIXTURE_PATH)
    result: dict = {}

    def worker():
        with db.conn_lock:  # outer hold, as _from_source does
            result["schema"] = db.probe_schema(glob)  # re-acquires conn_lock

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "conn_lock self-deadlocked on same-thread re-entry"
    assert result["schema"] is not None and len(result["schema"]) > 0
    db._probe_schema_cached.cache_clear()


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

    def counting_ensure_tile(con_, release_, theme_, tile_, upstream_glob_, fingerprint_):
        with lock:
            calls.append(tile_)
        time.sleep(0.1)  # widen the window so a second miss can race in
        return cache.tile_path(release_, theme_, fingerprint_, tile_)

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


# --- Issue #63: schema-fingerprinted tile layout --- #


def _write_fixture_dropping_column(con: duckdb.DuckDBPyConnection, dest, column: str) -> None:
    """A copy of the places fixture with `column` dropped — same rows/bbox
    coverage, different (smaller) schema, so it fingerprints differently."""
    con.execute(
        f"COPY (SELECT * EXCLUDE ({column}) FROM read_parquet('{FIXTURE_PATH}')) "
        f"TO '{dest}' (FORMAT PARQUET)"
    )


def test_stale_tile_ignored_after_upstream_schema_changes(con, cache_dir, sync_cache, tmp_path):
    """The core issue #63 scenario: a tile materialized against schema A
    must not be read back once upstream's schema has moved on to schema B —
    the next query has to notice the fingerprint changed and materialize a
    fresh tile, leaving the old one on disk but unread.
    """
    bbox = (-74.0, 40.0, -73.0, 41.0)  # covers the fixture's NYC-area cluster

    # Schema A: materialize against the full places fixture.
    paths_a = cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(FIXTURE_PATH), lambda: con)
    assert paths_a
    fingerprint_a = cache.resolve_fingerprint(RELEASE, THEME, str(FIXTURE_PATH))
    for p in paths_a:
        assert f"/{fingerprint_a}/" in p

    # Schema B: same rows/bbox, but missing the "confidence" column — a
    # different upstream dataset (older/newer code, a different fixture
    # during dev) with a different column layout.
    fixture_b = tmp_path / "places_no_confidence.parquet"
    _write_fixture_dropping_column(con, fixture_b, "confidence")

    fingerprint_b = cache.resolve_fingerprint(RELEASE, THEME, str(fixture_b))
    assert fingerprint_b != fingerprint_a  # different columns, different fingerprint

    paths_b = cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(fixture_b), lambda: con)
    assert paths_b
    # The new query materialized fresh tiles under schema B's fingerprint —
    # it did not (and could not have) read schema A's stale tile.
    assert paths_b != paths_a
    for p in paths_b:
        assert f"/{fingerprint_b}/" in p
    quoted = ", ".join(f"'{p}'" for p in paths_b)
    desc = con.execute(f"SELECT * FROM read_parquet([{quoted}]) LIMIT 0").description
    assert "confidence" not in {c[0] for c in desc}

    # The old, now-stale schema-A tile is still sitting on disk — untouched,
    # just never looked at again (it self-cleans via LRU eviction later).
    for t in cache.tiles_for_bbox(*bbox):
        assert cache.tile_path(RELEASE, THEME, fingerprint_a, t).exists()


def test_cached_tile_paths_ignores_stale_fingerprint_dir(con, cache_dir, sync_cache, tmp_path):
    """cached_tile_paths() (place_details' cheap first-look path) must only
    ever return tiles under the *current* schema fingerprint, never a
    stale one left over from an earlier schema.
    """
    bbox = (-74.0, 40.0, -73.0, 41.0)
    cache.local_paths_for_query(con, RELEASE, THEME, bbox, str(FIXTURE_PATH), lambda: con)
    stale = cache.cached_tile_paths(RELEASE, THEME, str(FIXTURE_PATH))
    assert stale != []

    fixture_b = tmp_path / "places_no_confidence.parquet"
    _write_fixture_dropping_column(con, fixture_b, "confidence")

    # Nothing has been materialized under schema B's fingerprint yet, so
    # cached_tile_paths must report empty for it — not fall through to
    # schema A's (stale, wrong-schema) tiles.
    assert cache.cached_tile_paths(RELEASE, THEME, str(fixture_b)) == []


def test_offline_fallback_serves_newest_existing_fingerprint_dir(
    con, cache_dir, sync_cache, monkeypatch
):
    """Issue #63's subtle case: upstream is unreachable (probe_schema fails),
    but cache-as-fallback (#5) must still serve whatever fingerprint dir was
    last populated, rather than refusing to answer.
    """
    bbox = (-74.0, 40.0, -73.0, 41.0)
    populated = cache.local_paths_for_query(
        con, RELEASE, THEME, bbox, str(FIXTURE_PATH), lambda: con
    )
    assert populated

    # Simulate upstream going unreachable: the schema probe fails no matter
    # what glob is passed.
    monkeypatch.setattr(db, "probe_schema", lambda glob: None)

    served = cache.local_paths_for_query(
        con, RELEASE, THEME, bbox, "s3://unreachable/*", lambda: con
    )
    assert served == populated  # same on-disk tiles, served without touching "upstream"


def test_offline_fallback_with_no_existing_fingerprint_dir_returns_none(
    con, cache_dir, monkeypatch
):
    """If upstream is unreachable AND nothing has ever been cached for this
    release/theme, there's nothing to fall back to — local_paths_for_query
    must return None (caller falls back to querying upstream directly,
    which will hit the same unreachable error).
    """
    monkeypatch.setattr(db, "probe_schema", lambda glob: None)
    bbox = (-74.0, 40.0, -73.0, 41.0)
    result = cache.local_paths_for_query(
        con, RELEASE, THEME, bbox, "s3://unreachable/*", lambda: con
    )
    assert result is None


def test_eviction_counts_old_layout_tiles_toward_cap(con, cache_dir, monkeypatch):
    """Pre-#63 tiles living directly under <theme>/ (no fingerprint dir) are
    not migrated, but they must still be walked and evicted by the
    size-based LRU cap like any other cached tile — see the module
    docstring's "Old-layout tiles" section for why no active migration.
    """
    # An old-layout tile: no fingerprint subdirectory.
    old_layout_dir = cache.cache_dir() / RELEASE / THEME
    old_layout_dir.mkdir(parents=True)
    old_tile = old_layout_dir / "tile_40_-74.parquet"
    old_tile.write_bytes(b"0" * 4000)
    old_time = time.time() - 1000
    os.utime(old_tile, (old_time, old_time))  # oldest: first evicted

    monkeypatch.setenv("PLACEROOT_CACHE_MAX_MB", str(5000 / 1024 / 1024))
    tiles = [(-74, 40), (-75, 40), (-76, 40)]
    for t in tiles:
        cache.ensure_tile(con, RELEASE, THEME, t, str(FIXTURE_PATH))

    assert not old_tile.exists()  # evicted: it was the oldest file on disk


def test_inflight_dedup_key_includes_fingerprint(con, cache_dir, monkeypatch):
    """Two queries that miss the same tile coordinates but resolve to
    *different* schema fingerprints must each trigger their own background
    fetch — the in-flight dedup key has to include the fingerprint, or a
    fetch for schema A's tile would wrongly suppress schema B's.
    """
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)
    calls: list[tuple] = []
    lock = threading.Lock()

    def counting_ensure_tile(con_, release_, theme_, tile_, upstream_glob_, fingerprint_):
        with lock:
            calls.append((fingerprint_, tile_))
        time.sleep(0.1)  # widen the window so both fetches are in flight together
        return cache.tile_path(release_, theme_, fingerprint_, tile_)

    monkeypatch.setattr(cache, "ensure_tile", counting_ensure_tile)

    def fake_schema_fingerprint(glob: str) -> str:
        return "fingerprintA" if glob == "upstreamA" else "fingerprintB"

    monkeypatch.setattr(cache, "schema_fingerprint", fake_schema_fingerprint)

    bbox = (-74.0, 40.0, -73.0, 41.0)
    tile = cache.tiles_for_bbox(*bbox)[0]
    cache.local_paths_for_query(con, RELEASE, THEME, bbox, "upstreamA", duckdb.connect)
    cache.local_paths_for_query(con, RELEASE, THEME, bbox, "upstreamB", duckdb.connect)
    time.sleep(0.3)  # let both background attempts finish

    assert ("fingerprintA", tile) in calls
    assert ("fingerprintB", tile) in calls


def test_parse_warm_region_valid():
    assert cache.parse_warm_region("40.7,-73.9,1000") == (40.7, -73.9, 1000.0)


def test_parse_warm_region_malformed_returns_none():
    assert cache.parse_warm_region("not,a,region,at,all") is None
    assert cache.parse_warm_region("abc,def") is None
    assert cache.parse_warm_region("abc,def,ghi") is None


# --- #142: eviction must not delete a tile an in-flight query resolved ------


def test_resolved_tiles_survive_a_concurrent_eviction(con, cache_dir, sync_cache, monkeypatch):
    """The exact race: a query resolves its cache-hit paths and releases the
    lock, then a concurrent miss triggers eviction under a tiny cap before
    the first query's SELECT runs. The resolved tiles must still be there.
    """
    bbox = (-74.0, 40.0, -73.0, 41.0)
    paths = cache.local_paths_for_query(
        con, RELEASE, THEME, bbox, str(FIXTURE_PATH), lambda: con
    )
    assert paths

    # Cap of ~0 bytes: without the claim guard, eviction walks the whole
    # cache and deletes everything, including what we just resolved.
    monkeypatch.setenv("PLACEROOT_CACHE_MAX_MB", "0")
    cache.evict_if_needed()

    for p in paths:
        assert os.path.exists(p), "an in-flight query's tile was evicted mid-query"

    # And the read that the real query would run still succeeds.
    joined = ", ".join(f"'{p}'" for p in paths)
    (n,) = con.execute(f"SELECT count(*) FROM read_parquet([{joined}])").fetchone()
    assert n > 0


def test_unclaimed_tiles_are_still_evicted_normally(con, cache_dir, monkeypatch):
    """The guard is narrow: tiles nobody is mid-query on evict as before, so
    the size cap still does its job."""
    monkeypatch.setenv("PLACEROOT_CACHE_MAX_MB", str(5000 / 1024 / 1024))
    tiles = [(-74, 40), (-75, 40), (-76, 40), (15, 78)]
    paths = [cache.ensure_tile(con, RELEASE, THEME, t, str(FIXTURE_PATH)) for t in tiles]
    remaining = list(cache.cache_dir().rglob("*.parquet"))
    assert len(remaining) < len(paths)


def test_claims_expire_so_tiles_do_not_pin_the_cache_forever(con, cache_dir, sync_cache):
    """A claim is a short lease, not a permanent pin — otherwise a crashed or
    abandoned query would keep its tiles un-evictable for the process's life.
    """
    bbox = (-74.0, 40.0, -73.0, 41.0)
    paths = cache.local_paths_for_query(
        con, RELEASE, THEME, bbox, str(FIXTURE_PATH), lambda: con
    )
    assert paths
    now = time.monotonic()
    with cache._claims_lock:
        assert all(cache._is_claimed_locked(p, now) for p in paths)

    # Re-claim with an already-elapsed lease, standing in for the passage of
    # _CLAIM_TTL_S without sleeping through it.
    with cache._claims_lock:
        for p in paths:
            cache._claims[p] = time.monotonic() - 1.0

    # Expired claims no longer protect anything, and eviction clears them.
    now = time.monotonic()
    with cache._claims_lock:
        assert not any(cache._is_claimed_locked(p, now) for p in paths)


def test_claim_paths_never_shortens_an_existing_claim():
    path = "/tmp/placeroot-test-tile.parquet"
    with cache._claims_lock:
        cache._claims.clear()
    cache.claim_paths([path], ttl_s=300.0)
    long_deadline = cache._claims[path]
    cache.claim_paths([path], ttl_s=1.0)  # a shorter concurrent claim
    assert cache._claims[path] == long_deadline
    with cache._claims_lock:
        cache._claims.clear()


def test_eviction_blocked_while_a_query_is_resolving_its_tiles(con, cache_dir, sync_cache):
    """An eviction pass that starts *while* a query is resolving its tiles
    must not take those tiles.

    Drives eviction from another thread from inside the scan (hooking
    os.utime, which runs within the scan's critical section) rather than
    before or after it, so the two genuinely overlap.

    Scope note: this pins the end-to-end property against the pre-fix
    behavior (no claims at all). It does NOT discriminate between claiming
    inside the scan's critical section and claiming immediately after it --
    the lock serializes both, and which one wins the handoff is a timing
    detail. The claim is made inside the section because that ordering is
    correct by construction, not because this test can observe it.
    """
    bbox = (-74.0, 40.0, -73.0, 41.0)
    assert cache.local_paths_for_query(
        con, RELEASE, THEME, bbox, str(FIXTURE_PATH), lambda: con
    )
    # Forget the populate step's claims: this test is about whether the
    # cache-HIT path protects what it resolves.
    with cache._claims_lock:
        cache._claims.clear()

    evictions: list[threading.Thread] = []
    real_utime = os.utime

    def utime_then_evict(path, times):
        real_utime(path, times)
        if evictions:  # only interleave once, on the first tile scanned
            return
        # Cap of 0: this pass would delete every unclaimed tile in the cache.
        os.environ["PLACEROOT_CACHE_MAX_MB"] = "0"
        t = threading.Thread(target=cache.evict_if_needed, daemon=True)
        evictions.append(t)
        t.start()
        time.sleep(0.05)  # let it reach (and block on) the claims lock

    os.utime = utime_then_evict
    try:
        paths = cache.local_paths_for_query(
            con, RELEASE, THEME, bbox, str(FIXTURE_PATH), lambda: con
        )
    finally:
        os.utime = real_utime
        for t in evictions:
            t.join(timeout=5)
            assert not t.is_alive(), "eviction thread never released the claims lock"
        os.environ.pop("PLACEROOT_CACHE_MAX_MB", None)

    assert paths
    missing = [p for p in paths if not os.path.exists(p)]
    assert not missing, f"eviction deleted {len(missing)} tile(s) mid-resolve: {missing}"
