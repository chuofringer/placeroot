"""placeroot_native must agree with the pure-Python routing core.

Two kinds of test live here:

* ``test_python_*`` run in every environment. They pin the pure-Python
  behaviour the native path has to reproduce (counts, adjacency, shortest
  paths, cut-offs) and the import/env switch, and need no extension.
* ``test_native_*`` run the same inputs through both paths and assert they
  agree: node and edge counts, node order, adjacency order, weights and
  lengths (within 1e-9), coordinates, names, shortest-path cost, distance and
  path, and max_cost behaviour. They skip with a reason when the
  ``placeroot_native`` extension is not importable.

Synthetic graphs go through a fake DuckDB connection (as in
test_routing_build_graph.py), so they run offline. The fixture-graph
comparisons need DuckDB's httpfs extension and skip with a reason when it
cannot be loaded.
"""

import math
import os
import pickle
import random
import subprocess
import sys

import pytest

from placeroot import native, routing
from placeroot.errors import UpstreamUnavailable

from ._routing_fixture import build_routing_fixture as fx

EXTENSION_IMPORTABLE = True
try:
    import placeroot_native  # noqa: F401
except ImportError:
    EXTENSION_IMPORTABLE = False

requires_native = pytest.mark.skipif(
    not EXTENSION_IMPORTABLE,
    reason="placeroot_native is not installed (build with `maturin develop -m native/Cargo.toml`)",
)

CENTER_LAT = fx.ORIGIN_LAT
CENTER_LON = fx.ORIGIN_LON
RADIUS_M = 800

# ---------------------------------------------------------------------------
# Synthetic segment extraction (offline)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return _FakeResult(self._rows)


@pytest.fixture
def python_path(monkeypatch):
    """Force the pure-Python path for a Python-only test, even when the extension is installed."""
    monkeypatch.setattr(native, "AVAILABLE", False)


@pytest.fixture
def eager_native(monkeypatch):
    """Let placeroot_native serve a graph's first search (the default waits for the second)."""
    monkeypatch.setattr(routing, "NATIVE_MIN_SEARCHES", 1)


@pytest.fixture
def build_synthetic(monkeypatch):
    """build_graph over canned rows; returns build(rows, *, native_on, **kwargs)."""
    monkeypatch.setattr(routing, "_upstream_glob", lambda: "fake://segments")
    monkeypatch.setattr(routing, "_check_schema", lambda glob: [])
    monkeypatch.setattr(routing, "_from_source", lambda bbox: "segments")
    monkeypatch.setattr(routing.db, "probe_schema", lambda glob: None)
    monkeypatch.setattr(routing.db, "ensure_spatial", lambda: None)
    monkeypatch.setattr(routing.geo, "geom_expr", lambda glob, as_wkt=False: "wkt")

    def build(rows, *, native_on, mode="walk", **kwargs):
        monkeypatch.setattr(routing.db, "shared_conn", lambda: _FakeConn(rows))
        monkeypatch.setattr(native, "AVAILABLE", native_on)
        return routing.build_graph(CENTER_LAT, CENTER_LON, RADIUS_M, mode=mode, **kwargs)

    return build


def _row(seg_id, wkt, connectors=None, cls="residential", access=None, names=None):
    return (seg_id, cls, connectors, None, access, names, wkt)


def _conn(connector_id, at):
    return {"connector_id": connector_id, "at": at}


def _wkt(points):
    return "LINESTRING (" + ", ".join(f"{lon} {lat}" for lon, lat in points) + ")"


