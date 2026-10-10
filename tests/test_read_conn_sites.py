"""Read-only query sites run on db.read_conn(), not under db.conn_lock.

Two layers of check:

1. Runtime: db.conn_lock is replaced by a recording lock and the read-only
   paths of routing.build_graph, buildings.summarize_buildings,
   water.water_near and transit.transit_stops_near are driven to completion.
   The DuckDB side is a stub connection, which read_conn() honours the same
   way it honours a replaced db.shared_conn, so this runs offline. Nothing
   on these paths may acquire the lock.

2. Static: every function migrated off `with ...conn_lock:` is parsed and
   checked for lock blocks. A source-text grep would also match comments
   that mention conn_lock, so this walks the AST instead.
"""

import ast
import importlib
import inspect
import textwrap

import pytest

from placeroot import buildings, db, overture, routing, transit, water


class _RecordingLock:
    """Counts every acquisition; never blocks."""

    def __init__(self):
        self.acquisitions = 0

    def __enter__(self):
        self.acquisitions += 1
        return self

    def __exit__(self, *exc):
        return False

    def acquire(self, *args, **kwargs):
        self.acquisitions += 1
        return True

    def release(self):
        pass


class _EmptyResult:
    def fetchall(self):
        return []

    def fetchone(self):
        return None

    @property
    def description(self):
        return None


class _StubConn:
    """Answers every statement with no rows and records the SQL it saw."""

    def __init__(self):
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        return _EmptyResult()


@pytest.fixture
def recording_lock(monkeypatch):
    lock = _RecordingLock()
    monkeypatch.setattr(db, "conn_lock", lock)
    return lock


@pytest.fixture
def stub_conn(monkeypatch):
    conn = _StubConn()
    monkeypatch.setattr(db, "shared_conn", lambda: conn)
    monkeypatch.setattr(db, "ensure_spatial", lambda: None)
    monkeypatch.setattr(db, "probe_schema", lambda glob: None)
    return conn


def _no_schema_probe(monkeypatch, module, source="fake://segments"):
    """Skip the schema probe and the FROM-clause resolution (both need the
    upstream bucket); the statement under test is what must stay lock-free."""
    monkeypatch.setattr(module, "_upstream_glob", lambda: source)
    monkeypatch.setattr(module, "_check_schema", lambda glob: [])
    monkeypatch.setattr(module, "_from_source", lambda bbox: "fake_source")


LAT, LON = 40.7, -73.9


def test_build_graph_read_runs_without_conn_lock(monkeypatch, recording_lock, stub_conn):
    _no_schema_probe(monkeypatch, routing)
    monkeypatch.setattr(routing.geo, "geom_expr", lambda glob, as_wkt=False: "wkt")

    routing.build_graph(LAT, LON, 500, mode="walk")

    assert any("fake_source" in s for s in stub_conn.sql), "segment fetch never ran"
    assert recording_lock.acquisitions == 0


def test_summarize_buildings_read_runs_without_conn_lock(monkeypatch, recording_lock, stub_conn):
    _no_schema_probe(monkeypatch, buildings)

    buildings.summarize_buildings(LAT, LON, 300)

    assert any("fake_source" in s for s in stub_conn.sql), "buildings read never ran"
    assert recording_lock.acquisitions == 0


def test_water_near_read_runs_without_conn_lock(monkeypatch, recording_lock, stub_conn):
    _no_schema_probe(monkeypatch, water)

    water.water_near(LAT, LON, 1000)

    assert any("fake_source" in s for s in stub_conn.sql), "water read never ran"
    assert recording_lock.acquisitions == 0


def test_transit_stops_near_read_runs_without_conn_lock(monkeypatch, recording_lock, stub_conn):
    # transit reads through infrastructure's schema/source helpers.
    monkeypatch.setattr(transit.infrastructure, "_from_source", lambda bbox: "fake_source")
    monkeypatch.setattr(transit.infrastructure, "_check_schema", lambda glob: [])
    monkeypatch.setattr(transit.infrastructure, "_upstream_glob", lambda: "fake://segments")

    transit.transit_stops_near(LAT, LON, 800)

    assert any("fake_source" in s for s in stub_conn.sql), "transit read never ran"
    assert recording_lock.acquisitions == 0


def test_geocode_qualifier_read_runs_without_conn_lock(monkeypatch, recording_lock, stub_conn):
    """The geocode sites go through overture.read_conn(), which must reach
    db.read_conn() (and so the stubbed connection) when overture.conn is the
    real one. Any unique name keeps the lru_cache from answering."""
    from placeroot.geocode import _qualifiers

    assert _qualifiers._division_named_exactly_cached("zz-no-such-division", "t.parquet") is False
    assert len(stub_conn.sql) == 1
    assert recording_lock.acquisitions == 0


