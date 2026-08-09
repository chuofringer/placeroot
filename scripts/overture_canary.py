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

Exit code 0 when both hold; 1 with a markdown report on stdout otherwise —
the workflow turns that report into a GitHub issue. Network-dependent by
design; never run from pytest.

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
            f"`{newest}`. Bump the pin (src/placeroot/release.py), regenerate "
            f"fixtures, and sweep docs for the old string."
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
    finally:
        con.close()

    if findings:
        print(f"## Overture canary findings ({target})\n")
        print("\n".join(findings))
        return 1
    print(f"canary clean: pin {release.PINNED_RELEASE} is newest ({target}); all schemas hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