def _random_rows(seed, count=160):
    """A jittered street grid with shared and interior connectors, duplicate
    endpoint connectors, one-way and blocked segments, names, and malformed
    rows the Python loop skips (NULL, bad numbers, one point, no parens)."""
    rng = random.Random(seed)
    base_lat, base_lon = CENTER_LAT, CENTER_LON
    rows = []
    for k in range(count):
        gi, gj = rng.randrange(12), rng.randrange(12)
        lat0 = base_lat + gi * 0.0009 + rng.uniform(-2e-5, 2e-5)
        lon0 = base_lon + gj * 0.0011 + rng.uniform(-2e-5, 2e-5)
        n_pts = rng.randint(2, 5)
        points = []
        for i in range(n_pts):
            dlon = i * rng.uniform(1e-4, 4e-4) * rng.choice([1, -1])
            dlat = i * rng.uniform(-3e-4, 3e-4)
            points.append((lon0 + dlon, lat0 + dlat))
        connectors = []
        if rng.random() < 0.5:
            connectors.append(_conn(f"j{gi}_{gj}", 0.0))
        if rng.random() < 0.2:
            connectors.append(_conn(f"j{gi}_{gj}b", 0.0))  # duplicate endpoint
        if rng.random() < 0.5:
            connectors.append(_conn(f"j{gi}_{gj + 1}", 1.0))
        for _ in range(rng.randint(0, 2)):
            connectors.append(_conn(f"x{rng.randrange(40)}", round(rng.uniform(0.05, 0.95), 4)))
        access = None
        if rng.random() < 0.15:
            access = [{"access_type": "denied", "when": {"heading": "backward"}}]
        elif rng.random() < 0.08:
            access = [{"access_type": "denied", "when": {"heading": "forward"}}]
        names = rng.choice([None, {}, {"primary": f"Street {k % 7}"}])
        cls = rng.choice(["residential", "primary", "secondary", "footway", "motorway"])
        rows.append(_row(f"seg-{k}", _wkt(points), connectors or None, cls, access, names))
    rows.append(_row("null-geom", None))
    rows.append(_row("one-point", "LINESTRING (-73.9 40.7)"))
    rows.append(_row("bad-number", "LINESTRING (-73.9 abc, -73.8 40.7)"))
    rows.append(_row("no-parens", "LINESTRING -73.9 40.7, -73.8 40.8"))
    rows.append(_row("triple", "LINESTRING (-73.9 40.7 1.0, -73.8 40.8 1.0)"))
    return rows


def _graph_state(graph):
    """Everything the rest of routing.py reads from a Graph, in comparable form."""
    return {
        "nodes": list(graph.adjacency),
        "coords": dict(graph.coords),
        "adjacency": {
            node: [(t, w, length) for t, w, length in edges]
            for node, edges in graph.adjacency.items()
        },
        "names": dict(graph._edge_names),
        "undirected": {k: set(v) for k, v in graph._undirected_neighbors.items()},
        "truncated": graph.truncated,
        "weight_is_time": graph.weight_is_time,
    }


def _assert_same_graph(py_state, native_state):
    assert native_state["nodes"] == py_state["nodes"], "node set or insertion order differs"
    assert native_state["coords"] == py_state["coords"]
    assert native_state["names"] == py_state["names"]
    assert native_state["undirected"] == py_state["undirected"]
    assert native_state["truncated"] == py_state["truncated"]
    assert native_state["weight_is_time"] == py_state["weight_is_time"]
    assert native_state["adjacency"].keys() == py_state["adjacency"].keys()
    for node, py_edges in py_state["adjacency"].items():
        nat_edges = native_state["adjacency"][node]
        assert [t for t, _, _ in nat_edges] == [t for t, _, _ in py_edges], node
        for (_, nw, nl), (_, pw, pl) in zip(nat_edges, py_edges):
            assert nw == pytest.approx(pw, abs=1e-9, rel=0), node
            assert nl == pytest.approx(pl, abs=1e-9, rel=0), node


# ---------------------------------------------------------------------------
# Python-only (always run)
# ---------------------------------------------------------------------------


def test_python_env_switch_disables_native_even_when_installed():
    """PLACEROOT_NATIVE=0 wins over an installed extension; no switch means
    AVAILABLE tracks whether the extension imports."""
    code = "from placeroot import native; print(native.AVAILABLE)"
    env = dict(os.environ, PLACEROOT_NATIVE="0")
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"

    env = {k: v for k, v in os.environ.items() if k != "PLACEROOT_NATIVE"}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == str(EXTENSION_IMPORTABLE)


def test_python_straight_segment_with_interior_connector(build_synthetic, python_path):
    rows = [
        _row(
            "s1",
            _wkt([(-73.9, 40.7), (-73.8, 40.7)]),
            [_conn("a", 0.0), _conn("m", 0.5), _conn("b", 1.0)],
        )
    ]
    graph = build_synthetic(rows, native_on=False)
    assert graph.node_count() == 3
    assert graph.edge_count() == 2
    mid = graph.adjacency["m"]
    assert sorted(t for t, _, _ in mid) == ["a", "b"]
    found = routing._dijkstra_path_to_target(graph, "a", "b", 1.0)
    assert found is not None
    _, distance_m, path = found
    assert [node for node, _ in path] == ["a", "m", "b"]
    assert distance_m == pytest.approx(routing._haversine_m(40.7, -73.9, 40.7, -73.8), rel=1e-6)


