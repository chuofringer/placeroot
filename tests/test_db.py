"""db.probe_schema caching behavior (#144)."""

import duckdb

from placeroot import db

from .conftest import FIXTURE_PATH


def test_probe_schema_does_not_cache_a_transient_failure():
    """#144: a probe that fails transiently must NOT be memoized — the next
    call has to retry, or a single blip permanently blinds degraded_fields()
    and the tile-cache schema-drift detection for the whole process. A
    successful probe is still cached (LRU) so repeats don't re-read.
    """
    db._probe_schema_cached.cache_clear()
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
        # 1st probe fails transiently -> None, and (the bug) must not be cached.
        assert db.probe_schema(glob) is None
        # 2nd probe must RETRY (not return a cached None) and get the real schema.
        schema = db.probe_schema(glob)
        assert schema is not None
        assert len(schema) > 0
        # 3rd probe is a cache hit on the success -> no new shared_conn() call.
        again = db.probe_schema(glob)
        assert again == schema
        assert calls["n"] == 2  # only the failure + the one successful read
    finally:
        db.shared_conn = real_shared_conn
        db._probe_schema_cached.cache_clear()
