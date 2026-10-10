"""Heavy-theme first touch: direct bbox-pushdown scan, tiles warm in the
background, nothing COPYs under db.conn_lock (perf/routing-direct-scan).

All offline: the schema probe runs on a bare local connection, the
"upstream" is the local places fixture, and every COPY is a stub that
records the call (and asserts the shared connection lock is not held by
the thread running it).
"""

import threading
import time

import duckdb
import pytest

from placeroot import cache, db, release, routing

from .conftest import FIXTURE_PATH

THEME = "transportation"  # a real heavy theme (HEAVY_THEME_TILE_DEG)
# A box inside one 0.125° transportation tile (tx=-592, ty=325).
ONE_TILE_BBOX = (-73.95, 40.65, -73.94, 40.66)


def _conn_lock_held_by_this_thread() -> bool:
    """CPython's RLock reports ownership; db.conn_lock delegates to one."""
    return db.conn_lock._target()._is_owned()


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """Tile cache in tmp, probe/cursors on bare local connections, no
    background delay, clean claim/in-flight registries."""
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "placeroot-cache"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)
    monkeypatch.delenv("PLACEROOT_INLINE_TILE_COPY", raising=False)
    monkeypatch.setattr(cache, "BACKGROUND_FETCH_DELAY_S", 0.0)
    # The shared instance needs httpfs (a network install); the fixture is
    # a local parquet that a bare connection reads fine.
    monkeypatch.setattr(db, "new_connection", duckdb.connect)
    probed: dict[str, frozenset] = {}

    def probe(glob: str):
        if glob not in probed:
            desc = duckdb.connect().execute(
                f"SELECT * FROM read_parquet({db._sql_str(glob)}) LIMIT 0"
            ).description
            probed[glob] = frozenset(c[0] for c in desc)
        return probed[glob]

    monkeypatch.setattr(db, "probe_schema", probe)
    with cache._claims_lock:
        cache._claims.clear()
    with cache._inflight_lock:
        cache._inflight.clear()
    yield
    deadline = time.monotonic() + 10
    while cache._inflight and time.monotonic() < deadline:
        time.sleep(0.01)


def _drain_background():
    deadline = time.monotonic() + 10
    while cache._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not cache._inflight, "background fetch did not finish"


def _recording_ensure_tile(calls, lock, *, create=False, delay=0.0):
    """A COPY stand-in: records (tile, thread) and asserts conn_lock is not
    held by the thread that would be running the COPY."""

    def ensure_tile(con, release_, theme_, tile_, upstream_glob_, fingerprint_=None):
        assert not _conn_lock_held_by_this_thread(), (
            "tile COPY ran while db.conn_lock was held by this thread"
        )
        with lock:
            calls.append((tile_, threading.get_ident()))
        if delay:
            time.sleep(delay)
        path = cache.tile_path(release_, theme_, fingerprint_, tile_)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
        return path

    return ensure_tile


# --- source_sql decision logic ---------------------------------------------


def test_heavy_theme_tiles_on_disk_are_read_locally(offline, monkeypatch):
    glob = str(FIXTURE_PATH)
    rel = release.resolve_release()
    fp = cache.resolve_fingerprint(rel, THEME, glob)
    (tile,) = cache.tiles_for_bbox(*ONE_TILE_BBOX, tile_deg=cache.tile_deg_for(THEME))
    path = cache.ensure_tile(duckdb.connect(), rel, THEME, tile, glob, fp)
    assert path.exists()

    scheduled = []
    monkeypatch.setattr(
        cache, "_materialize_in_background", lambda *a, **k: scheduled.append(a)
    )
    source = cache.source_sql(THEME, glob, ONE_TILE_BBOX)
    assert source == f"read_parquet([{db._sql_str(str(path))}])"
    assert scheduled == [], "a fully cached box must not schedule any fetch"


