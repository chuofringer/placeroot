"""Progress narration for slow queries (progress.py, server._progress_middleware).

A cold query may spend tens of seconds fetching data; these tests pin the
contract that makes that visible instead of a silent spinner: the reporter
is request-scoped and optional, it can never fail the query, the cache
layer's phase boundaries feed it, and the middleware only wires it up when
the client asked for progress (progressToken).
"""

import asyncio
import threading
import time

import duckdb
import pytest

from placeroot import cache, overture, progress, release, server

from .conftest import CENTER_LAT, CENTER_LON


@pytest.fixture
def captured():
    """Install a capturing reporter for the test; restore after."""
    events = []
    token = progress.set_reporter(lambda m, c, t: events.append((m, c, t)))
    try:
        yield events
    finally:
        progress.reset(token)


# --- the reporter contract ---------------------------------------------------


def test_report_is_a_noop_without_a_reporter():
    progress.report("nobody listening")  # must not raise


def test_report_reaches_the_installed_reporter(captured):
    progress.report("phase one", 1, 4)
    assert captured == [("phase one", 1, 4)]


def test_a_raising_reporter_never_fails_the_query_path():
    token = progress.set_reporter(lambda m, c, t: 1 / 0)
    try:
        progress.report("boom")  # swallowed
    finally:
        progress.reset(token)


def test_repeat_messages_are_throttled_but_new_phases_are_not(captured, monkeypatch):
    progress.report("same phase", 1, 10)
    progress.report("same phase", 2, 10)  # inside MIN_INTERVAL_S: dropped
    progress.report("new phase")          # different message: immediate
    assert [e[0] for e in captured] == ["same phase", "new phase"]


# --- the cache layer narrates its slow phases --------------------------------


