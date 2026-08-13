#!/usr/bin/env python3
"""Builds the bundled per-release file-extent manifests (see manifest.py).

    uv run python scripts/build_release_manifest.py [release]

For each queried (theme, type), reads every parquet file's row-group
statistics for the bbox columns (one footer pass per theme — the same pass
a cold query would pay) and records each file's overall spatial extent.
The output JSONs ship inside the wheel under
src/placeroot/data/manifests/<release>/, so a fresh install's very first
bbox-bounded query can read only the files its box intersects instead of
paying a footer read per file per theme (buildings: 512 files, ~25s
measured at 64 threads).

Run against the release being pinned when preparing one; CI's release flow
should regenerate on release bumps. A release is immutable once published,
so a manifest generated once is correct forever for that release.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "src" / "placeroot" / "data" / "manifests"

THEME_TYPES = [
    ("places", "place"),
    ("buildings", "building"),
    ("transportation", "segment"),
    ("transportation", "connector"),
    ("base", "land_use"),
    ("base", "land_cover"),
    ("base", "land"),
    ("base", "water"),
    ("base", "infrastructure"),
    ("divisions", "division"),
    ("divisions", "division_area"),
    ("addresses", "address"),
]

UPSTREAM_BASE = "s3://overturemaps-us-west-2/release"


def build_one(
    con, release: str, theme: str, type_: str
) -> tuple[dict[str, list[float]], list[str]]:
    glob = f"{UPSTREAM_BASE}/{release}/theme={theme}/type={type_}/*"
    columns = [
        r[0] for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{glob}', hive_partitioning=1) LIMIT 0"
        ).fetchall()
    ]
    rows = con.execute(
        """
        SELECT parse_filename(file_name) AS file,
               min(CASE WHEN path_in_schema = 'bbox, xmin'
                        THEN CAST(stats_min_value AS DOUBLE) END) AS xmin,
               min(CASE WHEN path_in_schema = 'bbox, ymin'
                        THEN CAST(stats_min_value AS DOUBLE) END) AS ymin,
               max(CASE WHEN path_in_schema = 'bbox, xmax'
                        THEN CAST(stats_max_value AS DOUBLE) END) AS xmax,
               max(CASE WHEN path_in_schema = 'bbox, ymax'
                        THEN CAST(stats_max_value AS DOUBLE) END) AS ymax,
               count(*) FILTER (
                   WHERE path_in_schema IN
                         ('bbox, xmin', 'bbox, ymin', 'bbox, xmax', 'bbox, ymax')
                     AND (stats_min_value IS NULL OR stats_max_value IS NULL)
               ) AS missing_stats
        FROM parquet_metadata($glob)
        GROUP BY 1 ORDER BY 1
        """,
        {"glob": glob},
    ).fetchall()
    files: dict[str, list[float]] = {}
    for file, xmin, ymin, xmax, ymax, missing_stats in rows:
        if missing_stats or None in (xmin, ymin, xmax, ymax):
            # ANY row group without bbox stats makes the file unprunable —
            # an aggregate over only the stats-bearing groups would shrink
            # the extent and silently drop rows. World extent keeps it in
            # every candidate list.
            files[file] = [-180.0, -90.0, 180.0, 90.0]
        else:
            # Floor mins / ceil maxes: plain round() can round an extent
            # inward, and a feature in that sliver at a file edge would be
            # wrongly pruned. The recorded extent must be a superset.
            files[file] = [
                math.floor(xmin * 1e5) / 1e5, math.floor(ymin * 1e5) / 1e5,
                math.ceil(xmax * 1e5) / 1e5, math.ceil(ymax * 1e5) / 1e5,
            ]
    return files, columns


def main() -> int:
    release = sys.argv[1] if len(sys.argv) > 1 else None
    if release is None:
        sys.path.insert(0, str(ROOT / "src"))
        from placeroot import release as release_mod

        release = release_mod.resolve_release()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2'; SET s3_access_key_id=''; SET s3_secret_access_key='';")
    con.execute("SET threads=96;")
    out_dir = OUT_ROOT / release
    out_dir.mkdir(parents=True, exist_ok=True)
    for theme, type_ in THEME_TYPES:
        files, columns = build_one(con, release, theme, type_)
        out = out_dir / f"{theme}__{type_}.json"
        out.write_text(json.dumps(
            {"release": release, "theme": theme, "type": type_,
             "columns": columns, "files": files},
            separators=(",", ":"),
        ))
        print(f"{theme}/{type_}: {len(files)} files, {len(columns)} columns "
              f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
