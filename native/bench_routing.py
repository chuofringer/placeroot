"""Timing for placeroot's graph build and path search, pure Python vs placeroot_native.

Builds one connected synthetic street grid (about --segments segments) through
routing.build_graph, fed by a canned extraction so no DuckDB or network is
involved, then runs --queries shortest-path searches between fixed random node
pairs. Both paths run with the product defaults, so the first search on a
graph stays in Python and later ones use the native CSR (see
routing.NATIVE_MIN_SEARCHES). Exits 1 if the two paths disagree on any path.

    uv run python native/bench_routing.py --segments 200000 --queries 50

Requires the extension for the native column (maturin develop); without it
only the Python column is printed.
"""

from __future__ import annotations

import argparse
import random
import sys
import time

from placeroot import native, routing

LAT0, LON0 = 40.7, -73.9
RADIUS_M = 3000


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return _Result(self._rows)


def synthetic_rows(segments: int, seed: int) -> list[tuple]:
    """A connected grid: each cell contributes one east and one north segment."""
    rng = random.Random(seed)
    side = int((segments // 2) ** 0.5)
    cells = side * side
    rows = []
    for k in range(segments):
        cell, horizontal = divmod(k, 2)
        gi, gj = divmod(cell % cells, side)
        if horizontal == 0:
            ni, nj = gi, gj + 1
        else:
            ni, nj = gi + 1, gj
        lat0 = LAT0 + gi * 0.0002
        lon0 = LON0 + gj * 0.00025
        points = [
            (lon0 + i * 0.00005, lat0 + rng.uniform(-1e-5, 1e-5))
            for i in range(rng.randint(2, 6))
        ]
        wkt = "LINESTRING (" + ", ".join(f"{x} {y}" for x, y in points) + ")"
        connectors = [
            {"connector_id": f"j{gi}_{gj}", "at": 0.0},
            {"connector_id": f"j{ni}_{nj}", "at": 1.0},
        ]
        if rng.random() < 0.3:
            connectors.append({"connector_id": f"m{k}", "at": 0.5})
        rows.append((f"seg-{k}", "residential", connectors, None, None, {"primary": "X"}, wkt))
    return rows


def install_fake_extraction(rows: list[tuple]) -> None:
    routing._upstream_glob = lambda: "bench://segments"
    routing._check_schema = lambda glob: []
    routing._from_source = lambda bbox: "segments"
    routing.db.probe_schema = lambda glob: None
    routing.db.ensure_spatial = lambda: None
    routing.geo.geom_expr = lambda glob, as_wkt=False: "wkt"
    routing.db.shared_conn = lambda: _Conn(rows)


def run(label: str, native_on: bool, query_pairs: list[tuple[str, str]], rows: list[tuple]):
    native.AVAILABLE = native_on
    t0 = time.perf_counter()
    graph = routing.build_graph(LAT0 + 0.01, LON0 + 0.01, RADIUS_M)
    build_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    hits = [routing._dijkstra_path_to_target(graph, s, t, 1.0) for s, t in query_pairs]
    search_s = time.perf_counter() - t1
    print(
        f"{label:8s} build {build_s:7.3f}s  {len(query_pairs)} searches {search_s:7.3f}s  "
        f"({search_s / len(query_pairs) * 1000:.1f} ms each)  "
        f"nodes={graph.node_count()} edges={graph.edge_count()}"
    )
    return build_s, search_s, hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--segments", type=int, default=200_000)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows = synthetic_rows(args.segments, args.seed)
    install_fake_extraction(rows)
    installed = native.AVAILABLE
    have = "yes" if installed else "no"
    print(f"segments={len(rows)} queries={args.queries} native_extension={have}")

    probe = routing.build_graph(LAT0 + 0.01, LON0 + 0.01, RADIUS_M)
    print(f"components={len(probe.connected_components())}")
    nodes = sorted(probe.adjacency)
    picker = random.Random(args.seed + 1)
    pairs = [(picker.choice(nodes), picker.choice(nodes)) for _ in range(args.queries)]

    py_build, py_search, py_hits = run("python", False, pairs, rows)
    if not installed:
        print("placeroot_native not installed; skipping the native column")
        return 0
    nat_build, nat_search, nat_hits = run("native", True, pairs, rows)

    same = py_hits == nat_hits
    print(f"identical paths: {same}")
    print(
        f"speedup: build {py_build / nat_build:.2f}x, searches {py_search / nat_search:.2f}x "
        f"(native searches include the one-time CSR build)"
    )
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