def test_heavy_theme_missing_tiles_scan_upstream_and_warm_in_background(
    offline, monkeypatch
):
    """Default: no inline COPY. The query reads upstream directly (the
    bbox WHERE of the caller does row-group pruning) and every missing
    tile is scheduled on the background fetch path exactly once."""
    glob = str(FIXTURE_PATH)
    scheduled = []
    monkeypatch.setattr(
        cache,
        "_materialize_in_background",
        lambda rel_, theme_, tile_, glob_, fp_, factory: scheduled.append(
            (theme_, tile_, factory)
        ),
    )
    copies = []
    monkeypatch.setattr(
        cache, "ensure_tile", _recording_ensure_tile(copies, threading.Lock())
    )

    source = cache.source_sql(THEME, glob, ONE_TILE_BBOX)

    # A local fixture path has no release manifest, so the upstream read is
    # the plain glob; against the public bucket it is the manifest-pruned
    # file list (manifest.pruned_source_sql), never a local tile.
    assert source == f"read_parquet({db._sql_str(glob)}, hive_partitioning=1)"
    assert copies == [], "no tile COPY may run inline on the default path"
    expected = cache.tiles_for_bbox(*ONE_TILE_BBOX, tile_deg=cache.tile_deg_for(THEME))
    assert [t for _, t, _ in scheduled] == expected
    assert all(theme_ == THEME for theme_, _, _ in scheduled)
    # Background fetches go through the same cursor factory as before, so
    # they share the shared instance's metadata cache, not the caller's
    # connection.
    assert all(factory is db.new_connection for _, _, factory in scheduled)


@pytest.mark.parametrize("env_var", ["PLACEROOT_INLINE_TILE_COPY", "PLACEROOT_CACHE_SYNC"])
def test_env_switch_restores_inline_copy_off_the_lock(offline, monkeypatch, env_var):
    """PLACEROOT_INLINE_TILE_COPY=1 (and the older PLACEROOT_CACHE_SYNC)
    COPY inline before answering — on this thread, but never while this
    thread holds db.conn_lock."""
    monkeypatch.setenv(env_var, "1")
    glob = str(FIXTURE_PATH)
    scheduled = []
    monkeypatch.setattr(
        cache, "_materialize_in_background", lambda *a, **k: scheduled.append(a)
    )
    copies = []
    monkeypatch.setattr(
        cache,
        "ensure_tile",
        _recording_ensure_tile(copies, threading.Lock(), create=True),
    )

    source = cache.source_sql(THEME, glob, ONE_TILE_BBOX)

    expected = cache.tiles_for_bbox(*ONE_TILE_BBOX, tile_deg=cache.tile_deg_for(THEME))
    assert [t for t, _ in copies] == expected
    assert copies[0][1] == threading.get_ident(), "inline means this thread waits"
    assert scheduled == []
    rel = release.resolve_release()
    fp = cache.resolve_fingerprint(rel, THEME, glob)
    path = cache.tile_path(rel, THEME, fp, expected[0])
    assert source == f"read_parquet([{db._sql_str(str(path))}])"


def test_inline_copy_never_runs_under_a_caller_held_conn_lock(offline, monkeypatch):
    """Even a caller that reaches source_sql while holding db.conn_lock
    (the lock is reentrant) must not have the COPY run on its thread with
    the lock held: the shared-connection lock is for queries on the shared
    connection, and a COPY on a cursor has no business under it. source_sql
    itself never takes the lock, so the only way the COPY sees the lock
    held is a caller holding it — which this test does, to pin the
    invariant for the inline path's cursors rather than its caller."""
    monkeypatch.setenv("PLACEROOT_INLINE_TILE_COPY", "1")
    glob = str(FIXTURE_PATH)
    seen_lock_held = []

    def ensure_tile(con, release_, theme_, tile_, upstream_glob_, fingerprint_=None):
        seen_lock_held.append(_conn_lock_held_by_this_thread())
        path = cache.tile_path(release_, theme_, fingerprint_, tile_)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    monkeypatch.setattr(cache, "ensure_tile", ensure_tile)
    # Without the caller holding it, the COPY thread must not hold it.
    cache.source_sql(THEME, glob, ONE_TILE_BBOX)
    assert seen_lock_held == [False]


