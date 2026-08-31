"""Builds the offline routing test fixture: tests/fixtures/transportation.parquet.

Synthetic Overture-transportation-shaped segment data (id, geometry as WKB
linestring, bbox, class, subclass, connectors, names) matching the columns
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
- A "crossing" pair of segments (build_cross_rows) that meet only at a
  shared *interior* connector, off the grid lattice — regression coverage
  for issue #37's interior-connector node splitting.
- An "isolated fragment" (build_isolated_fragment_rows): a disconnected
  2-node segment placed nearer to a query point than the real grid node
  next to it — regression coverage for issue #37's component-aware
  snapping.
- A one-way pair (build_oneway_rows): a single residential segment, off
  the grid lattice, carrying an access_restrictions entry that denies
  backward travel — regression coverage for issue #38's directed-edge
  handling (A -> B routable, B -> A not, for cycle/drive modes; walk
  ignores it).
- A "switchback" spur (build_switchback_rows): one segment whose geometry
  detours wildly between its two connectors — regression coverage for the
  #161 sweep finding that route(include_path=True) emitted chords between
  graph nodes and threw the road's real shape away.
- speed_limits/access_restrictions columns on every row (NULL for most —
  standing in for "older" rows a real Overture release might carry before
  those columns existed everywhere): the motorway SHORTCUT segment carries
  an explicit (and, versus its 27 m/s class default, much slower) posted
  speed_limits entry, regression coverage for issue #38's "speed_limits
  overrides the class default" rule.
- names.primary on every regular grid edge (#441): "Grid Ave {j}" for every
  horizontal edge in row j, "Grid St {i}" for every vertical edge in column
  i — regression coverage for map_match's road-name aggregation actually
  finding a name to aggregate. The bridge, diagonals, and motorway shortcut
  are left unnamed (names IS NULL), same as an unnamed real-world path.

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
# the long way through the grid. Must never be used by the walking (or
# cycling) graph, but is driveable — with an explicit posted speed limit
# (issue #38) well below its 27 m/s class default, to prove speed_limits
# wins over the class table.
SHORTCUT = {"from": (2, 2), "to": (6, 6), "cls": "motorway"}
SHORTCUT_SPEED_LIMIT_KMH = 45.0
SHORTCUT_SPEED_LIMIT_M_S = SHORTCUT_SPEED_LIMIT_KMH / 3.6


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


def _offset_m(east_m: float, north_m: float) -> tuple[float, float]:
    """Point east_m/north_m of ORIGIN, not tied to the grid lattice."""
    lat1, lon1 = _offset(ORIGIN_LAT, ORIGIN_LON, east_m, 90)
    return _offset(lat1, lon1, north_m, 0)


def _segment_row(idx: int, p0: tuple[float, float], p1: tuple[float, float], cls: str,
                  connectors: list[dict], speed_limits: list[dict] | None = None,
                  access_restrictions: list[dict] | None = None,
                  name: str | None = None) -> tuple:
    return _polyline_row(idx, [p0, p1], cls, connectors, speed_limits, access_restrictions, name)


def _polyline_row(idx: int, points: list[tuple[float, float]], cls: str,
                   connectors: list[dict], speed_limits: list[dict] | None = None,
                   access_restrictions: list[dict] | None = None,
                   name: str | None = None) -> tuple:
    """A segment row from an arbitrary-length (lat, lon) vertex list.

    Most fixture segments are straight two-point lines; the switchback below
    needs interior shape vertices, which is the whole point of it.

    name (#441) becomes the segment's Overture names.primary — a bare
    STRUCT(primary VARCHAR), since map_match's road-name aggregation
    (routing.Graph._edge_names / name_between) only ever reads that one
    field. None (the default) means "this segment carries no name," matching
    most of the fixture's synthetic geometry.
    """
    wkt_points = ", ".join(f"{lon} {lat}" for lat, lon in points)
    lats = [lat for lat, _lon in points]
    lons = [lon for _lat, lon in points]
    bbox = {
        "xmin": min(lons), "ymin": min(lats), "xmax": max(lons), "ymax": max(lats),
    }
    names = {"primary": name} if name else None
    return (
        f"seg-{idx:05d}", f"LINESTRING ({wkt_points})", bbox, cls, None, connectors,
        speed_limits, access_restrictions, names,
    )


# --- Interior-connector crossing fixture (issue #37) ----------------------
# Two segments that meet only at a shared *interior* connector (at == 0.5 on
# both) — neither segment terminates there. Placed at a fractional-metre
# offset from ORIGIN so it doesn't land on the grid lattice and collide
# with a real grid edge. Regression coverage for: interior connectors must
# become graph nodes, and two segments sharing only one must be linked
# (a real-world mid-block crossing that Overture doesn't represent as a
# shared endpoint).
CROSS_CENTER_M = (1730.0, 130.0)  # (east_m, north_m) from ORIGIN
CROSS_HALF_LEN_M = 20.0
CROSS_CONNECTOR_ID = "x_cross"


def cross_center_latlon() -> tuple[float, float]:
    return _offset_m(*CROSS_CENTER_M)


def build_cross_rows(start_idx: int) -> list[tuple]:
    cx_m, cy_m = CROSS_CENTER_M
    a0 = _offset_m(cx_m - CROSS_HALF_LEN_M, cy_m)
    a1 = _offset_m(cx_m + CROSS_HALF_LEN_M, cy_m)
    b0 = _offset_m(cx_m, cy_m - CROSS_HALF_LEN_M)
    b1 = _offset_m(cx_m, cy_m + CROSS_HALF_LEN_M)
    return [
        _segment_row(start_idx, a0, a1, "residential", [
            {"connector_id": "x_a0", "at": 0.0},
            {"connector_id": CROSS_CONNECTOR_ID, "at": 0.5},
            {"connector_id": "x_a1", "at": 1.0},
        ]),
        _segment_row(start_idx + 1, b0, b1, "residential", [
            {"connector_id": "x_b0", "at": 0.0},
            {"connector_id": CROSS_CONNECTOR_ID, "at": 0.5},
            {"connector_id": "x_b1", "at": 1.0},
        ]),
    ]


# --- Isolated fragment near a real grid node (issue #37) -------------------
# A disconnected 2-node segment placed closer to isolated_query_latlon()
# than the nearest real grid node — regression coverage for component-aware
# snapping: the origin must not snap onto this fragment just because it's
# geometrically nearest, since its (2-node) component is far below the
# "usable" threshold.
ISOLATED_ANCHOR_NODE = (5, 5)
ISOLATED_QUERY_OFFSET_M = (6.0, 30.0)  # (distance_m, bearing_deg) from the anchor node
ISOLATED_FRAGMENT_OFFSET_M = (2.0, 200.0)  # (distance_m, bearing_deg) from the query point
ISOLATED_FRAGMENT_LENGTH_M = 12.0


def isolated_query_latlon() -> tuple[float, float]:
    anchor_lat, anchor_lon = node_latlon(*ISOLATED_ANCHOR_NODE)
    dist_m, bearing = ISOLATED_QUERY_OFFSET_M
    return _offset(anchor_lat, anchor_lon, dist_m, bearing)


def build_isolated_fragment_rows(start_idx: int) -> list[tuple]:
    qlat, qlon = isolated_query_latlon()
    dist_m, bearing = ISOLATED_FRAGMENT_OFFSET_M
    f0 = _offset(qlat, qlon, dist_m, bearing)
    f1 = _offset(f0[0], f0[1], ISOLATED_FRAGMENT_LENGTH_M, bearing + 90)
    return [
        _segment_row(start_idx, f0, f1, "footway", [
            {"connector_id": "frag_0", "at": 0.0},
            {"connector_id": "frag_1", "at": 1.0},
        ]),
    ]


# --- One-way pair (issue #38) -----------------------------------------------
# A single residential segment, off the grid lattice, whose access_restrictions
# deny backward travel — regression coverage for directed-edge handling: A -> B
# must be routable (cycle/drive), B -> A must not be, and walk must ignore the
# restriction entirely (routable both ways).
ONEWAY_CENTER_M = (1730.0, 500.0)  # (east_m, north_m) from ORIGIN
ONEWAY_HALF_LEN_M = 30.0
ONEWAY_A_ID = "ow_a"
ONEWAY_B_ID = "ow_b"


def oneway_endpoints_latlon() -> tuple[tuple[float, float], tuple[float, float]]:
    cx_m, cy_m = ONEWAY_CENTER_M
    a = _offset_m(cx_m - ONEWAY_HALF_LEN_M, cy_m)
    b = _offset_m(cx_m + ONEWAY_HALF_LEN_M, cy_m)
    return a, b


def oneway_center_latlon() -> tuple[float, float]:
    return _offset_m(*ONEWAY_CENTER_M)


def build_oneway_rows(start_idx: int) -> list[tuple]:
    a, b = oneway_endpoints_latlon()
    access_restrictions = [
        {"access_type": "denied", "when": {"heading": "backward", "mode": []}, "between": None},
    ]
    return [
        _segment_row(
            start_idx, a, b, "residential",
            [
                {"connector_id": ONEWAY_A_ID, "at": 0.0},
                {"connector_id": ONEWAY_B_ID, "at": 1.0},
            ],
            access_restrictions=access_restrictions,
        ),
    ]


# --- Switchback spur (#161 sweep) ------------------------------------------
# A single segment with a big interior detour between its two connectors:
# every other fixture segment is a straight two-point line, so nothing here
# could ever catch a router that emits chords between graph nodes and
# discards the road's real shape. Built as a dead-end spur hanging off the
# grid's south-east corner node, east of the lattice, so it adds no new
# route between existing grid nodes and changes no existing distance.
SWITCHBACK_ANCHOR_NODE = (GRID_N - 1, 0)
SWITCHBACK_END_ID = "sw_end"
# (east_m, north_m) from ORIGIN, after the anchor node. Zig north / east /
# south repeatedly: ~850 m of road across a ~430 m chord.
SWITCHBACK_OFFSETS_M = [
    (2000.0, 0.0), (2000.0, 150.0), (2100.0, 150.0), (2100.0, 0.0),
    (2200.0, 0.0), (2200.0, 150.0), (2300.0, 150.0),
]


def switchback_endpoints_latlon() -> tuple[tuple[float, float], tuple[float, float]]:
    """((lat, lon) of the anchored start, (lat, lon) of the far end)."""
    return node_latlon(*SWITCHBACK_ANCHOR_NODE), _offset_m(*SWITCHBACK_OFFSETS_M[-1])


def build_switchback_rows(start_idx: int) -> list[tuple]:
    start, _end = switchback_endpoints_latlon()
    points = [start] + [_offset_m(*p) for p in SWITCHBACK_OFFSETS_M]
    return [
        _polyline_row(start_idx, points, "residential", [
            {"connector_id": node_id(*SWITCHBACK_ANCHOR_NODE), "at": 0.0},
            {"connector_id": SWITCHBACK_END_ID, "at": 1.0},
        ]),
    ]


def _edge_allowed(i: int, j: int, i2: int, j2: int) -> bool:
    if {i, i2} == {RIVER_GAP_I, RIVER_GAP_I + 1} and j == j2 and j != BRIDGE_J:
        return False
    if ((i, j), (i2, j2)) in MISSING_EDGES or ((i2, j2), (i, j)) in MISSING_EDGES:
        return False
    return True


def build_edges() -> list[tuple[tuple[int, int], tuple[int, int], str, str | None]]:
    """List of (node_a, node_b, class, name) quads.

    Horizontal edges get a per-row "Grid Ave {j}" name and vertical edges a
    per-column "Grid St {i}" name (#441: map_match's road-name aggregation
    needs at least one real, findable street name in the fixture) — the
    bridge, diagonals, and motorway shortcut stay unnamed (None), same as
    most real-world minor/informal paths.
    """
    edges = []

    # Horizontal.
    for i in range(GRID_N - 1):
        for j in range(GRID_N):
            if _edge_allowed(i, j, i + 1, j):
                is_bridge = i == RIVER_GAP_I and j == BRIDGE_J
                cls = "footway" if is_bridge else "residential"
                name = None if is_bridge else f"Grid Ave {j}"
                edges.append(((i, j), (i + 1, j), cls, name))

    # Vertical — the river runs north/south along the column boundary and
    # doesn't block north/south movement.
    for i in range(GRID_N):
        for j in range(GRID_N - 1):
            if _edge_allowed(i, j, i, j + 1):
                edges.append(((i, j), (i, j + 1), "residential", f"Grid St {i}"))

    # Diagonals, skipping the river column so they don't smuggle a second
    # east/west crossing.
    for i in range(0, GRID_N - 1, DIAG_STEP):
        for j in range(0, GRID_N - 1, DIAG_STEP):
            if i == RIVER_GAP_I:
                continue
            edges.append(((i, j), (i + 1, j + 1), "path", None))

    # Motorway shortcut — excluded from the walkable graph.
    edges.append((SHORTCUT["from"], SHORTCUT["to"], SHORTCUT["cls"], None))

    return edges


def build_rows() -> list[tuple]:
    edges = build_edges()
    rows = []
    for idx, (a, b, cls, name) in enumerate(edges):
        lat1, lon1 = node_latlon(*a)
        lat2, lon2 = node_latlon(*b)
        connectors = [
            {"connector_id": node_id(*a), "at": 0.0},
            {"connector_id": node_id(*b), "at": 1.0},
        ]
        speed_limits = None
        if (a, b, cls) == (SHORTCUT["from"], SHORTCUT["to"], SHORTCUT["cls"]):
            speed_limits = [
                {
                    "max_speed": {"value": SHORTCUT_SPEED_LIMIT_KMH, "unit": "km/h"},
                    "when": None,
                    "between": None,
                },
            ]
        rows.append(
            _segment_row(
                idx, (lat1, lon1), (lat2, lon2), cls, connectors,
                speed_limits=speed_limits, name=name,
            )
        )

    rows.extend(build_cross_rows(len(rows)))
    rows.extend(build_isolated_fragment_rows(len(rows)))
    rows.extend(build_oneway_rows(len(rows)))
    rows.extend(build_switchback_rows(len(rows)))
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
            connectors STRUCT(connector_id VARCHAR, "at" DOUBLE)[],
            speed_limits STRUCT(
                max_speed STRUCT(value DOUBLE, unit VARCHAR),
                "when" STRUCT(heading VARCHAR, mode VARCHAR[]),
                between DOUBLE[]
            )[],
            access_restrictions STRUCT(
                access_type VARCHAR,
                "when" STRUCT(heading VARCHAR, mode VARCHAR[]),
                between DOUBLE[]
            )[],
            names STRUCT("primary" VARCHAR)
        )
    """)
    con.executemany("INSERT INTO staging VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
            SELECT
                id,
                ST_AsWKB(ST_GeomFromText(wkt)) AS geometry,
                bbox, class, subclass, connectors, speed_limits, access_restrictions, names
            FROM staging
        ) TO '{FIXTURE_PATH}' (FORMAT PARQUET)
    """)
    print(f"wrote {len(rows)} rows to {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
