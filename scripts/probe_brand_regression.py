#!/usr/bin/env python3
"""Explain a canary field-coverage regression between two Overture releases.

    uv run python scripts/probe_brand_regression.py [--pinned R] [--newest R] [--metro NAME]

The weekly canary (scripts/overture_canary.py, #416) reports a *rate* —
brand non-null / places rows — and flags a >20% relative drop. A rate can
fall for three very different reasons, and the canary cannot tell them
apart:

  1. values collapsed: the same POIs are still there but `brand` went null
     (OvertureMaps/data#546's shape — the thing the gate exists for);
  2. branded POIs were removed from the release;
  3. the denominator grew: many *new* unbranded POIs were added, so the
     share fell while the branded count held.

This script joins the two releases on GERS id inside each probe bbox and
counts each case, plus the top brands by count on both sides. Network-only
(reads the public bucket through the runtime's own connection setup); run
it from the overture-probe workflow or a machine with S3 egress.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "scripts")

from overture_canary import PROBE_METROS, PROBE_RADIUS_M  # noqa: E402

from placeroot import db, geo, overture, release  # noqa: E402


def places_glob(rel: str) -> str:
    return f"{overture.DEFAULT_UPSTREAM_BASE}/{rel}/theme=places/type=place/*"


def probe(con, pinned: str, newest: str, name: str, lat: float, lon: float) -> None:
    xmin, ymin, xmax, ymax = geo.bbox_around(lat, lon, PROBE_RADIUS_M)
    filter_sql, params = geo.bbox_filter_sql(xmin, ymin, xmax, ymax)
    base = """
        SELECT id,
               brand.names.primary AS brand,
               names.primary AS name,
               confidence,
               taxonomy.primary AS category
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE {flt}
    """
    for table, rel in (("a", pinned), ("b", newest)):
        select = base.format(glob=places_glob(rel), flt=filter_sql)
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS {select}", params)
    (na, ba) = con.execute("SELECT count(*), count(brand) FROM a").fetchone()
    (nb, bb) = con.execute("SELECT count(*), count(brand) FROM b").fetchone()
    print(f"\n## {name}")
    print("| release | places rows | brand non-null | rate |")
    print("|---|---:|---:|---:|")
    print(f"| {pinned} (pinned) | {na} | {ba} | {ba / na if na else 0:.4f} |")
    print(f"| {newest} (newest) | {nb} | {bb} | {bb / nb if nb else 0:.4f} |")

    rows = con.execute(
        """
        SELECT
          count(*) FILTER (WHERE a.id IS NOT NULL AND b.id IS NOT NULL) AS same_id,
          count(*) FILTER (WHERE a.id IS NOT NULL AND b.id IS NULL)     AS removed,
          count(*) FILTER (WHERE a.id IS NULL AND b.id IS NOT NULL)     AS added,
          count(*) FILTER (WHERE a.brand IS NOT NULL AND b.id IS NOT NULL
                             AND b.brand IS NULL)                        AS brand_collapsed,
          count(*) FILTER (WHERE a.brand IS NOT NULL AND b.id IS NULL)   AS branded_removed,
          count(*) FILTER (WHERE a.id IS NULL AND b.id IS NOT NULL
                             AND b.brand IS NULL)                        AS added_unbranded,
          count(*) FILTER (WHERE a.id IS NULL AND b.id IS NOT NULL
                             AND b.brand IS NOT NULL)                    AS added_branded,
          count(*) FILTER (WHERE a.id IS NOT NULL AND b.id IS NOT NULL
                             AND a.brand IS NULL AND b.brand IS NOT NULL) AS brand_gained
        FROM a FULL OUTER JOIN b USING (id)
        """
    ).fetchone()
    keys = [
        "same_id",
        "removed",
        "added",
        "brand_collapsed",
        "branded_removed",
        "added_unbranded",
        "added_branded",
        "brand_gained",
    ]
    print("\nid-level diff (pinned -> newest):")
    for k, v in zip(keys, rows):
        print(f"- {k}: {v}")

    print("\ntop brands, pinned vs newest (count):")
    top = con.execute(
        """
        WITH ca AS (SELECT brand, count(*) n FROM a WHERE brand IS NOT NULL GROUP BY 1),
             cb AS (SELECT brand, count(*) n FROM b WHERE brand IS NOT NULL GROUP BY 1)
        SELECT coalesce(ca.brand, cb.brand) brand,
               coalesce(ca.n, 0) pinned, coalesce(cb.n, 0) newest
        FROM ca FULL OUTER JOIN cb USING (brand)
        ORDER BY abs(coalesce(ca.n, 0) - coalesce(cb.n, 0)) DESC, pinned DESC
        LIMIT 25
        """
    ).fetchall()
    print("| brand | pinned | newest |")
    print("|---|---:|---:|")
    for brand, p, n in top:
        print(f"| {brand} | {p} | {n} |")

    cat = con.execute(
        """
        WITH ca AS (SELECT category, count(*) n FROM a GROUP BY 1),
             cb AS (SELECT category, count(*) n FROM b GROUP BY 1)
        SELECT coalesce(ca.category, cb.category) category,
               coalesce(ca.n, 0) pinned, coalesce(cb.n, 0) newest
        FROM ca FULL OUTER JOIN cb USING (category)
        ORDER BY (coalesce(cb.n, 0) - coalesce(ca.n, 0)) DESC
        LIMIT 12
        """
    ).fetchall()
    print("\ncategories that grew most (newest - pinned):")
    print("| category | pinned | newest |")
    print("|---|---:|---:|")
    for c, p, n in cat:
        print(f"| {c} | {p} | {n} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pinned", default=release.PINNED_RELEASE)
    ap.add_argument("--newest", default=None)
    ap.add_argument("--metro", default=None, help="probe only this PROBE_METROS name")
    args = ap.parse_args()
    newest = args.newest or release._discover(timeout_s=30.0)
    if newest is None:
        print("release discovery failed", file=sys.stderr)
        return 1
    print(f"# Brand-coverage probe: {args.pinned} -> {newest}")
    con = db.new_connection()
    for name, lat, lon in PROBE_METROS:
        if args.metro and name != args.metro:
            continue
        probe(con, args.pinned, newest, name, lat, lon)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
