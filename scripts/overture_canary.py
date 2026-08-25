"""Weekly Overture release canary (#219): stale pin + schema drift, loudly.

Two questions, answered against the live public bucket:

1. Is `release.PINNED_RELEASE` still upstream's newest release? The pin is
   the fallback whenever discovery fails, and every fixture pins the same
   string, so it drifting means a discovery outage serves increasingly old
   data and nobody notices.
2. Does the *newest* release still carry every column we require, per theme?
   Runtime degrades gracefully when a column vanishes (#5), but degradation
   is a safety net, not a notification — this probe is the notification,
   fired before the TTL rollover (#219) adopts the new release in
   production.
3. Does the *newest* release still carry the same field COVERAGE as the
   pinned one, over a small fixed set of dense metro bboxes (#416)?
   OvertureMaps/data#546 documents a release where every required column
   stayed present but the values behind `brand`/`confidence`/`taxonomy`
   collapsed for a whole country (median -21% brand-matched POIs, some
   chains to zero) with no schema change at all — (2) above would have
   sailed straight through that release. This probe re-runs the same
   column-presence-blind bbox scan against both the pinned and the newest
   release and flags a large relative drop.

Exit code 0 when all three hold; 1 with a markdown report on stdout
otherwise — the workflow turns that report into a GitHub issue.
Network-dependent by design; never run from pytest.

Usage:
    uv run python scripts/overture_canary.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import duckdb  # noqa: E402

from placeroot import (  # noqa: E402
    addresses,
    buildings,
    db,
    divisions,
    geo,
    infrastructure,
    land_use,
    overture,
    release,
    routing,
    water,
)

# (theme, type, required columns) — the same lists the runtime degrades
# against, referenced rather than restated so the canary can't drift from the
# code. Every dataset the server reads appears exactly once:
# divisions.REQUIRED_COLUMNS is a superset of overture's division_area list,
# and land_cover gets its own list because it carries neither class nor names
# upstream (probing it with land_use's list would report by-design absences as
# drift every week).
THEME_REQUIREMENTS: list[tuple[str, str, list[str]]] = [
    ("places", "place", overture.REQUIRED_COLUMNS),
    ("divisions", "division_area", divisions.REQUIRED_COLUMNS),
    ("divisions", "division", addresses.DIVISION_REQUIRED_COLUMNS),
    ("addresses", "address", addresses.REQUIRED_COLUMNS),
    ("buildings", "building", buildings.REQUIRED_COLUMNS),
    ("base", "land_use", land_use.REQUIRED_COLUMNS),
    ("base", "land_cover", land_use.LAND_COVER_REQUIRED_COLUMNS),
    ("base", "infrastructure", infrastructure.REQUIRED_COLUMNS),
    ("base", "water", water.REQUIRED_COLUMNS),
    ("transportation", "segment", routing.REQUIRED_COLUMNS),
]


def probe_columns(con: duckdb.DuckDBPyConnection, glob: str) -> set[str] | None:
    """Top-level column names of the dataset at `glob`, or None on failure."""
    try:
        rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{glob}', hive_partitioning=1)"
        ).fetchall()
    except duckdb.Error as e:
        print(f"probe failed for {glob}: {e}", file=sys.stderr)
        return None
    return {r[0] for r in rows}


# --- #416 field-coverage gate -----------------------------------------------
#
# (name, lat, lon) for five dense metros spread across continents/hemispheres
# — a values-collapsed release (OvertureMaps/data#546) hit one country's
# brand matches, so no single bbox would reliably catch the next one. Picked
# for density (a small bbox still returns a useful row count) and stability
# (city-center coordinates, not anything likely to move or empty out).
PROBE_METROS: list[tuple[str, float, float]] = [
    ("Central Paris", 48.8566, 2.3522),  # Europe — Ile de la Cite / Louvre area
    ("Manhattan", 40.7549, -73.9840),  # North America — Midtown
    ("Tokyo (Shibuya)", 35.6595, 139.7005),  # Asia — Shibuya crossing area
    ("Sao Paulo (Se)", -23.5505, -46.6333),  # South America — historic center
    ("Lagos Island", 6.4550, 3.3841),  # Africa — the metric this gate exists for (#546)
]

# geo.bbox_around takes a half-width: 5_500m makes each box ~0.1 deg in
# half-height (~0.2 deg tall in total, wider in longitude *degrees* away
# from the equator, where a degree of longitude covers fewer meters) —
# the "~0.1 degree class" the issue asked for, while still covering
# enough of a dense downtown to make row counts meaningful.
PROBE_RADIUS_M = 5_500.0

# Relative drop (newest vs pinned) on any one probe metric that flags a
# regression. OvertureMaps/data#546's Brazil brand-collapse measured a
# *median* -21% drop (worse for some chains, to zero) with no schema
# change — 0.20 sits just under that median so the canary catches an
# incident of that shape without chasing ordinary release-to-release noise.
REGRESSION_TOLERANCE = 0.20

# Floor on the pinned-side row count a metric's denominator must clear
# before its percentage is trusted. A bbox with a handful of pinned rows
# turns "one row disappeared" into a 100% "drop" — noise, not signal.
MIN_ROWS = 50

# Metric keys, in report order. "*_rows" are raw row counts (their own
# denominator); the "*_non_null_rate" metrics are non-null fractions among
# a bbox's places rows, so they share places_rows as their denominator.
COVERAGE_METRICS: tuple[str, ...] = (
    "places_rows",
    "brand_non_null_rate",
    "confidence_non_null_rate",
    "category_non_null_rate",
    "addresses_rows",
)

# Which metric in a bbox's own result dict gauges whether that metric's
# denominator is big enough to trust (see MIN_ROWS above).
_DENOMINATOR_METRIC: dict[str, str] = {
    "places_rows": "places_rows",
    "brand_non_null_rate": "places_rows",
    "confidence_non_null_rate": "places_rows",
    "category_non_null_rate": "places_rows",
    "addresses_rows": "addresses_rows",
}

# Human-readable label per metric, for the report table.
METRIC_LABELS: dict[str, str] = {
    "places_rows": "places rows",
    "brand_non_null_rate": "brand non-null rate",
    "confidence_non_null_rate": "confidence non-null rate",
    # taxonomy.primary is the column find_places actually reads for
    # "category" (overture._place_select_exprs) — named for both so a
    # reader who only knows the API field still recognizes it.
    "category_non_null_rate": "category (taxonomy.primary) non-null rate",
    "addresses_rows": "addresses rows",
}


def probe_bbox_metrics(
    con: duckdb.DuckDBPyConnection, release_name: str, lat: float, lon: float
) -> dict[str, float] | None:
    """Coverage metrics for one probe bbox at one release, or None on failure.

    Network-dependent (reads live/pinned S3 parquet through the runtime's own
    glob resolution) — never exercised from pytest; compare_bbox_metrics()
    below is where the offline tests live.
    """
    xmin, ymin, xmax, ymax = geo.bbox_around(lat, lon, PROBE_RADIUS_M)
    filter_sql, params = geo.bbox_filter_sql(xmin, ymin, xmax, ymax)
    # Globs built directly from DEFAULT_UPSTREAM_BASE, exactly like the
    # column probe above: the canary's job is to look at the *live* bucket,
    # so a runner's PLACEROOT_DATA_PATH* pin or a registered override must
    # neither redirect these probes nor raise UpstreamUnavailable out of
    # them (which would abort the run and discard findings already
    # collected by the other checks).
    places_glob = (
        f"{overture.DEFAULT_UPSTREAM_BASE}/{release_name}/theme=places/type=place/*"
    )
    addresses_glob = (
        f"{overture.DEFAULT_UPSTREAM_BASE}/{release_name}/theme=addresses/type=address/*"
    )
    try:
        n, brand_n, confidence_n, category_n = con.execute(
            f"""
            SELECT
                count(*) AS n,
                count(brand.names.primary) AS brand_n,
                count(confidence) AS confidence_n,
                count(taxonomy.primary) AS category_n
            FROM read_parquet('{places_glob}', hive_partitioning=1)
            WHERE {filter_sql}
            """,
            params,
        ).fetchone()
        (addresses_n,) = con.execute(
            f"""
            SELECT count(*) AS n
            FROM read_parquet('{addresses_glob}', hive_partitioning=1)
            WHERE {filter_sql}
            """,
            params,
        ).fetchone()
    except duckdb.Error as e:
        print(f"coverage probe failed for {release_name} @ ({lat}, {lon}): {e}", file=sys.stderr)
        return None
    return {
        "places_rows": float(n),
        "brand_non_null_rate": (brand_n / n) if n else 0.0,
        "confidence_non_null_rate": (confidence_n / n) if n else 0.0,
        "category_non_null_rate": (category_n / n) if n else 0.0,
        "addresses_rows": float(addresses_n),
    }


def compare_bbox_metrics(
    pinned_by_bbox: dict[str, dict[str, float]],
    newest_by_bbox: dict[str, dict[str, float]],
) -> list[dict]:
    """Pure comparison: pinned vs newest metrics -> one row per (bbox, metric).

    Each row is {"bbox", "metric", "pinned", "newest", "delta_pct",
    "regression", "skip_reason"}. delta_pct is (newest - pinned) / pinned *
    100, None when skipped. A metric is skipped (skip_reason set,
    regression False, delta_pct None) when its denominator (see
    _DENOMINATOR_METRIC) is below MIN_ROWS on the pinned side, or when the
    pinned value is exactly 0 (nothing to take a percentage of). Only bboxes
    present in both dicts are compared — a bbox missing from one side (a
    failed probe) is silently skipped by the caller, not reported as a
    regression.
    """
    rows: list[dict] = []
    for bbox, pinned in pinned_by_bbox.items():
        newest = newest_by_bbox.get(bbox)
        if newest is None:
            continue
        for metric in COVERAGE_METRICS:
            pinned_value = pinned.get(metric)
            newest_value = newest.get(metric)
            if pinned_value is None or newest_value is None:
                continue
            denom = pinned.get(_DENOMINATOR_METRIC[metric])
            row = {
                "bbox": bbox,
                "metric": metric,
                "pinned": pinned_value,
                "newest": newest_value,
                "delta_pct": None,
                "regression": False,
                "skip_reason": None,
            }
            if denom is not None and denom < MIN_ROWS:
                row["skip_reason"] = f"pinned denominator {denom:g} < MIN_ROWS ({MIN_ROWS})"
                rows.append(row)
                continue
            if metric.endswith("_non_null_rate") and denom is not None:
                # For the rate metrics the noisy quantity is the *numerator*
                # (the pinned non-null count), not the row count: 800 places
                # rows with 6 branded ones still make a -33% "drop" out of
                # two rows moving. Floor it with the same MIN_ROWS.
                non_null = pinned_value * denom
                if non_null < MIN_ROWS:
                    row["skip_reason"] = (
                        f"pinned non-null count {non_null:g} < MIN_ROWS ({MIN_ROWS})"
                    )
                    rows.append(row)
                    continue
            if pinned_value == 0:
                row["skip_reason"] = "pinned value is 0"
                rows.append(row)
                continue
            delta_pct = (newest_value - pinned_value) / pinned_value * 100.0
            row["delta_pct"] = delta_pct
            row["regression"] = delta_pct <= -REGRESSION_TOLERANCE * 100.0
            rows.append(row)
    return rows


def render_coverage_report(
    rows: list[dict], pinned_release: str, newest_release: str
) -> list[str]:
    """Markdown finding lines for the flagged (regression=True) rows in
    `rows`, or [] if none. Pure — takes compare_bbox_metrics()'s output, not
    a connection, so it's fully covered by offline tests with synthetic
    numbers.
    """
    flagged = [r for r in rows if r["regression"]]
    if not flagged:
        return []
    lines = [
        f"- **Field-coverage regression** in `{newest_release}` vs pinned "
        f"`{pinned_release}`: at least one probe bbox lost more than "
        f"{REGRESSION_TOLERANCE:.0%} of a metric with no schema change "
        f"(the shape OvertureMaps/data#546 documented — columns present, "
        f"values collapsed). Do not bump the pin to this release until "
        f"this is understood.\n"
        f"\n"
        f"  | bbox | metric | pinned | newest | delta % |\n"
        f"  |---|---|---|---|---|"
    ]
    for r in flagged:
        lines.append(
            f"  | {r['bbox']} | {METRIC_LABELS[r['metric']]} | {r['pinned']:g} | "
            f"{r['newest']:g} | {r['delta_pct']:+.1f}% |"
        )
    return lines


def main() -> int:
    newest = release._discover(timeout_s=30.0)
    findings: list[str] = []

    if newest is None:
        findings.append(
            "- **Release discovery failed** — the canary could not list "
            "the public bucket. If this repeats, every deployment whose "
            "egress fails the same way is running on the pinned fallback."
        )
    elif newest != release.PINNED_RELEASE:
        findings.append(
            f"- **Pinned fallback is stale**: `PINNED_RELEASE = "
            f"\"{release.PINNED_RELEASE}\"` but upstream's newest release is "
            f"`{newest}`. Bump it with `uv run python scripts/bump_pin.py "
            f"{newest}` — see docs/PIN.md for the full runbook (what else "
            f"moves, and what deliberately doesn't)."
        )
        # #269: the pin is not just a fallback string any more — three
        # bundled artifact sets are keyed by it, and each one simply misses
        # on any other release. Until they are regenerated and shipped, a
        # deployment that rolls forward loses every acceleration this
        # package has (cold queries go from seconds to tens of seconds), so
        # the bump is a multi-step chore and the report has to say so.
        findings.append(
            "  Regenerate **all three** bundled artifact sets for the new "
            "release and commit them in the same PR, or the release rollover "
            "silently costs every user the cold-start work:\n"
            "    - `uv run python scripts/build_release_manifest.py`\n"
            "    - `uv run python scripts/build_geocode_index.py`\n"
            "    - `uv run python scripts/build_land_cover_grid.py`\n"
            "  Until then `data_version` reports `artifacts: unmatched`, and "
            "deployments stay on the artifact release until it goes stale "
            "(PLACEROOT_STALE_RELEASE_DAYS)."
        )

    target = newest or release.PINNED_RELEASE
    # The runtime's own connection setup (db._configure): anonymous credentials
    # for the public bucket, the S3 region, and the httpfs timeout/retry
    # settings. Hand-rolling those here is how a canary starts failing for
    # reasons the server never would — a signed request 403ing on a runner
    # that happens to carry AWS_* in its environment, say.
    con = db.new_connection()
    try:
        for theme, type_, required in THEME_REQUIREMENTS:
            glob = f"{overture.DEFAULT_UPSTREAM_BASE}/{target}/theme={theme}/type={type_}/*"
            cols = probe_columns(con, glob)
            if cols is None:
                findings.append(
                    f"- **Could not probe** `theme={theme}/type={type_}` at "
                    f"`{target}` — dataset missing or unreadable."
                )
                continue
            missing = [c for c in required if c not in cols]
            if missing:
                findings.append(
                    f"- **Schema drift** in `theme={theme}/type={type_}` at "
                    f"`{target}`: required column(s) {missing} are gone. Runtime "
                    f"degrades these to None fields — fix before the TTL rollover "
                    f"adopts this release."
                )

        coverage_note = None
        if newest is None:
            coverage_note = "release discovery failed"
        elif newest == release.PINNED_RELEASE:
            coverage_note = "pinned release is already newest"
        else:
            pinned_by_bbox: dict[str, dict[str, float]] = {}
            newest_by_bbox: dict[str, dict[str, float]] = {}
            for name, lat, lon in PROBE_METROS:
                pinned_metrics = probe_bbox_metrics(con, release.PINNED_RELEASE, lat, lon)
                newest_metrics = probe_bbox_metrics(con, newest, lat, lon)
                if pinned_metrics is None or newest_metrics is None:
                    findings.append(
                        f"- **Could not run coverage probe** for `{name}` "
                        f"({'pinned' if pinned_metrics is None else 'newest'} "
                        f"release unreadable) — skipped, not counted as clean."
                    )
                    continue
                pinned_by_bbox[name] = pinned_metrics
                newest_by_bbox[name] = newest_metrics
            comparison = compare_bbox_metrics(pinned_by_bbox, newest_by_bbox)
            findings.extend(render_coverage_report(comparison, release.PINNED_RELEASE, newest))
    finally:
        con.close()

    if findings:
        print(f"## Overture canary findings ({target})\n")
        print("\n".join(findings))
        return 1
    suffix = f" (coverage gate skipped: {coverage_note})" if coverage_note else ""
    print(
        f"canary clean: pin {release.PINNED_RELEASE} is newest ({target}); "
        f"all schemas hold{suffix}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
