"""db.probe_schema caching behavior (#144)."""

import duckdb
import pytest

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


# --- Connection configuration (_configure) ---------------------------------


def _httpfs_loadable() -> bool:
    """True if DuckDB can LOAD httpfs locally (installed earlier, or
    pre-installed). Settings like http_timeout only exist once it is."""
    try:
        duckdb.connect().execute("LOAD httpfs;")
        return True
    except duckdb.Error:
        return False


needs_httpfs = pytest.mark.skipif(
    not _httpfs_loadable(), reason="DuckDB httpfs extension not installed locally"
)


class _FakeConn:
    """Records executed statements; LOAD fails until an INSTALL has run."""

    def __init__(self, installed: bool):
        self.installed = installed
        self.statements: list[str] = []

    def execute(self, sql: str):
        self.statements.append(sql)
        if sql.startswith("INSTALL "):
            self.installed = True
            rest = sql.split(";", 1)[1].strip()
            if rest:
                self.execute(rest)
        elif sql.startswith("LOAD ") and not self.installed:
            raise duckdb.IOException("Extension not found. Install it first")
        return self


def test_load_extension_does_not_install_when_load_succeeds():
    """`INSTALL x` phones home even when x is already present, so an
    already-installed extension must be LOADed without it (offline hosts)."""
    con = _FakeConn(installed=True)
    db.load_extension(con, "spatial")
    assert con.statements == ["LOAD spatial;"]


def test_load_extension_installs_only_when_load_fails():
    con = _FakeConn(installed=False)
    db.load_extension(con, "httpfs")
    assert con.statements[0] == "LOAD httpfs;"
    assert any(s.startswith("INSTALL httpfs;") for s in con.statements)
    assert con.statements[-1] == "LOAD httpfs;"


def test_load_extension_surfaces_duckdbs_own_error_when_install_fails_too():
    class _Never(_FakeConn):
        def execute(self, sql):
            self.statements.append(sql)
            raise duckdb.IOException(f"simulated failure for {sql}")

    con = _Never(installed=False)
    with pytest.raises(duckdb.Error, match="INSTALL httpfs"):
        db.load_extension(con, "httpfs")


def test_extension_directory_env_is_applied_before_the_first_load(monkeypatch, tmp_path):
    """PLACEROOT_DUCKDB_EXTENSION_DIR points LOAD at a pre-populated
    directory, so it has to be SET before anything is loaded."""
    ext_dir = tmp_path / "ext"
    monkeypatch.setenv("PLACEROOT_DUCKDB_EXTENSION_DIR", str(ext_dir))
    con = _FakeConn(installed=True)
    try:
        db._configure(con)
    except duckdb.Error:
        pass  # later SETs may not be understood by the fake; irrelevant here
    assert con.statements[0] == f"SET extension_directory='{ext_dir}';"
    assert con.statements[1] == "LOAD httpfs;"


def test_no_extension_directory_env_leaves_duckdbs_default(monkeypatch):
    monkeypatch.delenv("PLACEROOT_DUCKDB_EXTENSION_DIR", raising=False)
    con = _FakeConn(installed=True)
    db._configure(con)
    assert con.statements[0] == "LOAD httpfs;"
    assert not any("extension_directory" in s for s in con.statements)
