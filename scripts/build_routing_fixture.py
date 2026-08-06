"""Builds the offline routing test fixture: tests/fixtures/transportation.parquet.

Synthetic Overture-transportation-shaped segment data (id, geometry as WKB
linestring, bbox, class, subclass, connectors) matching the columns
routing.py expects. A deterministic 20x20 street grid, 100m spacing:

- Cardinal edges (horizontal + vertical) between adjacent grid nodes.
- A "river": no horizontal edge crosses from column RIVER_GAP_I to
  RIVER_GAP_I + 1 except at row BRIDGE_J, where a single footway "bridge"
  segment connects the two halves.
- A handful of diagonal path segments.
- A couple of hand-picked missing edges (potholes).
- One direct "shortcut" segment classed motorway between two nodes that
  are also connected the long way through the grid — used to test that
  the walkable filter actually excludes motorways/trunks rather than just
  preferring the grid path by chance.

Regenerate with:

    uv run python scripts/build_routing_fixture.py
"""

import math
from pathlib import Path

import duckdb

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "transportation.parquet"
)
EARTH_RADIUS_M = 6371000.0

GRID_N = 20  # nodes per side: indices 0..GRID_N-1
SPACING_M = 100.0

# Fake neighborhood — not a real Overture location. Deliberately distinct
# from the places fixture's center.
ORIGIN_LAT = 40.740000
ORIGIN_LON = -73.990000

RIVER_GAP_I = 9  # no direct edge between column 9 and column 10...
BRIDGE_J = 10  # ...except at this row.

DIAG_STEP = 4  # diagonal edges every 4 grid cells, skipping the river column.

# Hand-picked missing edges ("potholes"): ((i, j), (i2, j2)) pairs that
# would otherwise be regular grid edges.
MISSING_EDGES = [((5, 5), (6, 5)), ((15, 15), (16, 15))]

# A motorway "shortcut" directly connecting two nodes that are also reachable
# the long way through the grid. Must never be used by the walking graph.
SHORTCUT = {"from": (2, 2), "to": (6, 6), "cls": "motorway"}


def node_id(i: int, j: int) -> str:
    return f"n_{i}_{j}"


def _offset(lat: float, lon: float, distance_m: float, bearing_deg: float) -> tuple[float, float]:
    """Destination point given a start point, distance, and bearing (spherical Earth)."""
    brng = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    ang = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brng)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def node_latlon(i: int, j: int) -> tuple[float, float]:
    """Grid node (i, j) coordinates: i*SPACING_M east then j*SPACING_M north of ORIGIN."""
    lat1, lon1 = _offset(ORIGIN_LAT, ORIGIN_LON, i * SPACING_M, 90)
    lat2, lon2 = _offset(lat1, lon1, j * SPACING_M, 0)
    return lat2, lon2


def _edge_allowed(i: int, j: int, i2: int, j2: int) -> bool:
    if {i, i2} == {RIVER_GAP_I, RIVER_GAP_I + 1} and j == j2 and j != BRIDGE_J:
        return False
    if ((i, j), (i2, j2)) in MISSING_EDGES or ((i2, j2), (i, j)) in MISSING_EDGES:
        return False
    return True


def build_edges() -> list[tuple[tuple[int, int], tuple[int, int], str]]:
    """List of (node_a, node_b, class) triples."""
    edges = []

    # Horizontal.
    for i in range(GRID_N - 1):
        for j in range(GRID_N):
            if _edge_allowed(i, j, i + 1, j):
                cls = "footway" if i == RIVER_GAP_I and j == BRIDGE_J else "residential"
                edges.append(((i, j), (i + 1, j), cls))

    # Vertical — the river runs north/south along the column boundary and
    # doesn't block north/south movement.
    for i in range(GRID_N):
        for j in range(GRID_N - 1):
            if _edge_allowed(i, j, i, j + 1):
                edges.append(((i, j), (i, j + 1), "residential"))

    # Diagonals, skipping the river column so they don't smuggle a second
    # east/west crossing.
    for i in range(0, GRID_N - 1, DIAG_STEP):
        for j in range(0, GRID_N - 1, DIAG_STEP):
            if i == RIVER_GAP_I:
                continue
            edges.append(((i, j), (i + 1, j + 1), "path"))

    # Motorway shortcut — excluded from the walkable graph.
    edges.append((SHORTCUT["from"], SHORTCUT["to"], SHORTCUT["cls"]))

    return edges


def build_rows() -> list[tuple]:
    edges = build_edges()
    rows = []
    for idx, (a, b, cls) in enumerate(edges):
        lat1, lon1 = node_latlon(*a)
        lat2, lon2 = node_latlon(*b)
        wkt = f"LINESTRING ({lon1} {lat1}, {lon2} {lat2})"
        bbox = {
            "xmin": min(lon1, lon2), "ymin": min(lat1, lat2),
            "xmax": max(lon1, lon2), "ymax": max(lat1, lat2),
        }
        connectors = [
            {"connector_id": node_id(*a), "at": 0.0},
            {"connector_id": node_id(*b), "at": 1.0},
        ]
        rows.append((f"seg-{idx:05d}", wkt, bbox, cls, None, connectors))
    return rows


def main() -> None:
    rows = build_rows()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("""
        CREATE TABLE staging (
            id VARCHAR,
            wkt VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            class VARCHAR,
            subclass VARCHAR,
            connectors STRUCT(connector_id VARCHAR, "at" DOUBLE)[]
        )
    """)
    con.executemany("INSERT INTO staging VALUES (?, ?, ?, ?, ?, ?)", rows)
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
            SELECT
                id,
                ST_AsWKB(ST_GeomFromText(wkt)) AS geometry,
                bbox, class, subclass, connectors
            FROM staging
        ) TO '{FIXTURE_PATH}' (FORMAT PARQUET)
    """)
    print(f"wrote {len(rows)} rows to {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