def test_python_one_way_and_point_ids(build_synthetic, python_path):
    rows = [
        _row("ow", _wkt([(-73.9, 40.7), (-73.9, 40.71)]), access=[
            {"access_type": "denied", "when": {"heading": "backward"}}
        ]),
        _row("bad", "LINESTRING (1 2)"),
    ]
    graph = build_synthetic(rows, native_on=False, mode="drive")
    assert graph.node_count() == 2
    assert f"pt_{round(-73.9, 6)}_{round(40.7, 6)}" in graph.adjacency
    start, end = f"pt_{round(-73.9, 6)}_{round(40.7, 6)}", f"pt_{round(-73.9, 6)}_{round(40.71, 6)}"
    assert [t for t, _, _ in graph.adjacency[start]] == [end]
    assert graph.adjacency[end] == []


def test_python_dijkstra_known_answer_and_cutoff(python_path):
    graph = routing.Graph()
    for node in "abcd":
        graph.add_node(node, 0.0, 0.0)
    graph.add_edge("a", "b", 1.0, 1.0)
    graph.add_edge("b", "c", 1.0, 1.0)
    graph.add_edge("a", "c", 5.0, 5.0)
    graph.add_edge("c", "d", 1.0, 1.0, directed=True)
    assert routing._dijkstra_path_to_target(graph, "a", "c", 1.0) == (
        2.0, 2.0, [("a", 0.0), ("b", 1.0), ("c", 2.0)]
    )
    assert routing._dijkstra_path_to_target(graph, "a", "c", 1.0, max_cost=1.5) is None
    assert routing._dijkstra_path_to_target(graph, "a", "c", 1.0, max_cost=2.0) is not None
    assert routing._dijkstra_path_to_target(graph, "d", "a", 1.0) is None
    assert routing._dijkstra_path_to_target(graph, "a", "a", 1.0) == (0.0, 0.0, [("a", 0.0)])


def test_python_graph_pickles_without_native_handle(python_path):
    graph = routing.Graph()
    graph.add_node("a", 0.0, 0.0)
    graph.add_node("b", 0.0, 1.0)
    graph.add_edge("a", "b", 1.0, 1.0)
    graph._native_csr = object()  # stands in for a native search handle
    loaded = pickle.loads(pickle.dumps(graph))
    assert "_native_csr" not in loaded.__dict__
    assert list(loaded.adjacency) == ["a", "b"]


# ---------------------------------------------------------------------------
# Native equivalence (skipped without the extension)
# ---------------------------------------------------------------------------


@requires_native
@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("mode", ["walk", "cycle", "drive"])
def test_native_build_matches_python_on_synthetic_rows(build_synthetic, seed, mode):
    rows = _random_rows(seed)
    py_graph = build_synthetic(rows, native_on=False, mode=mode)
    nat_graph = build_synthetic(rows, native_on=True, mode=mode)
    assert nat_graph.node_count() == py_graph.node_count()
    assert nat_graph.edge_count() == py_graph.edge_count()
    _assert_same_graph(_graph_state(py_graph), _graph_state(nat_graph))


@requires_native
def test_native_build_skips_shape_graphs_to_python(build_synthetic):
    """want_shapes keeps the Python loop, so shapes are identical by construction."""
    rows = _random_rows(7, count=40)
    py_graph = build_synthetic(rows, native_on=False, want_shapes=True)
    nat_graph = build_synthetic(rows, native_on=True, want_shapes=True)
    assert nat_graph.has_shapes and py_graph.has_shapes
    for a in py_graph.adjacency:
        for b, _, _ in py_graph.adjacency[a]:
            assert nat_graph.shape_between(a, b) == py_graph.shape_between(a, b)


