"""GERS id-diff across two Overture releases, bbox-bounded (issue #376).

The query layer for #309's "what changed here between releases" question:
scan a small bbox in release A and release B, join the two scans on GERS
`id`, and classify every id as appeared (only in B), disappeared (only in
A), or changed (in both, but its primary name or category differs).

This module deliberately has no MCP tool wired to it yet (that's #377) — it
is a pure query-layer function so the RAM-discipline requirements (bounded
bbox, bounded scans, bounded output) can be reviewed and tested in
isolation from any tool-surface concerns.

RAM discipline (this machine is memory-limited, issue #376's hard
requirement): every knob here exists to keep a single diff_places() call
from materializing more than a few thousand rows total.
  - MAX_BBOX_SPAN_DEG caps the queried area itself, before any SQL runs.
  - SCAN_LIMIT caps how many rows either release's SELECT can return.
  - `limit` caps how many appeared/disappeared/changed rows the *result*
    carries, independent of how many were scanned.
Exact counts, however, are never capped by `limit` — only the lists are.
"""

import duckdb

from placeroot import db, geo, overture
from placeroot.errors import SchemaDegraded, UpstreamUnavailable

# A raw bbox has no radius to run through geo.clamp_radius_m the way every
# other query in this codebase does (find_places, buildings, routing all
# start from a point+radius and only ever build a bbox as an internal
# prefilter). diff_places takes the bbox directly from the caller, so it
# needs its own area guard instead of inheriting one.
#
# 0.5 degrees a side is roughly a large metro area at mid-latitudes (~55 km
# east-west at 40 deg N, ~55 km north-south everywhere) — big enough to
# cover "what changed in Paris" but nowhere near a country or a continent.
# Two such boxes, each holding up to SCAN_LIMIT rows with a handful of
# scalar columns, stay comfortably under a few hundred KB of Python objects
# even before either list gets capped to `limit`. There is no existing
# area/bbox guard elsewhere in the codebase to reuse (geo.MAX_QUERY_RADIUS_M
# bounds a *radius*, not a raw caller-supplied box), so this is a new,
# narrowly-scoped cap rather than a shared one.
MAX_BBOX_SPAN_DEG = 0.5

# Per-release, per-query cap on how many place rows a single scan can
# return. A few thousand keeps one side of the diff (id, name, category,
# confidence, lon, lat -- six scalars, never a full row) to low tens of MB
# even in the pathological case of a dense city center packed with named
# points, while still being generous enough that a metro-scale bbox's real
# place count (typically hundreds to low thousands per Overture's places
# density) fits inside it without silently truncating the comparison.
SCAN_LIMIT = 3000

# Columns diff_places depends on. id and bbox are essential -- without id
# there is nothing to diff on, and without bbox there is no lon/lat to
# report or bbox column to filter by. names/taxonomy/confidence degrade to
# NULL in the row (matching overture.py's REQUIRED_COLUMNS/ESSENTIAL_COLUMNS
# convention) rather than failing the whole scan, since a fixture or a
# schema-drifted release missing just the display name is still diffable by
# id and category.
_REQUIRED_COLUMNS = ["id", "bbox", "names", "taxonomy", "confidence", "basic_category"]
_ESSENTIAL_COLUMNS = {"id", "bbox"}


