"""build_graph's row handling and extraction query, driven by a fake DuckDB
connection rather than the on-disk fixture: the query text and the rows it
hands back are controlled directly, so the SQL shape (class exclusion in
the WHERE, deterministic ORDER BY before the LIMIT) and the degenerate row
shapes real Overture data carries (NULL geometry, duplicate endpoint
connectors) can be pinned without a spatial scan. No network.
"""

import re

import duckdb
import pytest

from placeroot import routing


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Records every (sql, params) pair build_graph executes and answers
    with canned rows."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _FakeResult(self.rows)


def _row(idx, wkt, connectors=None, cls="residential", access=None, names=None):
    return (f"seg-{idx}", cls, connectors, None, access, names, wkt)


@pytest.fixture
def fake_extraction(monkeypatch):
    """Route build_graph's extraction through a _FakeConn; returns a setter
    that installs the rows and hands back the connection for inspection."""
    monkeypatch.setattr(routing, "_upstream_glob", lambda: "fake://segments")
    monkeypatch.setattr(routing, "_check_schema", lambda glob: [])
    monkeypatch.setattr(routing, "_from_source", lambda bbox: "segments")
    monkeypatch.setattr(routing.db, "probe_schema", lambda glob: None)
    monkeypatch.setattr(routing.db, "ensure_spatial", lambda: None)
    monkeypatch.setattr(routing.geo, "geom_expr", lambda glob, as_wkt=False: "wkt")

    def install(rows):
        conn = _FakeConn(rows)
        monkeypatch.setattr(routing.db, "shared_conn", lambda: conn)
        return conn

    return install


LAT, LON = 40.7, -73.9


def _normalized_sql(conn: _FakeConn) -> str:
    assert len(conn.calls) == 1
    return re.sub(r"\s+", " ", conn.calls[0][0]).strip()


# --- Query shape -------------------------------------------------------------


def test_mode_class_exclusions_are_applied_in_sql_before_the_limit(fake_extraction):
    conn = fake_extraction([])
    routing.build_graph(LAT, LON, 500, mode="walk")
    sql = _normalized_sql(conn)
    params = conn.calls[0][1]
    match = re.search(r"AND \(class IS NULL OR class NOT IN \(([^)]*)\)\)", sql)
    assert match, sql
    placeholders = [p.strip() for p in match.group(1).split(",")]
    assert {params[p.lstrip("$")] for p in placeholders} == set(
        routing.MODE_CONFIG["walk"]["excluded_classes"]
    )
    # The exclusion precedes the cap, so the cap counts usable rows only.
    assert sql.index("class NOT IN") < sql.index("LIMIT")
    # Parameterised, never interpolated.
    for excluded in routing.MODE_CONFIG["walk"]["excluded_classes"]:
        assert f"'{excluded}'" not in sql


def test_avoid_overlay_is_part_of_the_sql_exclusion(fake_extraction):
    conn = fake_extraction([])
    routing.build_graph(LAT, LON, 500, mode="drive", avoid=("motorway",))
    params = conn.calls[0][1]
    excluded = {v for k, v in params.items() if k.startswith("excluded_class_")}
    assert excluded == routing._excluded_classes_for("drive", ("motorway",))
    assert "motorway" in excluded and "motorway_link" in excluded


def test_extraction_is_ordered_by_distance_from_center_then_id(fake_extraction):
    conn = fake_extraction([])
    routing.build_graph(LAT, LON, 500, mode="walk")
    sql = _normalized_sql(conn)
    params = conn.calls[0][1]
    order_by = sql.index("ORDER BY")
    assert order_by < sql.index("LIMIT")
    order_clause = sql[order_by : sql.index("LIMIT")]
    assert "$center_lon" in order_clause and "$center_lat" in order_clause
    assert order_clause.rstrip().endswith(", id")
    assert params["center_lat"] == LAT and params["center_lon"] == LON
    assert 0 < params["lon_scale"] <= 1.0
    assert sql.endswith(f"LIMIT {routing.MAX_GRAPH_SEGMENTS + 1}")


