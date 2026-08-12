#!/usr/bin/env python3
"""Builds the wheel-bundled coarse land-cover grid (see land_use.py).

    uv run python scripts/build_land_cover_grid.py [release]

land_cover is the one dataset whose cold exact answer is bandwidth-bound:
its polygons include continent-scale multipolygons, and a containment
lookup must move their geometry across the wire (measured 10-25s on a
residential pipe). But land *cover* is a coarse fact by nature — forest,
urban, grassland — so a 0.1° (~11 km) grid of "smallest polygon whose
bbox covers this cell" answers the cold query instantly and honestly,
flagged as approximate; the exact polygon path still serves once the
area's tiles are warm.

Each polygon's bbox is exploded onto the cells it covers (~148M cell
paints from 123M polygons at 0.1°, measured on 2026-07-22.0) and each
cell keeps the subtype of the smallest-bbox polygon touching it — the
same smallest-first heuristic the runtime's own candidate ordering uses.
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
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2'; SET s3_access_key_id=''; SET s3_secret_access_key='';")
    con.execute("SET threads=96;")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    out = OUT_ROOT / f"{release}.parquet"
    sql = f"""
        COPY (
            WITH polys AS (
                SELECT subtype,
                       (bbox.xmax - bbox.xmin) * (bbox.ymax - bbox.ymin) AS a,
                       CAST(floor(bbox.xmin / {GRID_DEG}) AS INTEGER) AS gx0,
                       CAST(floor(bbox.xmax / {GRID_DEG}) AS INTEGER) AS gx1,
                       CAST(floor(bbox.ymin / {GRID_DEG}) AS INTEGER) AS gy0,
                       CAST(floor(bbox.ymax / {GRID_DEG}) AS INTEGER) AS gy1
                FROM read_parquet('{glob}', hive_partitioning=1)
                WHERE subtype IS NOT NULL
            ),
            cells AS (
                SELECT p.subtype, p.a,
                       CAST(gxs.generate_series AS SMALLINT) AS gx,
                       CAST(gys.generate_series AS SMALLINT) AS gy
                FROM polys p,
                     LATERAL generate_series(p.gx0, p.gx1) gxs,
                     LATERAL generate_series(p.gy0, p.gy1) gys
            )
            SELECT gx, gy, arg_min(subtype, a) AS subtype
            FROM cells
            GROUP BY gx, gy
            ORDER BY gx, gy
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
