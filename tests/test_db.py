"""db.probe_schema caching behavior (#144)."""

import duckdb

from placeroot import db

from .conftest import FIXTURE_PATH


def test_probe_schema_retries_a_transient_failure_after_the_memo_expires():
    """#144's concern, updated for the failure memo: a probe that fails
    transiently must not blind degraded_fields()/schema-drift detection for
    the whole *process* — but it IS memoized for PROBE_FAILURE_RETRY_S so a
    degraded deployment doesn't pay a network timeout and a warning on
    every call. Once the memo expires the next call retries and heals. A
    successful probe is still cached (LRU) so repeats don't re-read.
    """
    db._probe_schema_cached.cache_clear()
    db._probe_failed_at.clear()
    glob = str(FIXTURE_PATH)  # a real, readable parquet

    real_shared_conn = db.shared_conn
    calls = {"n": 0}

    def flaky_shared_conn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise duckdb.Error("simulated transient probe failure")
        return real_shared_conn()

    db.shared_conn = flaky_shared_conn
    try:
        # 1st probe fails transiently -> None, memoized.
        assert db.probe_schema(glob) is None
        # Within the memo window the failure is served without re-probing.
        assert db.probe_schema(glob) is None
        assert calls["n"] == 1
        # Expire the memo: the next probe must RETRY and get the real schema.
        db._probe_failed_at[glob] -= db.PROBE_FAILURE_RETRY_S + 1
        schema = db.probe_schema(glob)
        assert schema is not None
        assert len(schema) > 0
        assert glob not in db._probe_failed_at  # success clears the memo
        # Another probe is a cache hit on the success -> no new shared_conn() call.
        again = db.probe_schema(glob)
        assert again == schema
        assert calls["n"] == 2  # only the failure + the one successful read
    finally:
        db.shared_conn = real_shared_conn
        db._probe_schema_cached.cache_clear()
        db._probe_failed_at.clear()
