"""overture.warm_metadata: the startup pre-warm of parquet footers."""

import duckdb
import pytest

from placeroot import db, overture


class _FakeCursor:
    def __init__(self, fail: bool):
        self.fail = fail
        self.closed = False
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, sql):
        self.executed.append(sql)
        if self.fail:
            raise duckdb.IOException("unreachable")
        return self


@pytest.mark.parametrize("fail", [False, True])
def test_warm_metadata_closes_every_cursor_it_opens(monkeypatch, fail):
    """One cursor per theme, and each is closed whether the probe succeeded
    or raised — the warm lives in the shared instance's metadata cache,
    so an open cursor per theme per startup was a plain leak."""
    created: list[_FakeCursor] = []

    def new_connection():
        cur = _FakeCursor(fail)
        created.append(cur)
        return cur

    monkeypatch.setattr(db, "new_connection", new_connection)
    monkeypatch.setattr(overture, "dataset_is_pinned", lambda *a, **k: False)
    monkeypatch.setattr(overture, "_upstream_glob", lambda theme, type_: f"s3://b/{theme}/*")

    overture.warm_metadata()

    assert len(created) == len(overture._WARM_THEMES)
    assert all(cur.closed for cur in created)
    assert all(len(cur.executed) == 1 for cur in created)