def test_overture_read_conn_yields_a_stubbed_conn_alias(monkeypatch):
    """Tests stub overture.conn (the alias geocode's lookups used to query
    through). overture.read_conn() must keep yielding that stub, as it did
    before the sites migrated, rather than a pooled cursor."""

    class _Fake:
        def execute(self, sql, params=None):
            return self

        def fetchone(self):
            return ("stubbed",)

    fake = _Fake()
    monkeypatch.setattr(overture, "conn", lambda: fake)
    with overture.read_conn() as rc:
        assert rc is fake
        assert rc.execute("SELECT 1").fetchone() == ("stubbed",)


# --- static: no lock block remains in the migrated functions ----------------

# (module, function) for every function whose read-only statement moved from
# `with conn_lock:` to read_conn(). Functions that keep a lock for a write or
# SET (geocode _publish_copied_parquet, db._ensure_extension, ...) are not
# listed.
MIGRATED = [
    ("placeroot.routing", "build_graph"),
    ("placeroot.buildings", "summarize_buildings"),
    ("placeroot.buildings", "buildings_at"),
    ("placeroot.water", "_containing_body"),
    ("placeroot.water", "water_near"),
    ("placeroot.transit", "_run_query"),
    ("placeroot.infrastructure", "infrastructure_at"),
    ("placeroot.changes", "_scan_release"),
    ("placeroot.land_use", "_classify"),
    ("placeroot.area_suggest", "intersect_sheds"),
    ("placeroot.geometry_setops", "_set_op"),
    ("placeroot.geocode._addresses", "_division_area_bbox"),
    ("placeroot.geocode._addresses", "_warm_division_area_bboxes"),
    ("placeroot.geocode._addresses", "_scan_addresses_in_bbox"),
    ("placeroot.geocode._addresses", "_scan_street_neighbors_in_bbox"),
    ("placeroot.geocode._anchors", "_query_places_multi_anchor"),
    ("placeroot.geocode._anchors", "_query_places_fallback"),
    ("placeroot.geocode._divisions", "_query_divisions_from_local"),
    ("placeroot.geocode._divisions", "_query_divisions_from_upstream"),
    ("placeroot.geocode._divisions", "_query_alt_names"),
    ("placeroot.geocode._divisions", "_query_divisions_fuzzy"),
    ("placeroot.geocode._index", "_lang_variants_for"),
    ("placeroot.geocode._index", "_divisions_table_has_bbox"),
    ("placeroot.geocode._index", "_division_bbox"),
    ("placeroot.geocode._index", "_region_population_lookup_cached"),
    ("placeroot.geocode._postcode", "_query_postcode_countries"),
    ("placeroot.geocode._postcode", "_covering_division_from_local"),
    ("placeroot.geocode._qualifiers", "_resolve_region_from_table"),
    ("placeroot.geocode._qualifiers", "_division_named_exactly_cached"),
    ("placeroot.geocode._qualifiers", "_resolve_country_from_table"),
    ("placeroot.geocode._reverse", "_nearest_address"),
    ("placeroot.geocode._reverse", "_nearest_division"),
]


def _lock_withs_and_reads(func: ast.AST) -> tuple[list[int], int]:
    """(line numbers of `with` blocks taking a conn_lock, count of read_conn
    blocks) for one function node. Only `with` items are inspected, so
    comments and docstrings that mention the lock do not count."""
    locks, reads = [], 0
    for node in ast.walk(func):
        if not isinstance(node, ast.With):
            continue
        exprs = [ast.unparse(item.context_expr) for item in node.items]
        if any("conn_lock" in e for e in exprs):
            locks.append(node.lineno)
        if any("read_conn" in e for e in exprs):
            reads += 1
    return locks, reads


@pytest.mark.parametrize(("module_name", "func_name"), MIGRATED)
def test_migrated_function_takes_no_conn_lock(module_name, func_name):
    module = importlib.import_module(module_name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
    ]
    assert funcs, f"{module_name}.{func_name} not found"
    for func in funcs:
        locks, reads = _lock_withs_and_reads(func)
        assert not locks, f"{module_name}.{func_name} still takes conn_lock at line(s) {locks}"
        assert reads >= 1, f"{module_name}.{func_name} no longer runs a read_conn() block"


def test_static_check_ignores_comments_and_still_sees_a_real_lock():
    """The AST check must not be fooled either way: a comment naming the lock
    is not a use, and a real `with conn_lock:` is."""
    comment_only = ast.parse(
        "def f():\n    # takes db.conn_lock here\n    with db.read_conn() as rc:\n        pass\n"
    )
    real_lock = ast.parse("def f():\n    with db.conn_lock:\n        pass\n")
    (f1,) = [n for n in ast.walk(comment_only) if isinstance(n, ast.FunctionDef)]
    (f2,) = [n for n in ast.walk(real_lock) if isinstance(n, ast.FunctionDef)]
    assert _lock_withs_and_reads(f1) == ([], 1)
    assert _lock_withs_and_reads(f2)[0] == [2]