def _validate_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax), validated sane and metro-scale-or-smaller.

    Raises ValueError for a malformed tuple, min >= max on either axis, an
    out-of-range coordinate, or a box wider/taller than MAX_BBOX_SPAN_DEG.
    Antimeridian-crossing boxes (xmin > xmax by design, as geo.bbox_around
    can produce) are out of scope here: diff_places takes a caller-supplied
    box, not one computed from a point+radius, so there is no legitimate
    caller-facing reason for it to cross the seam, and rejecting min >= max
    outright keeps the validation simple and the error message unambiguous.
    """
    try:
        xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    except (TypeError, ValueError):
        raise ValueError(f"bbox must be a 4-tuple of numbers, got {bbox!r}") from None
    if not (xmin < xmax and ymin < ymax):
        raise ValueError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat) with min < max on both axes: "
            f"{bbox!r}"
        )
    if not (-180.0 <= xmin <= 180.0 and -180.0 <= xmax <= 180.0):
        raise ValueError(f"bbox longitude out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= ymin <= 90.0 and -90.0 <= ymax <= 90.0):
        raise ValueError(f"bbox latitude out of [-90, 90]: {bbox!r}")
    if (xmax - xmin) > MAX_BBOX_SPAN_DEG or (ymax - ymin) > MAX_BBOX_SPAN_DEG:
        raise ValueError(
            f"bbox too large for diff_places (max {MAX_BBOX_SPAN_DEG} degrees per side, "
            f"got {xmax - xmin:.4f} x {ymax - ymin:.4f}): {bbox!r}. Split into smaller "
            "boxes and diff each separately -- this is a hard RAM-discipline limit, not "
            "a tunable default."
        )
    return xmin, ymin, xmax, ymax


def _select_exprs(missing: set[str]) -> dict[str, str]:
    """SQL expressions for a scan row, degrading to NULL for a missing
    non-essential column -- same idiom as overture._place_select_exprs,
    trimmed to just the columns diff_places needs."""
    return {
        "name": "NULL" if "names" in missing else "names.primary",
        "category": "NULL" if "taxonomy" in missing else "taxonomy.primary",
        "confidence": "NULL" if "confidence" in missing else "confidence",
    }


def _category_filter(missing: set[str], category: str, params: dict) -> str:
    """Exact-match category clause (with a leading "AND "), or "" if
    neither category column is present.

    Exact equality, not the substring ILIKE find_places uses for its
    free-text category box: a diff is comparing category *values* between
    releases, so "coffee" silently also matching "coffee_shop_alternate"
    or every row whose category merely contains the substring would make
    the comparison mean something different depending on which release
    happened to phrase a category that way. lower() on both sides matches
    overture._place_categories_or_filter's own exact-match case-insensitivity.
    """
    clauses = []
    if "basic_category" not in missing:
        clauses.append("lower(basic_category) = lower($diff_category)")
    if "taxonomy" not in missing:
        clauses.append("lower(taxonomy.primary) = lower($diff_category)")
    if not clauses:
        return ""
    params["diff_category"] = category
    return f" AND ({' OR '.join(clauses)})"


def _scan_release(
    glob: str,
    bbox_filter: str,
    bbox_params: dict,
    category: str | None,
) -> tuple[list[dict], bool]:
    """Bounded (rows, hit_scan_limit) for one release's places within bbox.

    Rows carry only id/name/category/confidence/lat/lon -- never a full
    place row -- and the SELECT itself carries SCAN_LIMIT, so the cap is
    enforced in SQL rather than by slicing a larger Python list afterwards.
    The scan is ORDER BY id: when the cap bites, both releases keep the
    SAME deterministic id-prefix of the bbox, so the diff still compares
    like with like. Ordering by a ranking column (confidence) here would
    make a capped scan an arbitrary, release-dependent sample -- the two
    sides would then diff two different populations and report thousands
    of phantom appeared/disappeared rows; ranking for presentation happens
    in Python after the diff instead. Rows with a NULL id are excluded in
    SQL -- they cannot be diffed and would otherwise collapse into a single
    dict key and fabricate changed/unchanged entries.
    hit_scan_limit is True when the scan returned exactly SCAN_LIMIT rows,
    meaning the bbox may hold more matching places than this side actually
    saw -- that's surfaced to the caller via the result's "truncated" flag
    rather than silently comparing a partial view of one side as if it were
    complete.

    Raises SchemaDegraded if id or bbox is missing from glob's schema, or
    UpstreamUnavailable if the scan itself fails (network, missing file,
    corrupt parquet -- anything DuckDB reports as duckdb.Error).
    """
    missing = set(overture.missing_columns(glob, _REQUIRED_COLUMNS))
    essential_missing = [c for c in missing if c in _ESSENTIAL_COLUMNS]
    if essential_missing:
        raise SchemaDegraded(essential_missing)

    exprs = _select_exprs(missing)
    params = dict(bbox_params)
    where = bbox_filter
    if category:
        where += _category_filter(missing, category, params)

    sql = f"""
        SELECT
            id,
            {exprs["name"]}       AS name,
            {exprs["category"]}   AS category,
            {exprs["confidence"]} AS confidence,
            round(bbox.ymin, 6)  AS lat,
            round(bbox.xmin, 6)  AS lon
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE {where} AND id IS NOT NULL
        ORDER BY id
        LIMIT {SCAN_LIMIT}
    """
    try:
        with db.conn_lock:
            rows = db.shared_conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    cols = ["id", "name", "category", "confidence", "lat", "lon"]
    results = [dict(zip(cols, r)) for r in rows]
    return results, len(results) >= SCAN_LIMIT


def _sort_key(row: dict) -> tuple[float, str]:
    """Rank by confidence desc, then name -- rows with no confidence/name
    (a degraded dataset) sort last within their tier rather than crashing
    on a None comparison. Changed rows carry old_name/new_name instead of
    "name", so the tie-break falls back to new_name, then old_name."""
    confidence = row.get("confidence")
    name = row.get("name") or row.get("new_name") or row.get("old_name")
    return (-(confidence if confidence is not None else -1.0), name or "")


def diff_places(
    bbox: tuple[float, float, float, float],
    release_a: str,
    release_b: str,
    category: str | None = None,
    limit: int = 50,
) -> dict:
    """GERS id diff of places in `bbox` between release_a and release_b.

    bbox is (min_lon, min_lat, max_lon, max_lat), matching every other
    bbox-shaped argument in this codebase (find_places_in_bbox, cache's
    tile enumeration). Rejected (ValueError) if malformed, out of range, or
    larger than MAX_BBOX_SPAN_DEG per side -- see _validate_bbox.

    release_a/release_b are Overture release strings (YYYY-MM-DD.N);
    overture.upstream_glob raises ValueError for anything that doesn't
    match that shape, which is relied on here rather than re-validated.
    They must differ -- diffing a release against itself is always the
    empty diff and almost certainly a caller bug, so it's rejected instead
    of silently returning all-unchanged.

    category, when given, filters BOTH scans identically (exact match on
    basic_category/taxonomy.primary, case-insensitive -- see
    _category_filter) before the diff runs, so e.g. category="restaurant"
    only ever compares restaurants against restaurants; a place that
    changed OUT of the category between releases will look like it
    "disappeared" from this filtered view, which is the correct reading of
    "restaurants that changed" rather than a bug.

    Returns:
        {
            "appeared":     [{"id","name","category","confidence","lat","lon"}, ...],
            "disappeared":  [...same shape, from release_a...],
            "changed":      [{"id","old_name","new_name","old_category",
                               "new_category","confidence","lat","lon"}, ...],
            "counts": {"appeared": n, "disappeared": n, "changed": n, "unchanged": n},
            "truncated": bool,
            "releases": {"from": release_a, "to": release_b},
        }

    Every list is capped at `limit` (ranked by confidence desc, then name);
    "counts" is never reduced by `limit` -- it counts everything the scans
    saw even when a list is capped. When neither scan hit SCAN_LIMIT the
    counts are exact for the bbox; when a scan did hit it, the counts cover
    only the deterministic id-prefix window both sides scanned (ORDER BY
    id, see _scan_release) -- a like-for-like partial comparison, flagged
    via "truncated", not an exact census of the bbox. "truncated" is True
    if any list was capped by `limit` OR either release's scan itself hit
    SCAN_LIMIT (meaning the bbox held more candidates than that side could
    see at all -- the diff is then a comparison of partial views, not a
    complete one).

    Raises SchemaDegraded if either release's dataset is missing id/bbox,
    or UpstreamUnavailable if either scan fails.
    """
    if release_a == release_b:
        raise ValueError(f"release_a and release_b must differ, both are {release_a!r}")
    limit = max(0, int(limit))
    xmin, ymin, xmax, ymax = _validate_bbox(bbox)

    # Both releases query the identical bbox, so one bbox_filter/params pair
    # (and, when given, one category param) is shared by both scans --
    # there is no per-release bbox to keep separate.
    bbox_filter, bbox_params = geo.bbox_filter_sql(xmin, ymin, xmax, ymax)

    # upstream_glob validates the release strings (ValueError on anything
    # that isn't YYYY-MM-DD.N) as a side effect of resolving them -- this
    # runs before either scan so a typo'd release fails fast, not after
    # release_a's scan has already paid for a remote read.
    glob_a = overture.upstream_glob(overture.THEME, "place", release=release_a)
    glob_b = overture.upstream_glob(overture.THEME, "place", release=release_b)

    rows_a, capped_a = _scan_release(glob_a, bbox_filter, dict(bbox_params), category)
    rows_b, capped_b = _scan_release(glob_b, bbox_filter, dict(bbox_params), category)

    by_a = {r["id"]: r for r in rows_a}
    by_b = {r["id"]: r for r in rows_b}

    disappeared_full = [a for gers_id, a in by_a.items() if gers_id not in by_b]
    appeared_full = [b for gers_id, b in by_b.items() if gers_id not in by_a]

    changed_full = []
    unchanged = 0
    for gers_id, b in by_b.items():
        a = by_a.get(gers_id)
        if a is None:
            continue  # already counted in appeared_full
        if a["name"] != b["name"] or a["category"] != b["category"]:
            changed_full.append(
                {
                    "id": gers_id,
                    "old_name": a["name"],
                    "new_name": b["name"],
                    "old_category": a["category"],
                    "new_category": b["category"],
                    "confidence": b["confidence"],
                    "lat": b["lat"],
                    "lon": b["lon"],
                }
            )
        else:
            unchanged += 1

    appeared_full.sort(key=_sort_key)
    disappeared_full.sort(key=_sort_key)
    changed_full.sort(key=_sort_key)

    counts = {
        "appeared": len(appeared_full),
        "disappeared": len(disappeared_full),
        "changed": len(changed_full),
        "unchanged": unchanged,
    }
    truncated = (
        capped_a
        or capped_b
        or len(appeared_full) > limit
        or len(disappeared_full) > limit
        or len(changed_full) > limit
    )
    return {
        "appeared": appeared_full[:limit],
        "disappeared": disappeared_full[:limit],
        "changed": changed_full[:limit],
        "counts": counts,
        "truncated": truncated,
        "releases": {"from": release_a, "to": release_b},
    }