@requires_native
def test_native_point_ids_match_python_round6_repr():
    """pt_ node ids are f-strings of round(x, 6); exercise the repr edge cases."""
    rng = random.Random(11)
    values = [
        0.0, -0.0, 1e-5, 5e-5, 5e-7, -5e-7, 1.5e-5, 0.0078125, -0.0000004,
        123456.7891235, -179.9999999, 179.9999999, 89.1234565, 1e-4, 9.99e-5,
    ]
    values += [rng.uniform(-180, 180) for _ in range(400)]
    values += [rng.uniform(-1e-4, 1e-4) for _ in range(200)]
    rows = []
    expected = []
    for i in range(0, len(values) - 1, 2):
        lon, lat = values[i], values[i + 1]
        rows.append((_wkt([(lon, lat), (lon + 1e-3, lat)]), None, True, True, 1.0))
        expected.append(f"pt_{round(lon, 6)}_{round(lat, 6)}")
        expected.append(f"pt_{round(lon + 1e-3, 6)}_{round(lat, 6)}")
    ids, *_ = native.build_graph_arrays(rows, routing.EARTH_RADIUS_M)
    assert list(ids) == list(dict.fromkeys(expected))


@requires_native
@pytest.mark.parametrize("seed", [4, 5])
def test_native_dijkstra_matches_python_on_random_graphs(monkeypatch, eager_native, seed):
    """Random graphs with integer weights (many exact ties) and one-way edges.
    Paths, costs, distances and max_cost cut-offs must match exactly."""
    rng = random.Random(seed)
    graph = routing.Graph()
    nodes = [f"n{i:03d}" for i in range(120)]
    for node in nodes:
        graph.add_node(node, 0.0, 0.0)
    for _ in range(420):
        a, b = rng.sample(nodes, 2)
        weight = float(rng.randint(1, 6))
        length = weight * 10.0 if rng.random() < 0.5 else weight * 7.0 + 0.25
        graph.add_edge(a, b, weight, length, directed=rng.random() < 0.3)
    pairs = [tuple(rng.sample(nodes, 2)) for _ in range(60)]
    for source, target in pairs:
        monkeypatch.setattr(native, "AVAILABLE", False)
        py_full = routing._dijkstra_path_to_target(graph, source, target, 1.0)
        for speed in (1.0, 3.5):
            for max_cost in (math.inf, 4.0, 9.0):
                monkeypatch.setattr(native, "AVAILABLE", False)
                expected = routing._dijkstra_path_to_target(
                    graph, source, target, speed, max_cost=max_cost
                )
                monkeypatch.setattr(native, "AVAILABLE", True)
                actual = routing._dijkstra_path_to_target(
                    graph, source, target, speed, max_cost=max_cost
                )
                assert actual == expected, (source, target, speed, max_cost)
        if py_full is not None:
            cost = py_full[0]
            for max_cost in (cost, cost - 1e-9, cost + 1e-9):
                monkeypatch.setattr(native, "AVAILABLE", False)
                expected = routing._dijkstra_path_to_target(graph, source, target, 1.0, max_cost)
                monkeypatch.setattr(native, "AVAILABLE", True)
                assert routing._dijkstra_path_to_target(
                    graph, source, target, 1.0, max_cost
                ) == expected


@requires_native
def test_native_dijkstra_sees_edges_added_after_a_search(monkeypatch, eager_native):
    """add_edge clears the cached native search state."""
    graph = routing.Graph()
    for node in "abc":
        graph.add_node(node, 0.0, 0.0)
    graph.add_edge("a", "b", 1.0, 1.0)
    graph.add_edge("b", "c", 1.0, 1.0)
    monkeypatch.setattr(native, "AVAILABLE", True)
    assert routing._dijkstra_path_to_target(graph, "a", "c", 1.0)[0] == 2.0
    graph.add_edge("a", "c", 0.5, 0.5)
    assert routing._dijkstra_path_to_target(graph, "a", "c", 1.0)[0] == 0.5


@requires_native
def test_native_first_search_stays_in_python_then_uses_native(monkeypatch):
    """The gate: one search per graph is Python; the second builds the CSR once."""
    graph = routing.Graph()
    for node in "abc":
        graph.add_node(node, 0.0, 0.0)
    graph.add_edge("a", "b", 1.0, 1.0)
    graph.add_edge("b", "c", 1.0, 1.0)
    monkeypatch.setattr(native, "AVAILABLE", True)
    assert routing.NATIVE_MIN_SEARCHES == 2
    assert routing._dijkstra_path_to_target(graph, "a", "c", 1.0)[0] == 2.0
    assert "_native_csr" not in graph.__dict__
    assert routing._dijkstra_path_to_target(graph, "a", "c", 1.0)[0] == 2.0
    assert "_native_csr" in graph.__dict__


