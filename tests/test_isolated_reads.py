"""Read-path concurrency: pooled cursors instead of the global conn_lock.

Everything here runs offline on a plain in-memory DuckDB instance (no
httpfs, no spatial): the instance is swapped in as db._instance, so the
real pool, lease and settings code paths are the ones under test.
"""

import os
import threading
import time

import duckdb
import pytest

from placeroot import db


@pytest.fixture
def instance(monkeypatch):
    inst = duckdb.connect()
    inst.execute("SET enable_progress_bar=false;")
    monkeypatch.setattr(db, "_instance", inst)
    monkeypatch.setattr(db, "_read_pool_obj", None)
    yield inst
    pool = db._read_pool_obj
    if pool is not None:
        pool.close()
    inst.close()


def _join(threads, timeout=30):
    for t in threads:
        t.join(timeout)
        assert not t.is_alive(), "thread hung"


# --- reads do not wait on each other -----------------------------------------


def test_read_conn_does_not_take_conn_lock(instance):
    """A reader proceeds while another thread holds the global lock."""
    got = {}
    with db.conn_lock:

        def reader():
            with db.read_conn() as con:
                got["v"] = con.execute("SELECT 41 + 1").fetchone()[0]

        t = threading.Thread(target=reader)
        t.start()
        _join([t], timeout=10)
    assert got["v"] == 42


def test_a_fast_read_finishes_while_a_slow_read_is_still_running(instance):
    """Deterministic: the slow reader parks inside its lease until the fast
    one has come and gone. With a shared lock this would deadlock."""
    slow_inside = threading.Event()
    fast_done = threading.Event()
    order = []

    def slow():
        with db.read_conn() as con:
            con.execute("SELECT count(*) FROM range(10)").fetchall()
            slow_inside.set()
            assert fast_done.wait(10), "fast read never ran while slow was leased"
            order.append("slow")

    def fast():
        assert slow_inside.wait(10)
        with db.read_conn() as con:
            order.append(con.execute("SELECT 7").fetchone()[0])
        fast_done.set()

    threads = [threading.Thread(target=slow), threading.Thread(target=fast)]
    for t in threads:
        t.start()
    _join(threads)
    assert order == [7, "slow"]


def test_slow_scan_does_not_hold_up_a_fast_query_timing(instance):
    """Timing version with generous margins: a multi-second scan on one
    thread, a near-instant query on another. The fast one must finish well
    before the slow one does; behind a shared lock it would finish last."""
    times = {}
    slow_text = "SELECT sum(sin(i::DOUBLE)) FROM range(120000000) t(i)"

    def slow():
        start = time.monotonic()
        with db.read_conn() as con:
            con.execute(slow_text).fetchall()
        times["slow_end"] = time.monotonic() - start

    def fast():
        time.sleep(0.2)  # let the slow query get going first
        start = time.monotonic()
        with db.read_conn() as con:
            con.execute("SELECT 1").fetchall()
        times["fast_end"] = time.monotonic() - start

    threads = [threading.Thread(target=slow), threading.Thread(target=fast)]
    for t in threads:
        t.start()
    _join(threads, timeout=120)
    assert times["slow_end"] > 1.0  # the slow side really was slow
    assert times["fast_end"] < times["slow_end"] / 2


# --- the pool cap -----------------------------------------------------------


def test_pool_cap_bounds_concurrent_leases_and_live_cursors(instance):
    pool = db.CursorPool(instance, cap=2)
    lock = threading.Lock()
    inside = 0
    peak = 0
    release = threading.Event()
    full = threading.Event()

    def worker():
        nonlocal inside, peak
        with pool.lease() as cur:
            with lock:
                inside += 1
                peak = max(peak, inside)
                if inside == 2:
                    full.set()
            cur.execute("SELECT 1").fetchall()
            release.wait(10)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    # Two workers hold the two slots; the other two must be parked.
    assert full.wait(10)
    time.sleep(0.2)
    with lock:
        assert inside == 2
    assert pool.live <= 2
    release.set()
    _join(threads)
    assert peak == 2
    assert pool.created <= 2
    pool.close()
    assert pool.live == 0


def test_a_third_lease_waits_until_a_slot_is_returned(instance):
    pool = db.CursorPool(instance, cap=1)
    first_in = threading.Event()
    waited = {}

    def holder():
        with pool.lease():
            first_in.set()
            time.sleep(0.3)

    def waiter():
        assert first_in.wait(10)
        start = time.monotonic()
        with pool.lease():
            waited["s"] = time.monotonic() - start

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter)]
    for t in threads:
        t.start()
    _join(threads)
    assert waited["s"] >= 0.15  # blocked until the holder returned its cursor
    assert pool.created == 1  # the one cursor was reused, not a second one


