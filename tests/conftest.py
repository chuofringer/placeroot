import os
import socket
import threading
from pathlib import Path

# Before anything imports placeroot.server: its module-level `mcp` is built
# at import time from PLACEROOT_TOOLS, so an ambient subset in the shell
# running pytest would leave `mcp` holding a partial registry and fail the
# suite's full-surface assertions — including test_tool_registry's, whose
# message would accuse a tool of a missing decorator. A fixture cannot undo
# that; it has to happen before the import. The autouse fixture below covers
# the rest of the run.
os.environ.pop("PLACEROOT_TOOLS", None)

import duckdb  # noqa: E402
import pytest  # noqa: E402

from placeroot import buildings, gers, overture, recreation, release, routing  # noqa: E402

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
def isolate_preferences(tmp_path_factory, monkeypatch):
    """Every test sees an empty preferences file, never the operator's.

    Routing tools consult local preferences for omitted mode. An ambient
    ~/.config/placeroot/preferences.json would make route/isochrone
    defaults machine-dependent.
    """
    dest = tmp_path_factory.mktemp("prefs") / "preferences.json"
    monkeypatch.setenv("PLACEROOT_PREFERENCES_PATH", str(dest))


@pytest.fixture(autouse=True)
def no_ambient_tool_selection(monkeypatch):
    """Every test sees PLACEROOT_TOOLS unset unless it sets one itself.

    Keeps build_server()'s env-reading path deterministic regardless of the
    shell the suite runs in. Autouse fixtures are set up before the test
    body runs, so a test's own monkeypatch.setenv still wins.
    """
    monkeypatch.delenv("PLACEROOT_TOOLS", raising=False)


@pytest.fixture(autouse=True)
def no_background_fetch_delay(monkeypatch):
    """Background tile fetches start immediately in tests.

    Production delays them (cache.BACKGROUND_FETCH_DELAY_S) so a fetch
    never races the query that scheduled it; tests asserting fetch
    behavior shouldn't spend wall-clock seconds waiting for that."""
    from placeroot import cache

    monkeypatch.setattr(cache, "BACKGROUND_FETCH_DELAY_S", 0.0)


@pytest.fixture(autouse=True)
def no_carried_probe_failures():
    """Every test starts with an empty probe-failure memo.

    db.probe_schema memoizes failures for PROBE_FAILURE_RETRY_S (60s —
    longer than most of the suite takes), and the suite reuses fixture
    globs across tests, so one test exercising a failure path would
    otherwise blind schema probes in every later test touching the same
    path.
    """
    from placeroot import db

    db._probe_failed_at.clear()
    yield
    db._probe_failed_at.clear()


@pytest.fixture(autouse=True)
def recreation_layer_off(monkeypatch):
    """Every test runs with the recreation layer off unless it opts in.

    The layer is on by default in production, but the fixtures the suite
    queries only cover theme=places: with the layer live, every existing
    places test would try to read theme=base from real S3 (there is no
    base-theme fixture to fall back to), and @live tests would pay for a
    second live scan they never asked for. So the default is pinned off
    here and test_recreation.py switches it back on against its own
    base-theme fixtures — which is also what keeps every other module's
    ground-truth counts stable.

    Not folded into offline_data because that one returns early for @live
    tests, which need this just as much.

    A test that wants the layer on must call recreation.set_enabled(True)
    (as test_recreation.py's layer_on fixture does): this fixture installs
    the module-level override, which enabled() checks *before* the env
    var, so monkeypatch.setenv alone cannot win. The override is reset
    afterwards either way.
    """
    monkeypatch.delenv(recreation.ENV_VAR, raising=False)
    recreation.set_enabled(False)
    yield
    recreation.set_enabled(None)


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
    # gers.py's negative cache is keyed by (release, id), and the release is
    # pinned identically for every test — so a not-found cached against one
    # test's fixtures would answer for the next test's. Start each test cold.
    gers.clear_miss_cache()
    try:
        yield
    finally:
        overture.set_data_path(None)
        overture.clear_division_geometry_cache()
        overture.set_data_path(None, theme="divisions")
        overture.set_data_path(None, theme="divisions", type_="division")
        overture.set_data_path(None, theme="addresses", type_="address")
        routing.set_data_path(None)
        buildings.set_data_path(None)
        routing.clear_graph_cache()


@pytest.fixture
def geocode_cache(tmp_path, monkeypatch):
    """Enables the #43 local divisions table (default fixtures otherwise run
    with PLACEROOT_CACHE=off, see offline_data above) at an isolated,
    per-test cache dir.

    Lives here rather than in test_geocode.py because the #221 ranking
    regression corpus (test_geocode_ranking.py) needs the same local table:
    the alternate-name search (#214) and the fuzzy tier (#215) both only run
    against it, so half the corpus is meaningless without it.
    """
    d = tmp_path / "placeroot-cache"
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(d))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    return d


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_http_server():
    """Start placeroot's streamable-HTTP transport on an ephemeral port.

    Uses uvicorn.Server directly (rather than server.mcp.run(), which blocks
    forever) so the test can request a graceful shutdown afterward; this is
    the same Starlette app server.mcp.run(transport="streamable-http", ...)
    builds internally (mcp.streamable_http_app()), so it exercises the exact
    code path --http uses in production.

    In-thread, not a subprocess, so the server shares this process's module
    state — in particular the `offline_data` autouse fixture's
    `overture.set_data_path(...)` overrides, which are plain module globals a
    subprocess would not inherit.
    """
    try:
        import uvicorn
    except ImportError:
        pytest.skip("uvicorn not installed; this SDK build lacks streamable-HTTP support")

    from placeroot import server

    host = "127.0.0.1"
    port = _free_port()
    app = server.mcp.streamable_http_app(host=host)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    usrv = uvicorn.Server(config)

    thread = threading.Thread(target=usrv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if usrv.started:
            break
        threading.Event().wait(0.05)
    else:
        pytest.fail("streamable-HTTP server did not start within 10s")

    try:
        yield f"http://{host}:{port}/mcp"
    finally:
        usrv.should_exit = True
        thread.join(timeout=5)


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