def test_no_class_filter_when_class_column_is_missing(fake_extraction, monkeypatch):
    conn = fake_extraction([])
    monkeypatch.setattr(routing, "_check_schema", lambda glob: ["class"])
    routing.build_graph(LAT, LON, 500, mode="walk")
    sql = _normalized_sql(conn)
    assert "class NOT IN" not in sql
    assert "NULL AS class" in sql


# --- Row handling ------------------------------------------------------------


def test_null_geometry_rows_are_skipped_not_raised(fake_extraction):
    fake_extraction(
        [
            _row(0, None),
            _row(1, "LINESTRING (-73.9 40.7, -73.899 40.7)"),
        ]
    )
    graph = routing.build_graph(LAT, LON, 500, mode="walk")
    assert graph.node_count() == 2


def test_malformed_wkt_rows_are_still_skipped(fake_extraction):
    fake_extraction([_row(0, "not a linestring"), _row(1, "LINESTRING (1 2)")])
    graph = routing.build_graph(LAT, LON, 500, mode="walk")
    assert graph.node_count() == 0


def test_duplicate_endpoint_connectors_all_become_nodes(fake_extraction):
    # Two connectors at at=0 and two at at=1: the second used to overwrite
    # the first, leaving a connector a neighbouring segment references
    # with no node at all.
    fake_extraction(
        [
            _row(
                0,
                "LINESTRING (-73.9 40.7, -73.899 40.7)",
                connectors=[
                    {"connector_id": "c_start_a", "at": 0.0},
                    {"connector_id": "c_start_b", "at": 0.0},
                    {"connector_id": "c_end_a", "at": 1.0},
                    {"connector_id": "c_end_b", "at": 1.0},
                ],
            ),
            # A second segment hanging only off the "duplicate" connectors.
            _row(
                1,
                "LINESTRING (-73.899 40.7, -73.899 40.701)",
                connectors=[
                    {"connector_id": "c_end_b", "at": 0.0},
                    {"connector_id": "far", "at": 1.0},
                ],
            ),
        ]
    )
    graph = routing.build_graph(LAT, LON, 500, mode="walk")
    for node in ("c_start_a", "c_start_b", "c_end_a", "c_end_b", "far"):
        assert node in graph.adjacency, node
    assert graph.coords["c_start_a"] == graph.coords["c_start_b"]
    assert graph.coords["c_end_a"] == graph.coords["c_end_b"]
    # The duplicates are tied to their endpoint for free, both ways.
    assert ("c_start_b", 0.0, 0.0) in graph.adjacency["c_start_a"]
    assert ("c_start_a", 0.0, 0.0) in graph.adjacency["c_start_b"]
    # ... so the whole thing is one routable component: c_start_b -> far.
    found = routing._dijkstra_path_to_target(graph, "c_start_b", "far", 1.0)
    assert found is not None
    assert found[1] == pytest.approx(
        routing._haversine_m(40.7, -73.9, 40.7, -73.899)
        + routing._haversine_m(40.7, -73.899, 40.701, -73.899),
        rel=1e-6,
    )


def test_duplicate_endpoint_tie_is_undirected_on_a_one_way_segment(fake_extraction):
    fake_extraction(
        [
            _row(
                0,
                "LINESTRING (-73.9 40.7, -73.899 40.7)",
                connectors=[
                    {"connector_id": "s_a", "at": 0.0},
                    {"connector_id": "s_b", "at": 0.0},
                    {"connector_id": "e", "at": 1.0},
                ],
                access=[{"access_type": "denied", "when": {"heading": "backward"}}],
            )
        ]
    )
    graph = routing.build_graph(LAT, LON, 500, mode="drive")
    assert ("s_a", 0.0, 0.0) in graph.adjacency["s_b"]
    assert ("s_b", 0.0, 0.0) in graph.adjacency["s_a"]
    # The segment itself stays one-way: e has no edge back.
    assert graph.adjacency["e"] == []
    assert routing._dijkstra_path_to_target(graph, "s_b", "e", 1.0) is not None
    assert routing._dijkstra_path_to_target(graph, "e", "s_b", 1.0) is None


