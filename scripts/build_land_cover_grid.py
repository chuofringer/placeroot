#!/usr/bin/env python3
"""Builds the wheel-bundled coarse land-cover grid (see land_use.py).

    uv run python scripts/build_land_cover_grid.py [release]

land_cover is the one dataset whose cold exact answer is bandwidth-bound:
its polygons include continent-scale multipolygons, and a containment
lookup must move their geometry across the wire (measured 10-25s on a
residential pipe). But land *cover* is a coarse fact by nature — forest,
urban, grassland — so a 0.1° (~11 km) grid answers the cold query
instantly and honestly, flagged as approximate; the exact polygon path
still serves once the area's tiles are warm.

The grid is painted from the dataset's own low-zoom generalization: the
cartography.min_zoom = 0 band is ~26k polygons (vs 123M in the fine
band) — the exact set Overture generalized for world-scale rendering.
Each cell takes the smallest polygon of that band containing its center
(real ST_Contains, same smallest-first pick the runtime's exact path
makes), so ocean cells are genuinely absent and a cell's answer is what
a zoom-7 map of the release would show there. A bbox-only heuristic was
tried first and rejected: smallest-bbox-touching-the-cell paints the
Amazon "barren" from tiny inholdings and mid-ocean "forest" from
continent-spanning bboxes.

Regenerate alongside the manifests when a new release is pinned.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "src" / "placeroot" / "data" / "land-cover-grid"

UPSTREAM_BASE = "s3://overturemaps-us-west-2/release"
GRID_DEG = 0.1


def main() -> int:
    release = sys.argv[1] if len(sys.argv) > 1 else None
    if release is None:
        sys.path.insert(0, str(ROOT / "src"))
        from placeroot import release as release_mod

        release = release_mod.PINNED_RELEASE
    glob = f"{UPSTREAM_BASE}/{release}/theme=base/type=land_cover/*"
    # The paint join OOMs an in-memory database (hit at 6.3 GiB): a
    # disk-backed db with a hard memory cap spills instead. The staged
    # coarse band also makes a re-run skip the remote pass entirely.
    import tempfile

    work = Path(tempfile.gettempdir()) / f"land-cover-grid-{release}"
    work.mkdir(parents=True, exist_ok=True)
    staged = work / "coarse.parquet"
    con = duckdb.connect(str(work / "build.duckdb"))
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2'; SET s3_access_key_id=''; SET s3_secret_access_key='';")
    con.execute("SET memory_limit='4GB'; SET preserve_insertion_order=false;")
    con.execute(f"SET temp_directory='{work / 'spill'}';")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    out = OUT_ROOT / f"{release}.parquet"

    # One remote pass: pull the low-zoom band local (geometry included) so
    # the containment join below never touches the network.
    if not (staged.exists() and staged.stat().st_size >= 100_000):
        con.execute("SET threads=96;")
        con.execute(f"""
            COPY (
                SELECT subtype, geometry, ST_Area(geometry) AS area,
                       bbox.xmin AS xmin, bbox.ymin AS ymin,
                       bbox.xmax AS xmax, bbox.ymax AS ymax
                FROM read_parquet('{glob}', hive_partitioning=1)
                WHERE cartography.min_zoom = 0 AND subtype IS NOT NULL
            ) TO '{staged}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    con.execute("SET threads=8;")
    n_coarse = con.execute(f"SELECT count(*) FROM read_parquet('{staged}')").fetchone()[0]
    staged_mb = staged.stat().st_size / 1048576
    print(f"coarse band: {n_coarse:,} polygons ({staged_mb:.0f} MB staged)", flush=True)
    con.execute(f"""
        CREATE OR REPLACE TABLE coarse AS
        SELECT row_number() OVER () AS pid, * FROM read_parquet('{staged}')
    """)

    # Candidate cells per polygon: every cell whose CENTER lies inside the
    # polygon's bbox (center of cell g is (g+0.5)*GRID_DEG). Materialized
    # WITHOUT geometry — a LATERAL that carries geometry into the pair
    # stream copies it per pair and OOMs (~5.7M pairs, hit twice at the
    # 4-6 GiB memory cap). The exact ST_Contains test happens in the join
    # below, where geometry lives only on the 26k-row build side and the
    # pair stream probes it vector-at-a-time.
    con.execute(f"""
        CREATE OR REPLACE TABLE pairs AS
        SELECT p.pid,
               CAST(gxs.generate_series AS SMALLINT) AS gx,
               CAST(gys.generate_series AS SMALLINT) AS gy
        FROM (SELECT pid, xmin, ymin, xmax, ymax FROM coarse) p,
             LATERAL generate_series(
                 CAST(ceil(p.xmin / {GRID_DEG} - 0.5) AS INTEGER),
                 CAST(floor(p.xmax / {GRID_DEG} - 0.5) AS INTEGER)) gxs,
             LATERAL generate_series(
                 CAST(ceil(p.ymin / {GRID_DEG} - 0.5) AS INTEGER),
                 CAST(floor(p.ymax / {GRID_DEG} - 0.5) AS INTEGER)) gys
    """)
    n_pairs = con.execute("SELECT count(*) FROM pairs").fetchone()[0]
    print(f"candidate pairs: {n_pairs:,}", flush=True)

    # Smallest containing polygon wins the cell — the runtime exact path's
    # own ordering.
    sql = f"""
        COPY (
            SELECT p.gx, p.gy, arg_min(c.subtype, c.area) AS subtype
            FROM pairs p JOIN coarse c ON p.pid = c.pid
            WHERE ST_Contains(c.geometry, ST_Point(
                      (p.gx + 0.5) * {GRID_DEG},
                      (p.gy + 0.5) * {GRID_DEG}))
            GROUP BY p.gx, p.gy
            ORDER BY p.gx, p.gy
        ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    # Same zero-byte-COPY hazard build_geocode_index.py hit (a remote-sourced
    # COPY completing with an empty file and no exception): verify and retry
    # once, fail loudly rather than ship a truncated bundle artifact.
    for attempt in (1, 2):
        con.execute(sql)
        if out.stat().st_size >= 100_000:
            break
        print(f"  attempt {attempt}: {out} came back {out.stat().st_size} bytes; retrying")
    size = out.stat().st_size
    if size < 100_000:
        raise SystemExit(f"grid came back implausibly small twice ({size} bytes): {out}")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    print(f"{n:,} cells at {GRID_DEG}deg -> {out.relative_to(ROOT)} ({size / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