def test_background_fetch_runs_off_conn_lock_and_dedups(offline, monkeypatch):
    """The real background path: own thread, own cursor, lock not held,
    and two queries missing the same tile start exactly one fetch."""
    glob = str(FIXTURE_PATH)
    copies = []
    monkeypatch.setattr(
        cache,
        "ensure_tile",
        _recording_ensure_tile(copies, threading.Lock(), create=True, delay=0.2),
    )
    source = cache.source_sql(THEME, glob, ONE_TILE_BBOX)
    assert source.startswith("read_parquet('")
    # Second miss on the same tile while the first fetch is in flight.
    source_again = cache.source_sql(THEME, glob, ONE_TILE_BBOX)
    assert source_again == source
    _drain_background()
    assert len(copies) == 1, "same tile, two misses, one COPY"
    assert copies[0][1] != threading.get_ident(), "the COPY ran on its own thread"
    # And the third query, now that the tile exists, reads it locally.
    local = cache.source_sql(THEME, glob, ONE_TILE_BBOX)
    assert local.startswith("read_parquet([")


def test_source_sql_does_not_take_conn_lock(offline, monkeypatch):
    """The decision itself (filesystem peek + cached probe) runs lock-free,
    so a slow COPY on one request can no longer queue every other tool
    call in the process behind db.conn_lock."""
    entered = []
    real = db.conn_lock

    class Recorder:
        def __enter__(self):
            entered.append(threading.get_ident())
            return real.__enter__()

        def __exit__(self, *exc):
            return real.__exit__(*exc)

        def acquire(self, *a, **k):
            entered.append(threading.get_ident())
            return real.acquire(*a, **k)

        def release(self):
            return real.release()

    monkeypatch.setattr(db, "conn_lock", Recorder())
    monkeypatch.setattr(cache, "_materialize_in_background", lambda *a, **k: None)
    cache.source_sql(THEME, str(FIXTURE_PATH), ONE_TILE_BBOX)
    assert entered == []


def test_wide_heavy_query_still_scans_directly(offline, monkeypatch):
    """Past HEAVY_SYNC_MAX_TILES the inline switch is ignored too: the
    overflow was always a direct scan plus background warm."""
    monkeypatch.setenv("PLACEROOT_INLINE_TILE_COPY", "1")
    scheduled = []
    monkeypatch.setattr(
        cache, "_materialize_in_background", lambda *a, **k: scheduled.append(a)
    )
    copies = []
    monkeypatch.setattr(cache, "ensure_tile", _recording_ensure_tile(copies, threading.Lock()))
    wide = (-74.5, 40.1, -74.0, 40.6)  # 5 x 5 tiles at 0.125°, under MAX_TILES_PER_QUERY
    n_tiles = len(cache.tiles_for_bbox(*wide, tile_deg=0.125))
    assert cache.HEAVY_SYNC_MAX_TILES < n_tiles <= cache.MAX_TILES_PER_QUERY
    source = cache.source_sql(THEME, str(FIXTURE_PATH), wide)
    assert source.startswith("read_parquet('")
    assert copies == []
    assert len(scheduled) == len(cache.tiles_for_bbox(*wide, tile_deg=0.125))


# --- build_graph: bbox pushdown on the direct scan ---------------------------