# --- The query against a real (in-memory, extension-free) DuckDB ------------


@pytest.fixture
def duckdb_extraction(monkeypatch):
    """Same seams as fake_extraction, but the query really runs against an
    in-memory `segments` table shaped like the fixture's columns, so the
    ORDER BY / class filter SQL is executed rather than string-matched."""
    conn = duckdb.connect()
    conn.execute(
        """
        CREATE TABLE segments (
            id VARCHAR, class VARCHAR, connectors JSON, speed_limits JSON,
            access_restrictions JSON, names JSON, wkt VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE)
        )
        """
    )
    monkeypatch.setattr(routing, "_upstream_glob", lambda: "fake://segments")
    monkeypatch.setattr(routing, "_check_schema", lambda glob: [])
    monkeypatch.setattr(routing, "_from_source", lambda bbox: "segments")
    monkeypatch.setattr(routing.db, "probe_schema", lambda glob: None)
    monkeypatch.setattr(routing.db, "ensure_spatial", lambda: None)
    monkeypatch.setattr(routing.geo, "geom_expr", lambda glob, as_wkt=False: "wkt")
    monkeypatch.setattr(routing.db, "shared_conn", lambda: conn)

    def insert(idx, lon0, lat0, lon1, lat1, cls="residential"):
        conn.execute(
            "INSERT INTO segments VALUES (?, ?, NULL, NULL, NULL, NULL, ?, "
            "{'xmin': ?, 'ymin': ?, 'xmax': ?, 'ymax': ?})",
            [
                f"seg-{idx}",
                cls,
                f"LINESTRING ({lon0} {lat0}, {lon1} {lat1})",
                min(lon0, lon1),
                min(lat0, lat1),
                max(lon0, lon1),
                max(lat0, lat1),
            ],
        )

    yield insert
    conn.close()


def test_truncated_graph_keeps_the_rows_nearest_the_center(duckdb_extraction, monkeypatch):
    # Four short east-west stubs at increasing distance from the center,
    # inserted far-first so a scan-order LIMIT would keep the wrong ones.
    for idx, offset in enumerate((0.004, 0.003, 0.001, 0.002)):
        duckdb_extraction(idx, LON, LAT + offset, LON + 0.0001, LAT + offset)
    monkeypatch.setattr(routing, "MAX_GRAPH_SEGMENTS", 2)
    graph = routing.build_graph(LAT, LON, 1000, mode="walk")
    assert graph.truncated is True
    kept_lats = sorted({round(lat - LAT, 4) for lat, _lon in graph.coords.values()})
    assert kept_lats == [0.001, 0.002]


def test_excluded_classes_never_reach_python_and_do_not_count_toward_cap(
    duckdb_extraction, monkeypatch
):
    # Two nearest rows are motorways (excluded for walk); with the filter
    # in SQL the two residential rows still fit under a cap of 2.
    duckdb_extraction(0, LON, LAT + 0.0005, LON + 0.0001, LAT + 0.0005, cls="motorway")
    duckdb_extraction(1, LON, LAT + 0.0006, LON + 0.0001, LAT + 0.0006, cls="motorway")
    duckdb_extraction(2, LON, LAT + 0.001, LON + 0.0001, LAT + 0.001)
    duckdb_extraction(3, LON, LAT + 0.002, LON + 0.0001, LAT + 0.002)
    monkeypatch.setattr(routing, "MAX_GRAPH_SEGMENTS", 2)
    graph = routing.build_graph(LAT, LON, 1000, mode="walk")
    assert graph.truncated is False
    assert graph.node_count() == 4


def test_python_side_class_exclusion_still_guards_unfiltered_rows(fake_extraction):
    # The SQL filter is the primary guard; the loop's check remains for a
    # source that hands back excluded classes anyway (no class column, or a
    # view that ignores the predicate).
    fake_extraction([_row(0, "LINESTRING (-73.9 40.7, -73.899 40.7)", cls="motorway")])
    graph = routing.build_graph(LAT, LON, 500, mode="walk")
    assert graph.node_count() == 0