@requires_native
def test_native_csr_rejects_inconsistent_arrays():
    with pytest.raises(ValueError):
        native.csr([0, 1], [0, 0], [1.0], [1.0, 1.0])
    with pytest.raises(ValueError):
        native.csr([0, 1], [5], [1.0], [1.0])


@requires_native
def test_native_module_dijkstra_matches_csr_method():
    import placeroot_native

    indptr = [0, 2, 3, 3]
    indices = [1, 2, 2]
    weights = [1.0, 4.0, 1.0]
    lengths = [10.0, 40.0, 10.0]
    module = placeroot_native.dijkstra(indptr, indices, weights, lengths, 0, 2, math.inf, 1.0)
    method = native.csr(indptr, indices, weights, lengths).dijkstra(0, 2, 1.0, math.inf)
    assert module == method == (2.0, 20.0, [0, 1, 2], [0.0, 10.0, 20.0])
    assert placeroot_native.dijkstra(indptr, indices, weights, lengths, 0, 2, 1.5, 1.0) is None


@requires_native
def test_native_fixture_graph_matches_python(monkeypatch, eager_native):
    """The routing fixture (transportation.parquet) through build_graph, both paths."""
    monkeypatch.setattr(native, "AVAILABLE", False)
    try:
        py_graph = routing.build_graph(fx.ORIGIN_LAT, fx.ORIGIN_LON, 3000)
    except UpstreamUnavailable as e:
        pytest.skip(f"DuckDB could not load the spatial/httpfs extensions offline: {e}")
    monkeypatch.setattr(native, "AVAILABLE", True)
    nat_graph = routing.build_graph(fx.ORIGIN_LAT, fx.ORIGIN_LON, 3000)
    assert nat_graph.node_count() == py_graph.node_count() == fx.GRID_N * fx.GRID_N + 5 + 2 + 2 + 1
    assert nat_graph.edge_count() == py_graph.edge_count()
    _assert_same_graph(_graph_state(py_graph), _graph_state(nat_graph))

    nodes = sorted(py_graph.adjacency)
    pairs = [(nodes[0], nodes[-1]), (nodes[3], nodes[len(nodes) // 2]), (nodes[7], nodes[11])]
    for source, target in pairs:
        monkeypatch.setattr(native, "AVAILABLE", False)
        expected = routing._dijkstra_path_to_target(py_graph, source, target, 1.0)
        monkeypatch.setattr(native, "AVAILABLE", True)
        actual = routing._dijkstra_path_to_target(nat_graph, source, target, 1.0)
        assert actual == expected
        if expected is not None:
            cost = expected[0]
            monkeypatch.setattr(native, "AVAILABLE", False)
            cut_py = routing._dijkstra_path_to_target(py_graph, source, target, 1.0, cost / 2)
            monkeypatch.setattr(native, "AVAILABLE", True)
            assert cut_py is None
            assert (
                routing._dijkstra_path_to_target(nat_graph, source, target, 1.0, cost / 2) is None
            )


@requires_native
def test_native_defers_to_python_on_a_dangling_edge(monkeypatch, eager_native):
    """A corrupted graph (a node with no adjacency entry): the answer, or the KeyError,
    must be Python's. add_edge itself refuses such graphs, so the entry is removed by hand."""
    graph = routing.Graph()
    graph.add_node("a", 0.0, 0.0)
    graph.add_node("ghost", 0.0, 1.0)
    graph.add_edge("a", "ghost", 1.0, 1.0, directed=True)
    del graph.adjacency["ghost"]
    monkeypatch.setattr(native, "AVAILABLE", False)
    expected = routing._dijkstra_path_to_target(graph, "a", "ghost", 1.0)
    monkeypatch.setattr(native, "AVAILABLE", True)
    assert routing._dijkstra_path_to_target(graph, "a", "ghost", 1.0) == expected
    assert expected == (1.0, 1.0, [("a", 0.0), ("ghost", 1.0)])

    monkeypatch.setattr(native, "AVAILABLE", False)
    with pytest.raises(KeyError):
        routing._dijkstra_path_to_target(graph, "a", "unreachable", 1.0)
    monkeypatch.setattr(native, "AVAILABLE", True)
    with pytest.raises(KeyError):
        routing._dijkstra_path_to_target(graph, "a", "unreachable", 1.0)