def test_build_graph_sql_pushes_bbox_down_on_physical_struct_columns(monkeypatch):
    """When the source is the upstream read, the only thing that keeps the
    scan to the box's row groups is the WHERE on the physical
    bbox.xmin/xmax/ymin/ymax columns — the same filter geo.py uses for
    places. A filter on a computed expression (a centroid, ST_*) would
    read every row group of every pruned file. Projection stays explicit."""
    captured = {}

    class FakeConn:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return self

        def fetchall(self):
            return []

    monkeypatch.setattr(routing, "_upstream_glob", lambda: "s3://fake/segment/*")
    monkeypatch.setattr(routing, "_check_schema", lambda glob: [])
    monkeypatch.setattr(
        db,
        "probe_schema",
        lambda glob: frozenset(routing.REQUIRED_COLUMNS) | {"subtype", "names"},
    )
    monkeypatch.setattr(routing.geo, "geom_expr", lambda glob, as_wkt=False: "ST_AsText(geometry)")
    monkeypatch.setattr(
        routing, "_from_source", lambda bbox: "read_parquet(['s3://fake/segment/part-0'])"
    )
    monkeypatch.setattr(db, "ensure_spatial", lambda: None)
    monkeypatch.setattr(db, "shared_conn", lambda: FakeConn())

    graph = routing.build_graph(35.658, 139.7016, 2300.0, mode="walk")
    assert graph.node_count() == 0

    sql = captured["sql"]
    params = captured["params"]
    xmin, ymin, xmax, ymax = routing._bbox_around(35.658, 139.7016, 2300.0)
    assert (
        "bbox.xmax >= $xmin AND bbox.xmin <= $xmax"
        " AND bbox.ymax >= $ymin AND bbox.ymin <= $ymax"
    ) in sql
    assert params["xmin"] == xmin and params["xmax"] == xmax
    assert params["ymin"] == ymin and params["ymax"] == ymax
    assert "SELECT *" not in sql
    assert "FROM read_parquet(['s3://fake/segment/part-0'])" in sql
    # The pushdown filter is the first WHERE predicate, directly on the
    # struct fields, not wrapped in a function or arithmetic.
    where = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert where.strip().startswith("bbox.xmax >= $xmin")
    for col in ("id", "class", "connectors", "speed_limits", "access_restrictions", "names"):
        assert col in sql.split("FROM", 1)[0]


# --- widen-and-retry: the retry is not padded a second time -----------------


def test_retry_extraction_is_not_padded_again(monkeypatch):
    """The first attempt pads by GRAPH_CACHE_MARGIN (1.3) so nearby repeats
    hit; the retry is already 1.6x wider — padding it again (2.08x) only
    makes the failure path's second extraction 4.3x the base area."""
    routing.clear_graph_cache()
    built = []

    def fake_build_graph(lat, lon, radius_m, **kwargs):
        built.append(radius_m)
        return routing.Graph()

    monkeypatch.setattr(routing, "build_graph", fake_build_graph)
    monkeypatch.setattr(routing, "_load_graph_from_disk", lambda *a, **k: None)
    monkeypatch.setattr(routing, "_persist_graph_to_disk", lambda *a, **k: None)
    monkeypatch.setattr(routing, "_upstream_glob", lambda: "s3://fake/segment/*")

    lat, lon = 12.345, 67.891  # nowhere another test caches a graph
    routing._get_or_build_graph(lat, lon, 1000.0, "walk", None)
    routing._get_or_build_graph(lat, lon, 1600.0, "walk", None, pad=False)
    assert built == [pytest.approx(1300.0), pytest.approx(1600.0)]
    routing.clear_graph_cache()


def test_shortest_path_retry_reuses_margin_only_on_first_attempt(monkeypatch):
    """_shortest_path's loop: first radius padded, the 1.6x retry not —
    and the retry really is wider than the first attempt's padding, which
    is the whole reason it cannot (and must not pretend to) reuse it."""
    calls = []

    def fake_get_or_build(lat, lon, radius_m, mode, speed_m_s=None, **kwargs):
        calls.append((radius_m, kwargs.get("pad", True)))
        return routing.Graph()  # empty: forces the retry, then NoGraphNearby

    monkeypatch.setattr(routing, "_get_or_build_graph", fake_get_or_build)
    with pytest.raises(routing.NoGraphNearby):
        routing._shortest_path(35.658, 139.7016, 35.6717, 139.6949, "walk")
    assert len(calls) == 2
    (first_r, first_pad), (retry_r, retry_pad) = calls
    assert first_pad is True and retry_pad is False
    assert retry_r == pytest.approx(first_r * routing.ROUTE_RADIUS_RETRY_FACTOR)
    assert routing.ROUTE_RADIUS_RETRY_FACTOR > routing.GRAPH_CACHE_MARGIN