def test_sync_materialization_reports_per_tile(captured, tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.setenv("PLACEROOT_CACHE_SYNC", "1")
    con = duckdb.connect()
    bbox = (CENTER_LON - 0.01, CENTER_LAT - 0.01, CENTER_LON + 0.01, CENTER_LAT + 0.01)
    paths = cache.local_paths_for_query(
        con, release.resolve_release(), "places", bbox, overture._upstream_glob(),
        duckdb.connect,
    )
    assert paths  # materialized from the fixture
    messages = [m for m, _, _ in captured]
    assert any("Fetching map data" in m for m in messages)
    assert any("cached" in m for m in messages)


def test_async_fallback_reports_the_direct_scan(captured, tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)
    # The call schedules real background fetch threads; stub the COPY so a
    # thread outliving this test can't write a fixture-derived tile into
    # the developer's real cache dir after the env monkeypatch is undone.
    monkeypatch.setattr(cache, "ensure_tile", lambda *a, **k: None)
    con = duckdb.connect()
    bbox = (CENTER_LON - 0.01, CENTER_LAT - 0.01, CENTER_LON + 0.01, CENTER_LAT + 0.01)
    paths = cache.local_paths_for_query(
        con, release.resolve_release(), "places", bbox, overture._upstream_glob(),
        duckdb.connect,
    )
    assert paths is None  # caller falls back to a direct scan
    assert any("direct scan" in m for m, _, _ in captured)
    # Wait for the spawned background fetches to run their (stubbed) COPY
    # before monkeypatch teardown restores the real ensure_tile and cache
    # dir — a parked daemon thread resolving them late would otherwise
    # write a real tile into the real user cache.
    deadline = time.monotonic() + 10
    while cache._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not cache._inflight, "background fetches still pending at teardown"


# --- background fetches are bounded ------------------------------------------


def test_background_fetches_respect_the_concurrency_bound(tmp_path, monkeypatch):
    """One cold query must not spawn one COPY per tile all at once: the
    answering scan shares the pipe with them (the measured starvation bug)."""
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    running, peak = [], []
    lock = threading.Lock()
    done = threading.Event()
    total = 6

    def fake_ensure(con, rel, theme, tile, glob, fingerprint=None):
        with lock:
            running.append(tile)
            peak.append(len(running))
        time.sleep(0.05)
        with lock:
            running.remove(tile)
            if len(peak) >= total and not running:
                done.set()

    monkeypatch.setattr(cache, "ensure_tile", fake_ensure)
    for i in range(total):
        cache._materialize_in_background(
            "r", "places", (i, i), "glob", "fp", lambda: None
        )
    assert done.wait(timeout=10), "background fetches never completed"
    assert max(peak) <= cache._background_fetch_concurrency()


def test_fetch_concurrency_env_parsing(monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_FETCH_CONCURRENCY", "4")
    assert cache._background_fetch_concurrency() == 4
    monkeypatch.setenv("PLACEROOT_CACHE_FETCH_CONCURRENCY", "0")
    assert cache._background_fetch_concurrency() == 1
    monkeypatch.setenv("PLACEROOT_CACHE_FETCH_CONCURRENCY", "nope")
    assert cache._background_fetch_concurrency() == 2


# --- the middleware wires it to MCP ------------------------------------------


class _FakeSession:
    def __init__(self):
        self.sent = []

    async def send_progress_notification(self, token, prog, total=None,
                                         message=None, related_request_id=None):
        self.sent.append((token, prog, total, message))


class _Ctx:
    def __init__(self, method, meta):
        self.method = method
        self.meta = meta
        self.session = _FakeSession()
        self.request_id = "req-1"


def test_middleware_installs_a_reporter_when_a_token_is_present():
    ctx = _Ctx("tools/call", {"progress_token": "tok"})

    async def call_next(c):
        progress.report("slow phase", 1, 2)
        progress.report("next phase", 1, 4)  # counts reset between phases...
        await asyncio.sleep(0.05)  # let run_coroutine_threadsafe land
        return "ok"

    async def run():
        return await server._progress_middleware(ctx, call_next)

    assert asyncio.run(run()) == "ok"
    # ...but the wire values are a per-request monotonic sequence (the spec
    # requires progress to increase per notification); the human-facing
    # counts travel in the message.
    assert ctx.session.sent == [
        ("tok", 1, None, "slow phase"),
        ("tok", 2, None, "next phase"),
    ]
    assert progress._reporter.get() is None  # reset after the call


def test_middleware_is_inert_without_a_token():
    ctx = _Ctx("tools/call", None)

    async def call_next(c):
        assert progress._reporter.get() is None
        return "ok"

    assert asyncio.run(server._progress_middleware(ctx, call_next)) == "ok"
    assert ctx.session.sent == []


def test_middleware_ignores_non_tool_requests():
    ctx = _Ctx("resources/read", {"progress_token": "tok"})

    async def call_next(c):
        assert progress._reporter.get() is None
        return "ok"

    assert asyncio.run(server._progress_middleware(ctx, call_next)) == "ok"


# --- honest ETAs (issue #314) ------------------------------------------------


def test_format_eta_is_a_range_not_a_countdown():
    assert progress.format_eta(8, 15) == "about 8–15 seconds"
    assert progress.format_eta(5, 5) == "about 5 seconds"
    assert progress.format_eta(1, 1) == "about 1 second"
    assert progress.format_eta(45, 180) == "about 1–3 minutes"
    assert progress.format_eta(0, 0) == "about a second"


def test_tile_eta_scales_with_missing_tiles_and_two_workers():
    lo, hi = progress.tile_eta("places", 1)
    assert (lo, hi) == (3.0, 8.0)
    # 4 tiles / 2 workers = 2 waves
    lo4, hi4 = progress.tile_eta("places", 4)
    assert (lo4, hi4) == (6.0, 16.0)
    assert progress.tile_eta("places", 0) == (0.0, 0.0)


def test_report_appends_eta_when_given(captured):
    progress.report("Fetching map data", 0, 2, eta_s=(3, 8))
    assert captured == [("Fetching map data — about 3–8 seconds", 0, 2)]


def test_sync_materialization_messages_carry_an_eta(captured, tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.setenv("PLACEROOT_CACHE_SYNC", "1")
    con = duckdb.connect()
    bbox = (CENTER_LON - 0.01, CENTER_LAT - 0.01, CENTER_LON + 0.01, CENTER_LAT + 0.01)
    cache.local_paths_for_query(
        con, release.resolve_release(), "places", bbox, overture._upstream_glob(),
        duckdb.connect,
    )
    messages = [m for m, _, _ in captured]
    assert any("about" in m and "second" in m for m in messages)