def test_nested_read_conn_reuses_the_thread_cursor_at_cap_one(instance, monkeypatch):
    """A nested read on the same thread must not lease a second slot, or a
    cap of one would deadlock the thread against itself."""
    monkeypatch.setenv(db.READ_CURSORS_ENV, "1")
    monkeypatch.setattr(db, "_read_pool_obj", None)
    with db.read_conn() as outer:
        with db.read_conn() as inner:
            assert inner is outer
            assert inner.execute("SELECT 3").fetchone()[0] == 3


def test_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv(db.READ_CURSORS_ENV, raising=False)
    assert db._read_cursor_cap() == db.DEFAULT_READ_CURSORS == 8
    monkeypatch.setenv(db.READ_CURSORS_ENV, "3")
    assert db._read_cursor_cap() == 3
    monkeypatch.setenv(db.READ_CURSORS_ENV, "0")
    assert db._read_cursor_cap() == 1
    monkeypatch.setenv(db.READ_CURSORS_ENV, "many")
    assert db._read_cursor_cap() == db.DEFAULT_READ_CURSORS


# --- no leaks ---------------------------------------------------------------


def test_no_cursor_leak_after_many_failing_reads(instance):
    pool = db.CursorPool(instance, cap=3)
    peak_live = 0
    for i in range(200):
        try:
            with pool.lease() as cur:
                peak_live = max(peak_live, pool.live)
                if i % 2:
                    cur.execute("SELECT no_such_column FROM range(1)")
                else:
                    raise ValueError("boom")
        except (duckdb.Error, ValueError):
            pass
    assert pool.live == 0, "failed leases must close their cursor"
    assert peak_live <= 3
    # Every slot is free again: the cap was not eaten by the failures.
    held = []

    def take():
        with pool.lease():
            held.append(1)
            time.sleep(0.05)

    threads = [threading.Thread(target=take) for _ in range(3)]
    for t in threads:
        t.start()
    _join(threads)
    assert len(held) == 3


def test_successful_reads_reuse_one_idle_cursor(instance):
    pool = db.CursorPool(instance, cap=4)
    for _ in range(100):
        with pool.lease() as cur:
            cur.execute("SELECT 1").fetchall()
    assert pool.created == 1
    assert pool.live == 1
    pool.close()
    assert pool.live == 0


def test_read_conn_nested_and_exception_path_restores_state(instance):
    with pytest.raises(ZeroDivisionError):
        with db.read_conn():
            raise ZeroDivisionError
    # The thread-local slot was cleared on the way out, so a fresh read works.
    assert getattr(db._reading, "conn", None) is None
    with db.read_conn() as con:
        assert con.execute("SELECT 5").fetchone()[0] == 5


def test_closing_the_pool_closes_cursors_returned_after_it(instance):
    pool = db.CursorPool(instance, cap=2)
    with pool.lease() as cur:
        pool.close()
        assert pool.live == 1  # still leased, so not closed yet
    assert pool.live == 0
    with pytest.raises(duckdb.Error):
        cur.execute("SELECT 1")


# --- settings and extensions on the cursors ----------------------------------


class _NoReadsInstance:
    """Stands in for the instance: cursor() works, execute() must never run."""

    def __init__(self, inst):
        self._inst = inst

    def cursor(self):
        return self._inst.cursor()

    def execute(self, *args, **kwargs):
        raise AssertionError("cursor creation read the instance")


def test_cursor_creation_never_reads_the_instance(instance):
    """Cursors are created from background threads that do not hold
    conn_lock; reading the instance there would race its execute()."""
    snapshot = db._instance_settings(instance)
    pool = db.CursorPool(_NoReadsInstance(instance), cap=2, settings=snapshot)
    with pool.lease() as cur:
        assert cur.execute("SELECT 1").fetchone()[0] == 1
    pool.close()


def test_settings_set_before_creation_are_seen_by_pooled_cursors(instance):
    instance.execute("SET threads=3;")
    instance.execute("SET TimeZone='Asia/Tokyo';")  # session-scoped (LOCAL)
    with db.read_conn() as con:
        rows = dict(
            con.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN ('threads', 'TimeZone')"
            ).fetchall()
        )
        tz = con.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    assert rows["threads"] == "3"
    assert rows["TimeZone"] == "Asia/Tokyo"
    assert tz == "Asia/Tokyo"


