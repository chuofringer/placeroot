from pathlib import Path

import duckdb
import pytest

from placeroot import overture, release

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "places.parquet"
DIVISIONS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "divisions.parquet"

CENTER_LAT = 40.700000
CENTER_LON = -73.900000


@pytest.fixture(autouse=True)
def offline_data(request, monkeypatch):
    """Point the query layer at the committed fixtures for every test except @live ones.

    Also pins the release (so no test triggers real release discovery) and
    disables the tile cache by default (so existing tests keep querying the
    fixture directly) — cache-specific tests opt back in explicitly. Both
    the places and divisions (#11) themes are pointed at their own
    committed fixture via overture.set_data_path's per-theme override.
    """
    if "live" in request.keywords:
        yield
        return
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", release.PINNED_RELEASE)
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    overture.set_data_path(str(FIXTURE_PATH))
    overture.set_data_path(str(DIVISIONS_FIXTURE_PATH), theme="divisions")
    try:
        yield
    finally:
        overture.set_data_path(None)
        overture.set_data_path(None, theme="divisions")


def raw_rows() -> list[dict]:
    """Every fixture row, read independently of overture.py's query logic.

    Ground truth for tests that check counts/filters against the query layer.
    """
    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT id, names.primary AS name, basic_category, taxonomy.primary AS category,
               bbox.ymin AS lat, bbox.xmin AS lon
        FROM read_parquet('{FIXTURE_PATH}')
    """).fetchall()
    cols = ["id", "name", "basic_category", "category", "lat", "lon"]
    return [dict(zip(cols, r)) for r in rows]
