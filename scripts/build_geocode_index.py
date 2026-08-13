#!/usr/bin/env python3
"""Builds the wheel-bundled stage-0 geocode index (see geocode.py).

    uv run python scripts/build_geocode_index.py [release]

The full local divisions name table (#43) answers geocode in
milliseconds — after a ~216MB first-call build. This index is the part of
that table almost every real query actually touches: the top
_INDEX_ROWS divisions by population (every city, town and district of
consequence, worldwide — the names people geocode and the anchors
address/place searches split off), in exactly the schema
_query_divisions_from_local reads, ~6MB zstd. Bundled per release beside
the manifests, it makes the first geocode of a fresh install local and
instant while the full table builds in the background; only long-tail
names (an unpopulated hamlet) pay a live scan before that build lands.

Regenerate alongside the manifests when a new Overture release is pinned.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb


def _copy_verified(con, sql: str, out: Path, min_bytes: int = 100_000) -> None:
    """Run a COPY and verify the output is a real parquet, retrying once.

    Twice observed on this machine: a remote-sourced COPY completing with a
    zero-byte output file and no exception. A truncated bundle artifact
    must never be committed, so verify-and-retry, and fail loudly if the
    retry produces the same.
    """
    for attempt in (1, 2):
        con.execute(sql)
        if out.stat().st_size >= min_bytes:
            return
        print(f"  attempt {attempt}: {out} came back {out.stat().st_size} bytes; retrying")
    raise SystemExit(f"COPY produced an invalid file twice: {out}")


ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "src" / "placeroot" / "data" / "geocode-index"

UPSTREAM_BASE = "s3://overturemaps-us-west-2/release"
_INDEX_ROWS = 150_000


def main() -> int:
    release = sys.argv[1] if len(sys.argv) > 1 else None
    if release is None:
        sys.path.insert(0, str(ROOT / "src"))
        from placeroot import release as release_mod

        release = release_mod.PINNED_RELEASE
    glob = f"{UPSTREAM_BASE}/{release}/theme=divisions/type=division/*"
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2'; SET s3_access_key_id=''; SET s3_secret_access_key='';")
    con.execute("SET threads=64;")
    out_dir = OUT_ROOT / release
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "table.parquet"
    # Same columns, same names, as _materialize_divisions_pass writes, so
    # _query_divisions_from_local reads either file identically. admin_chain
    # is NULL like a stage-1 build (hierarchies are 145MB of the build cost);
    # the bbox corner columns come from the row's own (point) bbox, which
    # #224 treats as degenerate and falls back past — exactly right here.
    _copy_verified(con, f"""
        COPY (
            SELECT id, names.primary AS name, subtype, country, region,
                   bbox.ymin AS lat, bbox.xmin AS lon, population,
                   NULL::VARCHAR[] AS admin_chain,
                   bbox.xmin AS bbox_xmin, bbox.ymin AS bbox_ymin,
                   bbox.xmax AS bbox_xmax, bbox.ymax AS bbox_ymax
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE names.primary IS NOT NULL AND population IS NOT NULL AND population > 0
            ORDER BY population DESC
            LIMIT {_INDEX_ROWS}
        ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """, out)
    size_mb = out.stat().st_size / 1048576
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    print(f"{n:,} rows -> {out.relative_to(ROOT)} ({size_mb:.1f} MB)")

    # The alternate-name rows for the indexed divisions, in the app's own
    # alt-table shape (geocode._materialize_alt_names_table builds the full
    # one): without them, "Tokyo" cannot find 東京都 through the bundled
    # index — the exact failure the alt search exists for. Restricted to
    # the index's ids so it stays wheel-sized.
    sys.path.insert(0, str(ROOT / "src"))
    from placeroot import geocode as geocode_mod

    alt_out = out_dir / "alt_names.parquet"
    fold = geocode_mod._fold_alt_name_sql
    _copy_verified(con, f"""
        COPY (
            SELECT id, alt_name, min(alt) AS alt_display
            FROM (
                SELECT id, alt, primary_folded, {fold("alt")} AS alt_name
                FROM (
                    SELECT id,
                           {fold("names.primary")} AS primary_folded,
                           unnest(map_values(names.common)) AS alt
                    FROM read_parquet('{glob}', hive_partitioning=1)
                    WHERE names.common IS NOT NULL
                      AND id IN (SELECT id FROM read_parquet('{out}'))
                )
            )
            WHERE alt_name IS NOT NULL AND alt_name <> '' AND alt_name <> primary_folded
            GROUP BY id, alt_name
        ) TO '{alt_out}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """, alt_out)
    alt_mb = alt_out.stat().st_size / 1048576
    alt_n = con.execute(f"SELECT count(*) FROM read_parquet('{alt_out}')").fetchone()[0]
    print(f"{alt_n:,} alt rows -> {alt_out.relative_to(ROOT)} ({alt_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