def test_cursor_settings_match_the_instance_after_configure(instance):
    """duckdb_settings() on a pooled cursor agrees with the configured
    instance, for every setting _configure sets. Needs httpfs for the S3
    half, so offline it skips here and runs in CI."""
    try:
        db._configure(instance)
    except duckdb.Error as e:
        pytest.skip(f"httpfs unavailable offline: {str(e).splitlines()[0]}")
    names = (
        "threads",
        "enable_progress_bar",
        "temp_directory",
        "enable_http_metadata_cache",
        "s3_region",
        "http_timeout",
    )
    query = (
        "SELECT name, value FROM duckdb_settings() WHERE name IN ("
        + ", ".join(f"'{n}'" for n in names)
        + ")"
    )
    want = dict(instance.execute(query).fetchall())
    with db.read_conn() as con:
        got = dict(con.execute(query).fetchall())
    assert got == want
    assert got["threads"] == str(max(1, int(os.environ.get("PLACEROOT_DUCKDB_THREADS", 96))))


def test_extension_option_set_on_the_instance_reaches_pooled_cursors(instance, monkeypatch):
    """icu's Calendar is an extension option, reported GLOBAL, that a cursor
    does not inherit by itself; _open_cursor must carry it over."""
    monkeypatch.setattr(db, "_loaded_extensions", set())
    db._ensure_extension("icu")
    instance.execute("SET Calendar='japanese';")
    with db.read_conn() as con:
        assert con.execute("SELECT current_setting('Calendar')").fetchone()[0] == "japanese"


def test_extension_loads_once_and_later_cursors_see_its_functions(instance, monkeypatch):
    loads = []
    real_load = db.load_extension

    def counting_load(con, name):
        loads.append(name)
        real_load(con, name)

    monkeypatch.setattr(db, "load_extension", counting_load)
    monkeypatch.setattr(db, "_loaded_extensions", set())
    db._ensure_extension("icu")
    db._ensure_extension("icu")
    assert loads == ["icu"]
    # Created after the LOAD, so it sees the extension's functions.
    with db.read_conn() as con:
        assert con.execute("SELECT icu_sort_key('a', 'en') IS NOT NULL").fetchone()[0]


def test_ensure_spatial_then_cursor_sees_st_functions(instance, monkeypatch):
    monkeypatch.setattr(db, "_loaded_extensions", set())
    try:
        db.ensure_spatial()
    except duckdb.Error as e:
        pytest.skip(f"spatial extension unavailable offline: {str(e).splitlines()[0]}")
    with db.read_conn() as con:
        assert con.execute("SELECT ST_AsText(ST_Point(1, 2))").fetchone()[0] == "POINT (1 2)"


# --- the exclusive path keeps its lock -------------------------------------


def test_conn_lock_still_serializes_shared_conn_writers(instance):
    holding = threading.Event()
    release = threading.Event()

    def writer():
        with db.conn_lock:
            holding.set()
            release.wait(10)

    t = threading.Thread(target=writer)
    t.start()
    assert holding.wait(10)
    try:
        # Another exclusive caller is kept out...
        assert db.conn_lock.acquire(timeout=0.2) is False
        # ...while a read is not.
        with db.read_conn() as con:
            assert con.execute("SELECT 9").fetchone()[0] == 9
    finally:
        release.set()
        _join([t])


def test_exclusive_writes_from_two_threads_do_not_overlap(instance):
    inside = 0
    peak = 0
    guard = threading.Lock()

    def writer():
        nonlocal inside, peak
        for _ in range(5):
            with db.conn_lock:
                with guard:
                    inside += 1
                    peak = max(peak, inside)
                db.shared_conn().execute("SELECT count(*) FROM range(1000)").fetchall()
                with guard:
                    inside -= 1

    threads = [threading.Thread(target=writer) for _ in range(3)]
    for t in threads:
        t.start()
    _join(threads)
    assert peak == 1


# --- overrides that already reroute queries keep working ---------------------


def test_read_conn_yields_the_isolated_cursor_inside_isolated_reads(instance):
    with db.isolated_reads():
        private = db._isolation.conn
        with db.read_conn() as con:
            assert con is private


def test_read_conn_honours_a_replaced_shared_conn(instance, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(db, "shared_conn", lambda: sentinel)
    with db.read_conn() as con:
        assert con is sentinel


def test_shared_conn_is_unchanged_for_existing_callers(instance):
    assert db.shared_conn() is instance
    with db.isolated_reads():
        assert db.shared_conn() is db._isolation.conn


def test_probe_schema_runs_on_the_read_path(instance, monkeypatch, tmp_path):
    """The schema probe must not need conn_lock: it is a LIMIT 0 read."""
    path = tmp_path / "t.parquet"
    instance.execute(f"COPY (SELECT 1 AS a, 'x' AS b) TO '{path}' (FORMAT PARQUET)")
    db._probe_schema_cached.cache_clear()
    got = {}
    with db.conn_lock:

        def probe():
            got["cols"] = db._probe_schema_cached(str(path))

        t = threading.Thread(target=probe)
        t.start()
        _join([t], timeout=10)
    assert got["cols"] == frozenset({"a", "b"})
    db._probe_schema_cached.cache_clear()
