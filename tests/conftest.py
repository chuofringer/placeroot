from pathlib import Path

import duckdb
import pytest

from placeroot import buildings, overture, release, routing

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "places.parquet"
# type=division_area (polygons; consumed by divisions.py's admin_lookup) and
# type=division (points + hierarchy chain; consumed by geocode.py) are two
# different Overture types under the same "divisions" theme — see
# overture.py's _override_key for how both fixtures stay active at once.
DIVISION_AREAS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "division_areas.parquet"
DIVISIONS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "divisions.parquet"
ADDRESSES_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "addresses.parquet"
TRANSPORTATION_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "transportation.parquet"
BUILDINGS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "buildings.parquet"

CENTER_LAT = 40.700000
CENTER_LON = -73.900000


@pytest.fixture(autouse=True)
def offline_data(request, monkeypatch):
    """Point the query layer at the committed fixtures for every test except @live ones.

    Also pins the release (so no test triggers real release discovery) and
    disables the tile cache by default (so existing tests keep querying the
    fixture directly) — cache-specific tests opt back in explicitly. places,
    divisions (#11, both its types), and addresses (#10) are each pointed at
    their own committed fixture via overture.set_data_path's per-theme (and,
    for divisions, per-type) override.
    """
    if "live" in request.keywords:
        yield
        return
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", release.PINNED_RELEASE)
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    overture.set_data_path(str(FIXTURE_PATH))
    # Bare theme override: what divisions.py's admin_lookup (type_="division_area")
    # resolves to, and what every existing divisions test already expects.
    overture.set_data_path(str(DIVISION_AREAS_FIXTURE_PATH), theme="divisions")
    # More specific type_ override: only geocode.py's type_="division" queries
    # resolve to this one; admin_lookup's lookups fall through to the bare
    # override above instead.
    overture.set_data_path(str(DIVISIONS_FIXTURE_PATH), theme="divisions", type_="division")
    overture.set_data_path(str(ADDRESSES_FIXTURE_PATH), theme="addresses", type_="address")
    routing.set_data_path(str(TRANSPORTATION_FIXTURE_PATH))
    buildings.set_data_path(str(BUILDINGS_FIXTURE_PATH))
    # The in-memory built-graph cache (#39) is a module-level global that
    # would otherwise leak a graph built against one test's data/mode/radius
    # into the next test — clear it so every test starts from a cold cache,
    # which the cache-specific tests rely on to count extractions accurately.
    routing.clear_graph_cache()
    try:
        yield
    finally:
        overture.set_data_path(None)
        overture.set_data_path(None, theme="divisions")
        overture.set_data_path(None, theme="divisions", type_="division")
        overture.set_data_path(None, theme="addresses", type_="address")
        routing.set_data_path(None)
        buildings.set_data_path(None)
        routing.clear_graph_cache()


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
