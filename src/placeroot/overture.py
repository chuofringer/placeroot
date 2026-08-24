"""DuckDB query layer over Overture Maps GeoParquet on S3.

Queries the public Overture bucket directly — no ETL, no database, no API key.
bbox column pushdown keeps remote scans to a handful of row groups. A local
tile cache (see cache.py) sits in front of repeat remote scans for the same
area, and the active release (see release.py) is discovered at runtime
instead of hardcoded.

Concurrency (issue #24): DuckDB connections aren't safe for concurrent
execute() calls from multiple threads, which HTTP transport mode makes
possible (stdio only ever has one request in flight). db.conn_lock
serializes every use of the shared connection — every call site in this
file (and in routing.py, divisions.py, buildings.py, and geocode.py, which
all now reuse the same connection via db.py — issue #40) acquires it
first. Background tile materialization (cache.py) is the only exception:
it always runs on its own connection via db.new_connection().

Connection setup, schema probing, and geometry helpers live in db.py and
geo.py; _conn/_conn_lock/_probe_schema are thin aliases kept for this
module's call sites and the tests — import db directly in new code.
"""

import hashlib
import logging
import math
import os
import struct
import threading
import unicodedata

import duckdb

from placeroot import budget, cache, categories, db, geo, honesty, recreation, release, trace
from placeroot.errors import (  # noqa: F401 - re-exported; see below
    SchemaDegraded,
    UpstreamUnavailable,
)
from placeroot.geo import bbox_around as _bbox_around  # noqa: F401 - back-compat alias, see below
from placeroot.geo import bbox_filter_sql as _bbox_filter_sql  # noqa: F401 - back-compat alias

logger = logging.getLogger(__name__)

# Every tool answer stays within this many rows so responses remain
# small enough for an agent's context window.
MAX_ROWS = 25

THEME = "places"

# Columns every tool depends on somewhere. bbox is the only one treated as
# essential (it's what the radius/distance math runs on) — the rest degrade
# gracefully: a tool still answers, just without that field, and callers can
# check degraded_fields() to note it. id (GERS, issue #25) and the place_details
# fields (addresses/websites/phones/socials/brand/sources, issue #9) degrade
# the same way: missing means that field comes back None/empty, not a failure.
REQUIRED_COLUMNS = [
    "names", "taxonomy", "basic_category", "operating_status", "confidence", "bbox",
    "id", "addresses", "websites", "phones", "socials", "brand", "sources",
]
ESSENTIAL_COLUMNS = {"bbox"}

# Overrides a theme's dataset location — set by tests to point at a
# committed fixture instead of the live S3 release. Takes precedence over
# the PLACEROOT_DATA_PATH[_<THEME>] env var, which in turn overrides the
# live release. Keyed by theme so divisions (#11) and geocode's own
# divisions/addresses queries (#10) can each be pointed at their own
# fixture independently of the places fixture.
#
# A single Overture theme can carry more than one `type` (divisions has
# both type=division, which geocode.py reads, and type=division_area,
# which divisions.py reads) — a bare theme key can't distinguish those, so
# an override may also be keyed on the more specific "theme:type_" string.
# _upstream_glob checks the specific key first and falls back to the bare
# theme key, so every existing call site that overrides by theme alone
# (divisions.py's admin_lookup tests included) keeps working unchanged.
_data_path_overrides: dict[str, str] = {}


def _override_key(theme: str, type_: str | None, release: str | None = None) -> str:
    # release, when given, makes the key even more specific than theme:type_
    # (e.g. "places:place@2026-07-22.0") — see set_data_path()'s docstring.
    # release without type_ isn't a supported combination (an explicit
    # release is only ever asked for alongside a theme+type in
    # _upstream_glob), so it isn't given its own key shape here.
    key = theme if type_ is None else f"{theme}:{type_}"
    return key if release is None else f"{key}@{release}"


def set_data_path(
    path: str | None, theme: str = THEME, type_: str | None = None, release: str | None = None
) -> None:
    """Point the query layer at a local dataset instead of live S3.

    Pass None to restore the default (env var, then discovered release) for
    that theme. Intended for tests. theme defaults to "places" for
    back-compat with existing callers that only ever queried places.

    type_ defaults to None, which overrides every type under that theme
    (what every existing caller — admin_lookup's tests included — wants:
    "divisions" only ever has one type in play for them). Pass type_
    explicitly only when a theme has more than one type active at once, as
    divisions now does (type=division for geocode.py, type=division_area
    for divisions.py) — that overrides just that type, leaving a bare-theme
    override (or the live default) in place for the others.

    release (#375) narrows the override further, to one explicit release of
    one theme:type_ — e.g. set_data_path(fixture_a, "places", "place",
    release="2026-07-22.0") and set_data_path(fixture_b, "places", "place",
    release="2026-08-19.0") let a test point two "releases" at two local
    parquet fixtures. A release-keyed override is checked only by an
    _upstream_glob() call that itself passes that release — an explicit
    theme:type_ (no release) override does NOT catch those calls, since the
    two fixtures need to stay distinguishable by release; conversely a
    release-keyed override never answers a plain (no-release) query, so
    existing single-release callers are unaffected either way.

    release requires type_: a release-keyed override without a type_ would
    register under a key _upstream_glob() can never build (an explicit
    release is only ever asked for alongside a theme+type), so it would be
    silently ignored — raise instead.
    """
    if release is not None and type_ is None:
        raise ValueError(
            "set_data_path(release=...) requires type_: a release-keyed "
            "override without a type_ can never be looked up"
        )
    key = _override_key(theme, type_, release)
    if path is None:
        _data_path_overrides.pop(key, None)
    else:
        _data_path_overrides[key] = path


def _env_var_for_theme(theme: str) -> str:
    # Back-compat: places kept its original, unsuffixed env var name.
    return "PLACEROOT_DATA_PATH" if theme == THEME else f"PLACEROOT_DATA_PATH_{theme.upper()}"


# The public Overture bucket's release root. Issue #20: PLACEROOT_UPSTREAM_BASE
# swaps this for a mirror (our own bucket, or any other S3-compatible/local
# base) without touching the theme=/type=/release path structure below it, so
# an upstream layout change stays an inconvenience (re-point the env var) —
# never an outage. See scripts/mirror_theme.py (builds the mirror) and
# docs/MIRROR.md (the switchover runbook).
DEFAULT_UPSTREAM_BASE = "s3://overturemaps-us-west-2/release"


def _upstream_base() -> str:
    return os.environ.get("PLACEROOT_UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE).rstrip("/")


# Back-compat (issue #40): routing.py used to read its own, differently
# named env var for the transportation theme instead of following the
# PLACEROOT_DATA_PATH_<THEME> convention every other theme uses. Still
# honored — as a fallback, checked only when the standard name isn't set —
# so any deployment already setting it keeps working unchanged.
_TRANSPORTATION_LEGACY_ENV_VAR = "PLACEROOT_TRANSPORTATION_DATA_PATH"


def _upstream_glob(theme: str = THEME, type_: str = "place", release_: str | None = None) -> str:
    # release_ (#375): an explicit-release query, for callers (changes_in_area,
    # #309) that need a *specific* Overture release rather than "whatever is
    # active right now" — diffing release A against release B needs both
    # gloms live at once, which resolve_release()'s single active-release
    # cache can't express.
    if release_ is not None and not release._RELEASE_RE.fullmatch(release_):
        # release_ becomes a path/glob segment below (…/release_/theme=…),
        # so validate it the same way release.py validates
        # PLACEROOT_OVERTURE_RELEASE — a stray "../" or other junk here would
        # otherwise flow straight into an S3 glob.
        raise ValueError(f"not a release (YYYY-MM-DD.N): {release_!r}")
    if release_ is not None:
        # Only the release-keyed override answers an explicit-release query.
        # A plain theme:type_ (or bare-theme) override exists to point a
        # *single* release-less call site at one fixture, and #375 needs
        # tests to point two different "releases" of the same theme:type_ at
        # two different local fixtures — so a bare override must not capture
        # an explicit-release call, or the two fixtures couldn't be told
        # apart. Falls straight through to the live glob below when no
        # release-keyed override is registered.
        keyed = _override_key(theme, type_, release_)
        if keyed in _data_path_overrides:
            return _data_path_overrides[keyed]
        # A plain override/env pin (dataset_is_pinned) names one single
        # dataset with no release dimension of its own, so it cannot answer
        # "give me release X" for an arbitrary X — but it must not be
        # silently bypassed either: on a pinned/air-gapped install that
        # would send an explicit-release query straight to live S3, exactly
        # what the pin exists to prevent. So: when the requested release IS
        # the one the deployment resolves to, the pinned dataset is that
        # release's data — serve it; for any other release, raise rather
        # than silently going upstream.
        if dataset_is_pinned(theme, type_):
            if release_ == release.resolve_release():
                return _upstream_glob(theme, type_)
            raise UpstreamUnavailable(
                f"no dataset for release {release_} in this deployment: "
                f"{theme}/{type_} is pinned to a local dataset "
                f"(PLACEROOT_DATA_PATH or an override) that serves release "
                f"{release.resolve_release()} only"
            )
        return f"{_upstream_base()}/{release_}/theme={theme}/type={type_}/*"
    specific_key = _override_key(theme, type_)
    if specific_key in _data_path_overrides:
        return _data_path_overrides[specific_key]
    if theme in _data_path_overrides:
        return _data_path_overrides[theme]
    env_path = os.environ.get(_env_var_for_theme(theme))
    if not env_path and theme == "transportation":
        env_path = os.environ.get(_TRANSPORTATION_LEGACY_ENV_VAR)
    if env_path:
        return env_path
    active_release = release.resolve_release()
    return f"{_upstream_base()}/{active_release}/theme={theme}/type={type_}/*"


def dataset_is_pinned(theme: str = THEME, type_: str = "place") -> bool:
    """Whether theme/type_ resolves to a caller-supplied dataset rather than
    the discovered live release — a test fixture override, or a
    PLACEROOT_DATA_PATH[_<THEME>] pointing at a local extract or a mirror.

    recreation.py uses this to keep its layer from reaching past a
    deployment's own configuration: an operator who pinned places at a
    local file did not thereby agree to a live S3 scan of theme=base.
    """
    if _override_key(theme, type_) in _data_path_overrides or theme in _data_path_overrides:
        return True
    if os.environ.get(_env_var_for_theme(theme)):
        return True
    return bool(theme == "transportation" and os.environ.get(_TRANSPORTATION_LEGACY_ENV_VAR))


def conn() -> duckdb.DuckDBPyConnection:
    """The shared DuckDB connection, for other modules (e.g. geocode.py) that
    query themes beyond places but want the same httpfs setup and warm cache.
    Callers must hold _conn_lock around any query they run against it."""
    return db.shared_conn()


def probe_schema(glob: str) -> frozenset | None:
    """Public wrapper over the schema probe, for callers outside overture.py."""
    return db.probe_schema(glob)


def upstream_glob(theme: str = THEME, type_: str = "place", release: str | None = None) -> str:
    """Public wrapper: the resolved glob/path for theme (fixture override, env, or live S3).

    type_ has no per-theme default beyond "place" — callers querying a
    theme other than places (geocode.py's divisions/addresses queries,
    divisions.py's division_area queries) must pass their own type_
    explicitly, the same way divisions.py already does.

    release (#375) asks for one specific Overture release instead of
    "whatever's active" — see _upstream_glob()'s docstring/comments for the
    override-precedence and env-bypass rules. None (the default) is
    byte-identical to the pre-#375 behavior.
    """
    return _upstream_glob(theme, type_, release_=release)


# Deprecated: import db directly instead. Kept as thin aliases so existing
# external references (tests included) keep working unchanged — see this
# module's docstring and db.py's.
_conn_lock = db.conn_lock
_conn = db.shared_conn
_new_connection = db.new_connection
_probe_schema = db.probe_schema
_configure = db._configure



# Themes the startup pre-warm walks, in the order a session is likely to
# need them. places first (the headline tools), then the two heavy themes
# whose cold footer pass is the largest (buildings 512 files measured
# ~25-50s), then the rest of the query surface.
_WARM_THEMES: tuple[tuple[str, str], ...] = (
    ("places", "place"),
    ("buildings", "building"),
    ("transportation", "segment"),
    ("base", "land_use"),
    ("base", "land"),
    ("divisions", "division"),
    ("addresses", "address"),
)


def warm_metadata() -> None:
    """Best-effort: warm the shared connection's parquet metadata cache for
    every queried theme (issue #31), so a first real query doesn't pay the
    cold footer-read cost alone. Meant to run on a background thread at
    startup; failures are logged and swallowed, never raised.

    Runs on a cursor of the shared instance (db.new_connection), so it
    holds no query lock and still warms the one per-instance metadata
    cache every query reads from. Skips pinned themes (a local file needs
    no warm), and skips everything when places itself is pinned — same
    rule as recreation._reaches_past_a_pinned_deployment: an operator who
    pinned places at a local extract (the offline demo, a mirror) did not
    agree to background S3 traffic for themes they never configured.
    """
    places_pinned = dataset_is_pinned(THEME, "place")
    for theme, type_ in _WARM_THEMES:
        if dataset_is_pinned(theme, type_) or places_pinned:
            continue
        upstream = _upstream_glob(theme, type_)
        try:
            db.new_connection().execute(
                f"SELECT * FROM read_parquet('{upstream}', hive_partitioning=1) LIMIT 0"
            )
        except duckdb.Error as e:
            logger.warning("Metadata pre-warm failed for %s (continuing): %s", upstream, e)
            continue


def missing_columns(glob: str, required: list[str] = REQUIRED_COLUMNS) -> list[str]:
    """`required` columns not present in glob's schema. Empty if the probe failed.

    required defaults to the places REQUIRED_COLUMNS; other themes (e.g.
    divisions, see divisions.py) pass their own list.
    """
    present = _probe_schema(glob)
    if present is None:
        return []
    return [c for c in required if c not in present]


def degraded_fields() -> list[str]:
    """Non-essential REQUIRED_COLUMNS missing from the currently active places dataset."""
    return [c for c in missing_columns(_upstream_glob()) if c not in ESSENTIAL_COLUMNS]


def _check_schema(
    glob: str, required: list[str] = REQUIRED_COLUMNS, essential: set[str] = ESSENTIAL_COLUMNS
) -> list[str]:
    """Missing columns for glob, raising SchemaDegraded if any are essential."""
    missing = missing_columns(glob, required)
    essential_missing = [c for c in missing if c in essential]
    if essential_missing:
        raise SchemaDegraded(essential_missing)
    return missing


def _with_recreation(
    source: str, bbox: tuple[float, float, float, float] | None, theme: str = THEME
) -> tuple[str, bool]:
    """`source`, unioned with the recreation layer unless it is switched off.

    Returns (sql, layer_active). layer_active is what tells a caller the
    marker column exists and can be referenced — the layer being *enabled*
    isn't enough, since a base-theme branch whose schema drifted is dropped
    (recreation.union_branches) and would leave the column undefined.

    The layer is a second read of Overture's *base* theme — the OSM-derived
    playground/park/beach polygons that the listings-derived places theme
    under-counts — projected into the places row shape. See recreation.py
    for what it carries and why. It is on by default;
    PLACEROOT_RECREATION_LAYER=0 turns it off, and then this returns
    `source` untouched and a places query is a single scan again.

    A parenthesized `UNION ALL BY NAME` subquery rather than one
    `read_parquet([...])` over both: the two sides are different datasets
    with different schemas, and the places side is read with
    hive_partitioning=1, which DuckDB will not mix into a list with
    anything else ("Hive partition mismatch ... key \"theme\" not found").
    BY NAME fills the columns each branch lacks with NULL, which is the
    truthful value on both sides. The query's bbox predicate does NOT
    prune the base-theme scans on its own: the projection collapses bbox
    to a computed centroid struct that row-group statistics can't match
    (measured cold cost: 14+ minutes of planet-wide base scans), so each
    branch applies the query bbox to its raw bbox columns itself — see
    recreation._projection's prune.

    Only places: no other theme has a recreation layer, and passing one
    through here would union places rows into a divisions query.
    """
    if theme != THEME:
        return source, False
    branches = recreation.union_branches(bbox)
    if not branches:
        return source, False
    # FALSE, not NULL, on the Overture side: the marker is read as a plain
    # boolean by the name filter below, and spelling it out here means no
    # call site has to remember to coalesce it.
    places_branch = f"SELECT *, FALSE AS {recreation.MARKER_COLUMN} FROM {source}"
    return f"({' UNION ALL BY NAME '.join([places_branch, *branches])})", True


def _places_source(
    bbox: tuple[float, float, float, float] | None, theme: str = THEME
) -> tuple[str, bool]:
    """SQL FROM-clause source for a places query, and whether the recreation
    layer is part of it: local cache tiles, or upstream.

    The recreation layer (unless switched off) is appended *after* cache
    resolution: a places tile materializes theme=places rows only, so a
    cached tile is always a faithful copy of that dataset and turning the
    layer off never leaves base-theme rows behind in it. The layer's own
    branches do their own cache resolution against the base theme's tiles,
    which land under land_use.py's existing "base_<type>" cache keys.
    """
    upstream = _upstream_glob(theme)
    try:
        source = cache.source_sql(theme, upstream, bbox)
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    return _with_recreation(source, bbox, theme)


def _name_filter(missing: set[str], has_recreation: bool) -> list[str]:
    """The "named places only" filter, relaxed for recreation-layer rows.

    Overture places rows without a primary name are dropped: an unnamed
    business listing is a row an agent can do nothing with. Recreation-layer
    rows are the opposite case — 77% of base-theme playgrounds carry no name
    because a playground usually isn't named at all, and "there is a
    playground 120 m from you" is exactly the answer that was asked for. So
    those rows survive with name: null rather than being filtered out. No
    name is invented for them (see recreation.py).
    """
    if "names" in missing:
        return []
    if has_recreation:
        return [f"(names.primary IS NOT NULL OR {recreation.MARKER_COLUMN})"]
    return ["names.primary IS NOT NULL"]


def _dedup_rows_sql(
    from_source: str, filters: list[str], has_recreation: bool
) -> tuple[str, str, list[str]]:
    """(with_clause, from_clause, filters) deduplicating the union's two themes.

    A real-world place can exist in both themes at once — Central Park is a
    places listing *and* a base-theme land_use polygon — and returning both
    rows spends two result slots (and two summarize_area counts) on one
    place. So a recreation-layer row is dropped when the same filtered row
    set also holds a places row with the same category within
    recreation.DEDUP_RADIUS_M; the places row wins because it is the richer
    one (confidence, addresses, the listing's own name).

    The dedup is a self-anti-join over the *already filtered* rows, not
    against the raw datasets: the candidate set is materialized once (AS
    MATERIALIZED — DuckDB would otherwise inline the CTE into both
    references and scan the source twice), so the upstream scan cost of the
    query is unchanged and the join runs over the handful of rows that
    survived the bbox/category filters. Callers interpolate the returned
    pieces as f"{with_clause} SELECT ... FROM {from_clause} WHERE
    {' AND '.join(filters)}". When the layer isn't in the union there is
    nothing to dedup and the inputs pass through untouched.

    Category equality (taxonomy.primary) plus proximity is deliberately the
    whole test: name matching would miss the common case this exists for —
    the base polygon is unnamed while the places listing is named — and a
    places playground 100 m from a base playground being two distinct real
    playgrounds is rarer than the two being one place, per the 150 m
    overlap measurement in recreation.py.
    """
    if not has_recreation:
        return "", from_source, filters
    marker = recreation.MARKER_COLUMN
    duplicate_distance = """2 * 6371000 * asin(sqrt(
                pow(sin(radians(p.bbox.ymin - r.bbox.ymin) / 2), 2)
                + cos(radians(r.bbox.ymin)) * cos(radians(p.bbox.ymin))
                * pow(sin(radians(p.bbox.xmin - r.bbox.xmin) / 2), 2)
            ))"""
    with_clause = (
        f"WITH _candidate_rows AS MATERIALIZED "
        f"(SELECT * FROM {from_source} WHERE {' AND '.join(filters)})"
    )
    dedup_filter = f"""NOT (r.{marker} AND EXISTS (
            SELECT 1 FROM _candidate_rows p
            WHERE NOT p.{marker}
              AND p.taxonomy.primary = r.taxonomy.primary
              AND {duplicate_distance} <= {recreation.DEDUP_RADIUS_M}
        ))"""
    return with_clause, "_candidate_rows AS r", [dedup_filter]


# Haversine great-circle distance in meters between (bbox.ymin, bbox.xmin)
# and a named point, using named params $lat and $lon. Public: geocode.py
# reuses this same distance expression for divisions/addresses nearest-point
# queries so all themes agree on what "distance" means.
DISTANCE_EXPR = """2 * 6371000 * asin(sqrt(
                pow(sin(radians(bbox.ymin - $lat) / 2), 2)
                + cos(radians($lat)) * cos(radians(bbox.ymin))
                * pow(sin(radians(bbox.xmin - $lon) / 2), 2)
            ))"""
_DISTANCE_EXPR = DISTANCE_EXPR  # noqa: N816 - kept as an alias for existing in-module uses


def area_geometry(
    lat: float, lon: float, radius_m: float
) -> tuple[str, str, dict, tuple[float, float, float, float], float]:
    """Shared "is this place within radius_m of (lat, lon)" predicate.

    Returns (bbox_filter_sql, distance_filter_sql, params, bbox,
    effective_radius_m) — the bbox filter is a cheap row-group prefilter
    using intersection semantics (so it doesn't drop non-point geometry the
    way full-containment would); the distance filter is the exact circle
    (already antimeridian-safe: it's built from sin/cos, which are periodic,
    so a raw longitude difference across the seam still comes out small) and
    is what actually decides membership. Both find_places and summarize_area
    use this so they agree on what's "in" an area. params is a dict of named
    parameters shared by both filters plus any additional query-specific
    ones the caller adds.

    bbox is the raw (xmin, ymin, xmax, ymax) box from _bbox_around — pass it
    straight to _from_source()/cache lookups, not the bbox_filter's own
    params: near the antimeridian those diverge (see _bbox_filter_sql), and
    it's this raw, possibly out-of-range box that cache.tiles_for_bbox()
    needs to enumerate tiles on both sides of the seam.

    radius_m is clamped to geo.MAX_QUERY_RADIUS_M (an abuse guard against a
    world-spanning bbox — see geo.clamp_radius_m) and the clamped value is
    used for both the bbox and the $radius_m distance parameter so they
    agree. That same clamped value is returned as effective_radius_m so
    callers that report or reason about the radius they searched (rather
    than just building the SQL) use what actually ran, not the caller's
    unclamped input — see issue #131.
    """
    radius_m = geo.clamp_radius_m(radius_m)
    xmin, ymin, xmax, ymax = _bbox_around(lat, lon, radius_m)
    bbox_filter, bbox_params = _bbox_filter_sql(xmin, ymin, xmax, ymax)
    distance_filter = f"{_DISTANCE_EXPR} <= $radius_m"
    params = {**bbox_params, "lat": lat, "lon": lon, "radius_m": radius_m}
    return bbox_filter, distance_filter, params, (xmin, ymin, xmax, ymax), radius_m


# Overture's operating_status is a business-lifecycle field (is this place a
# going concern?), NOT opening hours. Its raw value "open" reads as "open right
# now" to anyone glancing at a results table, which we can't answer — we surface
# no hours data. Relabel the ambiguous values to lifecycle language and pass any
# unrecognised value through unchanged so we never misrepresent the source.
_OPERATING_STATUS_LABELS = {
    "open": "in business",
    "closed": "permanently closed",
    "closed_permanently": "permanently closed",
    "closed_temporarily": "temporarily closed",
}


def _label_operating_status(value):
    if value is None:
        return None
    return _OPERATING_STATUS_LABELS.get(value, value)


def _annotate_place(row: dict) -> dict:
    """Relabel operating_status and attach a compact trust_note (#308)."""
    row["operating_status"] = _label_operating_status(row.get("operating_status"))
    return honesty.attach_trust_note(row)


def _operating_status_reverse_map() -> dict[str, list[str]]:
    """label -> every raw Overture value that relabels to it, e.g.
    "permanently closed" -> ["closed", "closed_permanently"]."""
    reverse: dict[str, list[str]] = {}
    for raw, label in _OPERATING_STATUS_LABELS.items():
        reverse.setdefault(label, []).append(raw)
    return reverse


def accepted_operating_status_values() -> list[str]:
    """Every operating_status value find_places accepts: relabeled + raw.

    Shared by the ValueError message below and the published schema's
    enum (server.py), so both stay derived from the same source of truth.
    """
    return sorted(set(_OPERATING_STATUS_LABELS) | set(_operating_status_reverse_map()))


def _resolve_operating_status(operating_status: str) -> list[str]:
    """Resolve a caller-supplied operating_status (relabeled or raw, case-
    insensitive) to the raw Overture value(s) it should filter on.

    Raises ValueError if the value matches neither a relabeled value nor a
    raw Overture value.
    """
    needle = operating_status.strip().lower()
    reverse = _operating_status_reverse_map()
    for label, raw_values in reverse.items():
        if label.lower() == needle:
            return raw_values
    for raw in _OPERATING_STATUS_LABELS:
        if raw.lower() == needle:
            return [raw]
    accepted = accepted_operating_status_values()
    raise ValueError(
        f"unrecognized operating_status {operating_status!r}; accepted values: "
        f"{', '.join(accepted)}"
    )


def _like_escape(value: str) -> str:
    """Escape LIKE/ILIKE wildcards so a user substring matches literally.

    Backslash is the ESCAPE char (see the ESCAPE '\\' clauses at each call
    site) — escape it first, then the two DuckDB ILIKE metacharacters (%
    matches any run, _ matches any single char). Without this, a caller's
    own % or _ (routine for Overture's snake_case category values, e.g.
    coffee_shop) silently turns into a wildcard and over-matches.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _place_category_name_filters(
    missing: set[str], category: str | None, name: str | None, params: dict
) -> list[str]:
    """category/name filter clauses (and their params, added to params in
    place) shared by find_places and find_places_in_division so both narrow
    results identically."""
    filters = []
    if category:
        cat_clauses = []
        if "basic_category" not in missing:
            cat_clauses.append("basic_category ILIKE $category ESCAPE '\\'")
        if "taxonomy" not in missing:
            cat_clauses.append(
                "(taxonomy.primary ILIKE $category ESCAPE '\\'"
                " OR list_contains(taxonomy.alternates, $category_exact))"
            )
        if cat_clauses:
            filters.append(f"({' OR '.join(cat_clauses)})")
            params["category"] = f"%{_like_escape(category)}%"
            params["category_exact"] = category
    if name:
        if "names" not in missing:
            filters.append("names.primary ILIKE $name ESCAPE '\\'")
            params["name"] = f"%{_like_escape(name)}%"
    return filters


# --- #373: typo/alt-spelling fallback tiers for POI name search -----------
#
# Mirrors geocode.py's #214 (names.common alternates) and #215 (jaro_winkler
# fuzzy) tiers on the divisions theme, scaled to a bboxed radius query
# instead of a locally materialized whole-theme table: places has no local
# index the way divisions does (#43), so both fallback tiers here run
# strictly INSIDE the same bbox/distance/category/attribute pruning the
# tier-1 ILIKE scan already applies — a bounded LIMIT'd scan, never an
# unbounded one. Tier 1 (names.primary ILIKE) always runs first and, when it
# yields any row, these tiers never run at all.
#
# The fold is a simpler, independent copy of geocode.py's — lower() plus
# accent-stripping, no unfolded-letter table (ß/ø/ł/...) — since overture.py
# is imported BY geocode.py and can't import back from it, and Overture
# business names are overwhelmingly Latin-script-plus-diacritics; widening
# this fold to match geocode.py's exonym table has its own regressions to
# measure and is left for a follow-up if POI data turns out to need it.
_POI_FUZZY_SIMILARITY_THRESHOLD = 0.92  # mirrors geocode._FUZZY_SIMILARITY_THRESHOLD (#215)

# Cap on tier 3's candidate pool — the nearest rows that already cleared
# the similarity threshold (#374: the score is applied before this limit,
# so density can't crowd the real match out), within the query's own
# bbox/radius, never a theme-wide scan. Sized like geocode.py's
# CHECKLIST_MAX_CANDIDATES-class caps: generous for a single radius query,
# small enough that carrying it through the query is free.
_POI_FUZZY_POOL_SIZE = 500


def _fold_poi_name(s: str) -> str:
    """Casefold + diacritic-strip a POI name/query for tier-2/3 comparison.

    Must stay in step with _fold_poi_name_sql, which folds the stored
    column the same way — the two only ever meet as an equality/similarity
    comparison.
    """
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c)
    ).lower()


def _fold_poi_name_sql(expr: str) -> str:
    """SQL twin of _fold_poi_name over `expr`."""
    return f"lower(strip_accents({expr}))"


def _find_places_name_fallback(
    from_source: str,
    base_filters: list[str],
    params: dict,
    has_recreation: bool,
    exprs: dict[str, str],
    name: str,
    limit: int,
) -> list[dict]:
    """Tiers 2 (alt-name) and 3 (fuzzy) of the #373 POI name fallback.

    Only called when tier 1's ILIKE substring search came back empty and a
    name filter was given. `base_filters` is every find_places filter
    EXCEPT the name clause (bbox, distance, category, attribute, presence)
    — both tiers stay inside that same pruning, just swap the name
    predicate for a looser one.

    Tries tier 2 (names.common alternate spellings) first; if that finds
    nothing, falls through to tier 3 (fuzzy). Returns [] if neither tier
    finds anything — an honest empty answer, not an error.

    Rows carry "matched_by": "alt_name" | "fuzzy" so a caller (and
    resolve_place, which reads it to relabel its own match field) can tell
    a correction from a literal hit; see find_places' docstring for the
    honesty convention this mirrors from geocode.py's #214/#215.
    """
    select_cols = f"""
        {exprs["id"]} AS id,
        {exprs["name"]} AS name,
        {exprs["category"]} AS category,
        {exprs["basic_category"]} AS basic_category,
        {exprs["operating_status"]} AS operating_status,
        {exprs["confidence"]} AS confidence,
        {exprs["brand"]} AS brand,
        {exprs["has_website"]} AS has_website,
        {exprs["has_phone"]} AS has_phone,
        round(bbox.ymin, 6) AS lat,
        round(bbox.xmin, 6) AS lon,
        round({_DISTANCE_EXPR}, 0) AS distance_m
    """
    cols = [
        "id", "name", "category", "basic_category", "operating_status",
        "confidence", "brand", "has_website", "has_phone", "lat", "lon", "distance_m",
    ]

    # --- tier 2: names.common alternate spellings ---------------------
    #
    # names.common is an optional nested field probe_schema can't see (it
    # only inspects top-level columns), so a dataset without it — or one
    # whose `names` struct simply has no "common" key, like every fixture
    # in this repo — fails the query itself at bind time. Caught and
    # swallowed here, same convention as
    # geocode._materialize_divisions_table's alt-name half: losing this
    # tier is a much smaller loss than raising, so a miss falls straight
    # through to tier 3 rather than failing the call.
    with_clause, from_clause, dedup_filters = _dedup_rows_sql(
        from_source, base_filters, has_recreation
    )
    folded_name = _fold_poi_name(name)
    base_params = {k: v for k, v in params.items() if k != "name"}
    alt_params = dict(base_params, _folded_name=folded_name)
    # list_distinct + the QUALIFY row_number guard (#374): names.common is a
    # per-language map, and the same alternate spelling routinely repeats
    # across locale keys ({'en': 'Munich', 'es': 'Munich', ...}) — a plain
    # UNNEST(map_values(...)) yields one row per locale, so a single place
    # came back as duplicate result rows. list_distinct collapses repeats of
    # the same string; row_number over id collapses the remaining case of
    # two *different* alternates both folding equal to the query.
    alt_sql = f"""
        {with_clause}
        SELECT {select_cols}, _alt_spelling AS matched_alt
        FROM {from_clause}, UNNEST(list_distinct(map_values(names.common))) AS _u(_alt_spelling)
        WHERE {' AND '.join(dedup_filters)}
          AND {_fold_poi_name_sql("_alt_spelling")} = $_folded_name
        QUALIFY row_number() OVER (PARTITION BY {exprs["id"]} ORDER BY _alt_spelling) = 1
        ORDER BY distance_m
        LIMIT {limit}
    """
    try:
        with trace.scan("places alt-name scan", bounded=True, source=from_clause), _conn_lock:
            rows = _conn().execute(alt_sql, alt_params).fetchall()
    except duckdb.Error:
        rows = []
    if rows:
        results = [dict(zip([*cols, "matched_alt"], r)) for r in rows]
        for d in results:
            d["matched_by"] = "alt_name"
            d.pop("matched_alt")
        return results

    # --- tier 3: fuzzy (jaro_winkler) on names.primary -----------------
    #
    # Score first, THEN limit (#374): the similarity predicate is applied
    # INSIDE the pool CTE, so the pool is "the nearest _POI_FUZZY_POOL_SIZE
    # rows that actually clear the threshold" — not "the nearest 500 rows,
    # hoping the real match is among them". A pure distance-first pool died
    # in dense areas: 500 Manhattan storefronts filled it before "Empire
    # State Building" was ever scored. Still bounded by construction — the
    # same bbox/distance/category/attribute filters prune what gets scored,
    # and the CTE's LIMIT caps what leaves it.
    #
    # The score itself is substring-tolerant (#374): tier 1 is a substring
    # match, so a query for one word of a longer stored name ("startbucks"
    # vs "Starbucks Coffee") must not be held to whole-string similarity —
    # jaro_winkler('startbucks', 'starbucks coffee') is 0.89, under the
    # threshold. Each name is therefore also compared through a prefix
    # window of the query's own length, and the better of the two scores
    # counts; the 0.92 threshold itself is unchanged.
    #
    # Empty when the query folds to the empty string, same guard as
    # geocode._query_divisions_fuzzy and for the same reason: DuckDB scores
    # jaro_winkler_similarity(name, '') above the threshold, which would
    # otherwise answer a nonsense query with the nearest places, dressed up
    # as spelling corrections.
    if not folded_name:
        return []
    with_clause, from_clause, dedup_filters = _dedup_rows_sql(
        from_source, base_filters, has_recreation
    )
    fuzzy_params = dict(base_params, _folded_name=folded_name)
    folded_col = _fold_poi_name_sql("names.primary")
    similarity_expr = (
        f"greatest("
        f"jaro_winkler_similarity({folded_col}, $_folded_name), "
        f"jaro_winkler_similarity("
        f"left({folded_col}, length($_folded_name)), $_folded_name))"
    )
    # _dedup_rows_sql's with_clause (when the recreation layer is in play)
    # already opens its own "WITH _candidate_rows AS (...)" — chaining a
    # second CTE onto it is a comma, not a second WITH keyword. When there's
    # no recreation layer, with_clause is "" and this pool CTE opens its own.
    pool_cte = (
        f"_pool AS MATERIALIZED ("
        f"SELECT * FROM ("
        f"SELECT {select_cols}, {similarity_expr} AS similarity "
        f"FROM {from_clause} WHERE {' AND '.join(dedup_filters)}) "
        f"WHERE similarity >= {_POI_FUZZY_SIMILARITY_THRESHOLD} "
        f"ORDER BY distance_m LIMIT {_POI_FUZZY_POOL_SIZE})"
    )
    if with_clause:
        pool_with_clause = f"{with_clause}, {pool_cte}"
    else:
        pool_with_clause = f"WITH {pool_cte}"
    # Nearest first, like every other find_places path (#374): similarity
    # already gated admission above, so it only breaks distance ties here —
    # a fallback row 900m away must not outrank one 50m away just because
    # its spelling scored a shade closer.
    fuzzy_sql = f"""
        {pool_with_clause}
        SELECT {', '.join(cols)}, similarity
        FROM _pool
        ORDER BY distance_m, similarity DESC
        LIMIT {limit}
    """
    try:
        with trace.scan("places fuzzy scan", bounded=True, source=from_clause), _conn_lock:
            rows = _conn().execute(fuzzy_sql, fuzzy_params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    results = [dict(zip([*cols, "similarity"], r)) for r in rows]
    for d in results:
        d["matched_by"] = "fuzzy"
        d.pop("similarity")
    return results


def _expand_need_slugs(slugs: list[str]) -> list[str]:
    """Self plus bundled-taxonomy descendants, de-duplicated.

    Unknown slugs are kept as-is so an exact primary match can still fire.
    """
    seen: set[str] = set()
    out: list[str] = []
    for slug in slugs:
        if not slug:
            continue
        children = categories.slugs_under(slug)
        for item in children or [slug]:
            key = item.lower()
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out


def _place_categories_or_filter(
    missing: set[str], slugs: list[str], params: dict, prefix: str = "cat"
) -> list[str]:
    """OR of several category slugs — exact / hierarchy, never substring.

    Public find_places keeps ILIKE so "coffee" still hits coffee_shop.
    Verdict compose must not: park is not parking, school is not
    driving_school. Each need slug is expanded to {self + descendants}
    via the bundled taxonomy, then matched by equality on
    basic_category / taxonomy.primary or list_contains on alternates.

    prefix namespaces the bound parameter names so several calls (one per
    counted category, as _count_places_by_category makes) can share one
    params dict without colliding.
    """
    clauses = []
    for i, slug in enumerate(_expand_need_slugs(slugs)):
        if not slug:
            continue
        parts = []
        exact = slug.lower()
        if "basic_category" not in missing:
            parts.append(f"lower(basic_category) = ${prefix}{i}_exact")
        if "taxonomy" not in missing:
            parts.append(f"lower(taxonomy.primary) = ${prefix}{i}_exact")
            parts.append(f"list_contains(taxonomy.alternates, ${prefix}{i}_raw)")
        if parts:
            clauses.append(f"({' OR '.join(parts)})")
            params[f"{prefix}{i}_exact"] = exact
            params[f"{prefix}{i}_raw"] = slug
    if not clauses:
        return []
    return [f"({' OR '.join(clauses)})"]


def _place_attribute_filters(
    missing: set[str],
    min_confidence: float | None,
    operating_status: str | None,
    params: dict,
) -> list[str]:
    """min_confidence/operating_status filter clauses (params added in
    place), shared by find_places and find_places_in_division so both modes
    of the find_places tool honor the same filters — otherwise a filter
    passed alongside division_id would be silently ignored.

    Raises ValueError for an out-of-range min_confidence or an
    unrecognized operating_status. Each filter is a no-op (not an error)
    when its column is absent from the active dataset, matching how
    category/name already degrade.
    """
    filters = []
    if min_confidence is not None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0")
        if "confidence" not in missing:
            filters.append("confidence >= $min_confidence")
            params["min_confidence"] = min_confidence
    if operating_status is not None:
        raw_values = _resolve_operating_status(operating_status)
        if "operating_status" not in missing:
            if len(raw_values) == 1:
                filters.append("operating_status = $operating_status0")
                params["operating_status0"] = raw_values[0]
            else:
                placeholders = [f"$operating_status{i}" for i in range(len(raw_values))]
                filters.append(f"operating_status IN ({', '.join(placeholders)})")
                for i, v in enumerate(raw_values):
                    params[f"operating_status{i}"] = v
    return filters


def _place_presence_filters(
    missing: set[str],
    brand: str | None,
    has_website: bool | None,
    has_phone: bool | None,
    params: dict,
) -> list[str]:
    """brand/has_website/has_phone filter clauses (params added in place),
    shared by find_places and find_places_in_division so both modes of the
    find_places tool honor them.

    brand is a substring match on the place's brand name; has_website and
    has_phone filter on whether the place has any entries at all, not on
    their content. Each is a no-op (not an error) when its column is
    absent from the active dataset, matching category/name.
    """
    filters = []
    if brand is not None and "brand" not in missing:
        filters.append("brand.names.primary ILIKE $brand ESCAPE '\\'")
        params["brand"] = f"%{_like_escape(brand)}%"
    if has_website is not None and "websites" not in missing:
        if has_website:
            filters.append("(websites IS NOT NULL AND len(websites) > 0)")
        else:
            filters.append("(websites IS NULL OR len(websites) = 0)")
    if has_phone is not None and "phones" not in missing:
        if has_phone:
            filters.append("(phones IS NOT NULL AND len(phones) > 0)")
        else:
            filters.append("(phones IS NULL OR len(phones) = 0)")
    return filters


def _place_select_exprs(missing: set[str]) -> dict[str, str]:
    """SQL expressions for a place row's compact-result columns, degrading
    to NULL for any column absent from the active dataset — shared by
    find_places and find_places_in_division so both rows have the same
    shape and degrade the same way."""
    return {
        "id": "NULL" if "id" in missing else "id",
        "name": "NULL" if "names" in missing else "names.primary",
        "category": "NULL" if "taxonomy" in missing else "taxonomy.primary",
        "basic_category": "NULL" if "basic_category" in missing else "basic_category",
        "operating_status": "NULL" if "operating_status" in missing else "operating_status",
        "confidence": "NULL" if "confidence" in missing else "round(confidence, 2)",
        "brand": "NULL" if "brand" in missing else "brand.names.primary",
        "has_website": (
            "FALSE" if "websites" in missing
            else "(websites IS NOT NULL AND len(websites) > 0)"
        ),
        "has_phone": (
            "FALSE" if "phones" in missing else "(phones IS NOT NULL AND len(phones) > 0)"
        ),
    }


def find_places(
    lat: float,
    lon: float,
    radius_m: float = 1000,
    category: str | None = None,
    name: str | None = None,
    min_confidence: float | None = None,
    operating_status: str | None = None,
    brand: str | None = None,
    has_website: bool | None = None,
    has_phone: bool | None = None,
    limit: int = 10,
    allow_name_fallback: bool = True,
    offset: int = 0,
) -> list[dict]:
    """Places near a point, nearest first, compact rows.

    min_confidence (0.0-1.0) filters to rows with confidence >= that
    threshold; raises ValueError if out of range. operating_status filters
    to rows matching either a relabeled value ("in business", "permanently
    closed", "temporarily closed") or a raw Overture value ("open",
    "closed", "closed_permanently", "closed_temporarily"), matched
    case-insensitively; raises ValueError if unrecognized. Both are a no-op
    (not an error) if the underlying column is missing from the active
    dataset — see degraded_fields().

    brand is a substring match on the place's brand name (e.g. a chain);
    has_website/has_phone filter on whether a place has any website/phone
    entries at all, not on their content. Rows carry brand (str or None)
    and has_website/has_phone (bool presence flags) — not the raw
    websites/phones arrays, which stay compact and are only exposed in
    full by place_details. Note brand is sparse (chains only): a null
    brand does NOT mean "not that chain".

    Raises SchemaDegraded if bbox is missing from the active dataset (the
    tool can't answer at all without it), or UpstreamUnavailable if the
    remote scan fails after retries. Non-essential columns missing from the
    dataset come back as None in their field — see degraded_fields().

    #373: when `name` is given and the literal ILIKE substring search comes
    back empty, two fallback tiers run in order, both staying inside this
    same bbox/category/attribute pruning (never an unbounded scan): a
    names.common alternate-spelling match ("Munich" -> a place named
    "München"), then a jaro_winkler fuzzy match over a bounded nearest-first
    candidate pool ("Startbucks" -> "Starbucks"). Rows found either way
    carry "matched_by": "alt_name" | "fuzzy" (absent on an ordinary tier-1
    row) — a caller reading that field can tell a spelling correction from
    a literal hit. names.common missing from the dataset silently skips the
    alt-name tier; the fuzzy tier always runs (jaro_winkler is a built-in
    DuckDB function). allow_name_fallback=False disables both fallback
    tiers — for callers whose answer shape can't honestly carry a
    correction (#374: within_distance is a yes/no with no note surface, and
    "is there a Startbucks within 200m" must not silently become a claim
    about Starbucks).

    offset skips that many rows of the same nearest-first ordering before
    LIMIT applies (cursor pagination, ROADMAP feature 4) - 0 for a first
    page. Ordering breaks distance ties by id so repeated calls at
    different offsets never skip or repeat a row. A server caller wanting
    to detect "more rows exist beyond this page" passes limit+1 and slices
    the extra row off itself; this function's own contract is otherwise
    unchanged. The name-fallback tiers only run at offset 0 (see below) -
    a later page finding zero rows means the query ran past the end, not
    that tier 1 found nothing.
    """
    # int() before interpolating into the SQL LIMIT/OFFSET clauses: the MCP
    # layer already type-validates these args, but the cast makes the query
    # layer safe for any direct (non-MCP) caller too, rather than relying on
    # the transport for injection safety. limit stays clamped to [0,
    # MAX_ROWS] exactly like every other query-layer function — the
    # find_places *tool*'s own overfetch-by-one (it requests
    # effective_limit + 1 to detect "more rows exist") is therefore capped
    # at MAX_ROWS too, so has_more can't be detected on a page that's
    # already at the maximum size; that's an accepted, documented edge
    # case rather than a reason to relax this function's own cap. offset
    # clamped to a non-negative int.
    limit = max(0, min(int(limit), MAX_ROWS))
    offset = max(0, int(offset))
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    bbox_filter, distance_filter, params, bbox, _radius_m = area_geometry(lat, lon, radius_m)
    from_source, has_recreation = _places_source(bbox)
    base_filters = [bbox_filter, distance_filter]
    base_filters.extend(_name_filter(missing, has_recreation))
    base_filters.extend(_place_category_name_filters(missing, category, None, params))
    base_filters.extend(
        _place_attribute_filters(missing, min_confidence, operating_status, params)
    )
    base_filters.extend(
        _place_presence_filters(missing, brand, has_website, has_phone, params)
    )
    name_filters = _place_category_name_filters(missing, None, name, params)
    filters = base_filters + name_filters

    exprs = _place_select_exprs(missing)
    with_clause, from_clause, filters = _dedup_rows_sql(from_source, filters, has_recreation)

    sql = f"""
        {with_clause}
        SELECT
            {exprs["id"]}                       AS id,
            {exprs["name"]}                      AS name,
            {exprs["category"]}                  AS category,
            {exprs["basic_category"]}             AS basic_category,
            {exprs["operating_status"]}           AS operating_status,
            {exprs["confidence"]}                AS confidence,
            {exprs["brand"]}                     AS brand,
            {exprs["has_website"]}               AS has_website,
            {exprs["has_phone"]}                 AS has_phone,
            round(bbox.ymin, 6)                 AS lat,
            round(bbox.xmin, 6)                 AS lon,
            round({_DISTANCE_EXPR}, 0)          AS distance_m
        FROM {from_clause}
        WHERE {' AND '.join(filters)}
        ORDER BY distance_m, id
        LIMIT {limit}
        OFFSET {offset}
    """
    try:
        # Always bounded: find_places is a radius query, so the bbox filter
        # and the distance predicate are both in `filters` above.
        with trace.scan("places radius scan", bounded=True, source=from_clause), _conn_lock:
            rows = _conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    cols = [
        "id", "name", "category", "basic_category", "operating_status",
        "confidence", "brand", "has_website", "has_phone", "lat", "lon", "distance_m",
    ]
    results = [dict(zip(cols, r)) for r in rows]
    if not results and name and allow_name_fallback and "names" not in missing and offset == 0:
        # #373: tier 1 (the ILIKE substring search above) found nothing —
        # try the alt-name/fuzzy fallback tiers before giving up. Skipped
        # outright when `name` wasn't given (nothing to fall back from),
        # "names" is missing from the dataset (tier 1 already degraded to
        # a no-op in that case, and there's no primary/alternate name
        # column to fall back to either), or offset > 0 (a later page
        # coming back empty means the query ran past the end of tier 1's
        # own results, not that tier 1 found nothing at all — falling back
        # there would silently restart the search from a different tier).
        results = _find_places_name_fallback(
            from_source, base_filters, params, has_recreation, exprs, name, limit
        )
    for d in results:
        _annotate_place(d)
    return results


# Higher than MAX_ROWS: a checklist of several slugs in one scan still
# needs a nearest hit per slug, and a dense cluster of one category
# would otherwise fill a 25-row cap before the others appear.
CHECKLIST_MAX_CANDIDATES = 80


def find_places_for_categories(
    lat: float,
    lon: float,
    radius_m: float,
    categories: list[str],
    limit: int = CHECKLIST_MAX_CANDIDATES,
) -> list[dict]:
    """Places matching any of `categories`, nearest first — one scan.

    Internal compose helper (neighborhood_verdict). The public find_places
    API is single-category; this is the union so a 4-8 need checklist does
    not become N sequential scans of the same bbox.
    """
    slugs = [s for s in categories if s]
    if not slugs:
        return []
    limit = max(0, min(int(limit), CHECKLIST_MAX_CANDIDATES))
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    bbox_filter, distance_filter, params, bbox, _radius_m = area_geometry(lat, lon, radius_m)
    from_source, has_recreation = _places_source(bbox)
    filters = [bbox_filter, distance_filter]
    filters.extend(_name_filter(missing, has_recreation))
    filters.extend(_place_categories_or_filter(missing, slugs, params))

    exprs = _place_select_exprs(missing)
    with_clause, from_clause, filters = _dedup_rows_sql(from_source, filters, has_recreation)

    sql = f"""
        {with_clause}
        SELECT
            {exprs["id"]}                       AS id,
            {exprs["name"]}                      AS name,
            {exprs["category"]}                  AS category,
            {exprs["basic_category"]}             AS basic_category,
            {exprs["operating_status"]}           AS operating_status,
            {exprs["confidence"]}                AS confidence,
            {exprs["brand"]}                     AS brand,
            {exprs["has_website"]}               AS has_website,
            {exprs["has_phone"]}                 AS has_phone,
            round(bbox.ymin, 6)                 AS lat,
            round(bbox.xmin, 6)                 AS lon,
            round({_DISTANCE_EXPR}, 0)          AS distance_m
        FROM {from_clause}
        WHERE {' AND '.join(filters)}
        ORDER BY distance_m
        LIMIT {limit}
    """
    try:
        with trace.scan("places checklist scan", bounded=True, source=from_clause), _conn_lock:
            rows = _conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    cols = [
        "id", "name", "category", "basic_category", "operating_status",
        "confidence", "brand", "has_website", "has_phone", "lat", "lon", "distance_m",
    ]
    results = [dict(zip(cols, r)) for r in rows]
    for d in results:
        d["operating_status"] = _label_operating_status(d["operating_status"])
    return results


# Candidate cap for find_places_in_bbox: a corridor bbox has no radius to
# bound it, so a dense city between two points could otherwise pull an
# unbounded candidate set into Python for the per-place corridor test. Same
# "honest partial rather than unbounded" idiom as routing.MAX_GRAPH_SEGMENTS
# — the caller sees whether the cap was hit and can say so. A cap always
# slices by the query's ORDER BY rather than by geography, so callers
# covering an extended shape should spend this budget over several small
# boxes (see routing.CORRIDOR_BBOX_CHUNKS) rather than one large one.
BBOX_MAX_CANDIDATES = 500


def find_places_in_bbox(
    bbox: tuple[float, float, float, float],
    category: str | None = None,
    name: str | None = None,
    limit: int = BBOX_MAX_CANDIDATES,
    near: tuple[float, float] | None = None,
) -> tuple[list[dict], bool]:
    """Places whose point falls in an (xmin, ymin, xmax, ymax) box.

    The raw-box counterpart to find_places' point+radius circle, for callers
    that have a shape of their own to test against and only need the box as
    a cheap prefilter — currently routing.places_along_route, which measures
    each candidate against the route corridor in Python. bbox may run
    outside [-180, 180] in longitude exactly like geo.bbox_around's output;
    geo.bbox_filter_sql folds an antimeridian-crossing box into two in-range
    boxes.

    Rows come back in find_places' shape minus distance_m (there's no
    reference point to rank from — the caller supplies its own ordering),
    ordered by name then id so the row set is deterministic. category/name
    narrow results with exactly the same semantics as find_places (shared
    _place_category_name_filters).

    `near` is an optional (lat, lon) the rows are ranked by proximity to
    instead — it decides *which* rows survive `limit` when the box holds
    more than that, so a capped query returns the places closest to the
    point of interest rather than an alphabetical slice. The ranking is a
    cheap planar squared distance (longitude scaled by cos(lat)), which is
    plenty for an ordering the caller re-measures exactly afterwards; name
    and id still break ties so the row set stays deterministic. Pass it only
    for an in-range box: the expression compares raw longitudes, so it would
    rank the far side of an antimeridian-crossing box as distant.

    Returns (rows, capped); capped is True when the query hit `limit` rows,
    meaning the box holds more matching places than were returned and the
    caller's own filtering saw only a partial candidate set.

    Raises SchemaDegraded if bbox is missing from the active dataset, or
    UpstreamUnavailable if the remote scan fails after retries.
    """
    limit = max(0, int(limit))
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    xmin, ymin, xmax, ymax = bbox
    bbox_filter, params = _bbox_filter_sql(xmin, ymin, xmax, ymax)
    from_source, has_recreation = _places_source(bbox)
    filters = [bbox_filter]
    filters.extend(_name_filter(missing, has_recreation))
    filters.extend(_place_category_name_filters(missing, category, name, params))

    order_by = "name, id"
    if near is not None:
        near_lat, near_lon = near
        params["near_lat"] = near_lat
        params["near_lon"] = near_lon
        params["near_coslat"] = max(math.cos(math.radians(near_lat)), 1e-6)
        order_by = (
            "(bbox.ymin - $near_lat) * (bbox.ymin - $near_lat)"
            " + ((bbox.xmin - $near_lon) * $near_coslat)"
            " * ((bbox.xmin - $near_lon) * $near_coslat), name, id"
        )

    exprs = _place_select_exprs(missing)
    with_clause, from_clause, filters = _dedup_rows_sql(from_source, filters, has_recreation)
    sql = f"""
        {with_clause}
        SELECT
            {exprs["id"]}                       AS id,
            {exprs["name"]}                      AS name,
            {exprs["category"]}                  AS category,
            {exprs["basic_category"]}             AS basic_category,
            {exprs["operating_status"]}           AS operating_status,
            {exprs["confidence"]}                AS confidence,
            {exprs["brand"]}                     AS brand,
            {exprs["has_website"]}               AS has_website,
            {exprs["has_phone"]}                 AS has_phone,
            round(bbox.ymin, 6)                 AS lat,
            round(bbox.xmin, 6)                 AS lon
        FROM {from_clause}
        WHERE {' AND '.join(filters)}
        ORDER BY {order_by}
        LIMIT {limit}
    """
    try:
        with _conn_lock:
            rows = _conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    cols = [
        "id", "name", "category", "basic_category", "operating_status",
        "confidence", "brand", "has_website", "has_phone", "lat", "lon",
    ]
    results = [dict(zip(cols, r)) for r in rows]
    for d in results:
        _annotate_place(d)
    return results, len(results) >= limit


# division_area (divisions theme) columns find_places_in_division depends on
# to resolve a division's polygon by id. geometry is essential (mirrors
# divisions.py's admin_lookup) — without it there's no containment test to
# run. division_id is soft: falls back to id (see _resolve_division_geometry)
# the same way admin_lookup does.
_DIVISION_AREA_REQUIRED_COLUMNS = ["id", "geometry", "division_id"]
_DIVISION_AREA_ESSENTIAL_COLUMNS = {"geometry"}


def _ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once.

    Thin wrapper over db.ensure_spatial(), mirroring divisions.py's own
    (kept separate rather than imported from there to avoid a
    divisions<->overture import cycle: divisions.py already imports overture).
    """
    try:
        db.ensure_spatial()
    except duckdb.Error as e:
        raise UpstreamUnavailable(f"could not load spatial extension: {e}") from e


# Resolved division polygons, keyed by (division_area glob, division_id).
# Step 1 of the polygon search scans the divisions theme for one id, which
# is NOT partition- or row-group-prunable (there's no point to prune by, and
# the ids aren't sorted), so it costs a full column scan of a large theme --
# measured at tens of seconds against live Overture data (#148). The result
# is immutable for a given release, and an agent exploring one area
# typically issues several queries against the same division, so caching it
# turns every call after the first into a local dict hit.
#
# Keyed by glob so a set_data_path() switch (tests, a mirror swap, a new
# release) can never serve a polygon resolved against the previous dataset.
#
# Bounded by total WKB bytes rather than entry count: division polygons vary
# enormously (a neighborhood is a few KB, a country with islands can be tens
# of MB), so an entry-count cap would either be useless for small ones or
# hold hundreds of MB of large ones. A single polygon bigger than the whole
# budget is simply not cached — it still resolves, it just doesn't evict
# everything else to sit there alone.
_DIVISION_GEOMETRY_CACHE_MAX_BYTES = 64 * 1024 * 1024
_division_geometry_cache: dict[tuple[str, str], tuple | None] = {}
_division_geometry_lock = threading.Lock()


def clear_division_geometry_cache() -> None:
    """Drop every cached division polygon. For tests and hot-reloads."""
    with _division_geometry_lock:
        _division_geometry_cache.clear()


# On-disk twin of _division_geometry_cache (#153). The in-process cache (#148)
# turns the second resolve of a division_id into a dict hit, but the FIRST
# resolve of every process pays the full ~73s division_area column scan (no
# point to prune by, ids unsorted). Persisting the resolved polygon under the
# tile cache dir, keyed by the division_area glob (which encodes release +
# theme + any set_data_path override) and division_id, makes that cost
# once-per-release-per-machine instead of once-per-process. What's cached is
# the exact ST_Union_Agg result (WKB + its bbox), so the merged polygon is
# byte-for-byte identical — a coastal division's land+maritime union can never
# silently shrink, which is the hazard the naive bbox-prefilter fix risks.
# Serialized as: 1 flag byte (1=found, 0=cached miss), then for a hit 4
# little-endian float64 bbox values + the raw WKB. No hex, no pickle.
_DIVISION_DISK_MISS = object()


def _division_polygon_disk_path(div_upstream: str, division_id: str):
    """Local cache file for a resolved division polygon, or None if disabled."""
    if not cache.enabled():
        return None
    glob_key = hashlib.sha256(div_upstream.encode("utf-8")).hexdigest()[:16]
    id_key = hashlib.sha256(division_id.encode("utf-8")).hexdigest()[:32]
    return cache.cache_dir() / "division_polygons" / glob_key / f"{id_key}.bin"


def _read_division_polygon_disk(path):
    """Resolved tuple from disk, None for a cached miss, or _DIVISION_DISK_MISS
    if the file is absent/unreadable/corrupt (caller should re-resolve)."""
    try:
        data = path.read_bytes()
    except OSError:
        return _DIVISION_DISK_MISS
    if not data:
        return _DIVISION_DISK_MISS
    if data[0] == 0:
        return None  # a cached "unknown id" miss
    try:
        xmin, xmax, ymin, ymax = struct.unpack("<dddd", data[1:33])
    except struct.error:
        return _DIVISION_DISK_MISS  # truncated/corrupt — re-resolve
    return (bytes(data[33:]), xmin, xmax, ymin, ymax)


def _write_division_polygon_disk(path, resolved: tuple | None) -> None:
    """Best-effort persist. A disk failure never breaks the resolve."""
    if resolved is None:
        payload = b"\x00"
    else:
        wkb, xmin, xmax, ymin, ymax = resolved
        payload = b"\x01" + struct.pack("<dddd", xmin, xmax, ymin, ymax) + bytes(wkb)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)  # atomic — a reader sees the whole file or none
    except OSError as e:
        logger.warning("Could not persist division polygon to %s: %s", path, e)


def _resolve_division_geometry(
    division_id: str,
) -> tuple[bytes, float, float, float, float] | None:
    """A division's merged boundary polygon (WKB) and its bbox, by GERS id.

    division_area carries multiple polygon rows per division (e.g. land and
    maritime variants — see divisions.py's admin_lookup docstring) sharing
    one division_id; ST_Union_Agg merges every matching row into a single
    geometry so a place inside ANY variant counts as inside the division,
    not just its first (smallest-area) row. The bbox is derived straight
    from that merged geometry (ST_XMin/XMax/YMin/YMax) rather than read off
    a bbox column on division_area — real Overture division_area rows don't
    reliably carry one the way place rows do.

    Returns None if no division_area row's id matches (division_id is
    unknown in the active dataset — the caller/server turns that into a
    not_found error), never an empty-but-valid geometry (ST_Union_Agg over
    zero rows is NULL, which this treats the same as "no match").

    Raises SchemaDegraded if geometry is missing from the active divisions
    dataset (mirrors admin_lookup), or UpstreamUnavailable if the remote
    scan (or the one-time spatial extension load) fails.
    """
    _ensure_spatial()
    div_upstream = _upstream_glob(theme="divisions", type_="division_area")
    cache_key = (div_upstream, division_id)
    with _division_geometry_lock:
        if cache_key in _division_geometry_cache:
            return _division_geometry_cache[cache_key]
    # On-disk cache (#153): survives process restarts, so only the first
    # resolve of a division per release per machine pays the ~73s scan.
    disk_path = _division_polygon_disk_path(div_upstream, division_id)
    if disk_path is not None:
        disk = _read_division_polygon_disk(disk_path)
        if disk is not _DIVISION_DISK_MISS:
            _cache_division_geometry(cache_key, disk)  # promote into the process
            return disk
    missing = set(missing_columns(div_upstream, _DIVISION_AREA_REQUIRED_COLUMNS))
    essential_missing = [c for c in missing if c in _DIVISION_AREA_ESSENTIAL_COLUMNS]
    if essential_missing:
        raise SchemaDegraded(essential_missing)

    if "division_id" not in missing:
        id_filter_expr = "coalesce(division_id, id)"
    elif "id" not in missing:
        id_filter_expr = "id"
    else:
        id_filter_expr = "NULL"
    geom_expr = geo.geom_expr(div_upstream)

    sql = f"""
        WITH merged AS (
            SELECT ST_Union_Agg({geom_expr}) AS geom
            FROM read_parquet('{div_upstream}', hive_partitioning=1)
            WHERE {id_filter_expr} = $division_id
        )
        SELECT ST_AsWKB(geom), ST_XMin(geom), ST_XMax(geom), ST_YMin(geom), ST_YMax(geom)
        FROM merged
    """
    try:
        with db.conn_lock:
            row = db.shared_conn().execute(sql, {"division_id": division_id}).fetchone()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    # ST_Union_Agg over zero matching rows yields an empty GEOMETRYCOLLECTION
    # (ST_AsWKB(...) on it is non-NULL, just empty), but ST_XMin/etc. on an
    # empty geometry are NULL -- check the bbox extent, not the WKB, to
    # detect "division_id matched nothing."
    resolved = None if (row is None or row[1] is None) else row
    _cache_division_geometry(cache_key, resolved)
    if disk_path is not None:
        _write_division_polygon_disk(disk_path, resolved)
    return resolved


def _division_geometry_bytes(entry: tuple | None) -> int:
    """Approximate memory held by a cache entry (its WKB blob)."""
    return 0 if entry is None else len(entry[0])


def _cache_division_geometry(cache_key: tuple[str, str], resolved: tuple | None) -> None:
    """Store a resolved polygon, evicting oldest-first to stay under budget.

    A miss (unknown id) is cached too — it costs the same full scan to
    re-derive, and repeating a bad id shouldn't buy another one.
    """
    size = _division_geometry_bytes(resolved)
    if size > _DIVISION_GEOMETRY_CACHE_MAX_BYTES:
        return
    with _division_geometry_lock:
        _division_geometry_cache[cache_key] = resolved
        total = sum(_division_geometry_bytes(v) for v in _division_geometry_cache.values())
        while total > _DIVISION_GEOMETRY_CACHE_MAX_BYTES and len(_division_geometry_cache) > 1:
            # dicts preserve insertion order, so this is oldest-first.
            oldest = next(iter(_division_geometry_cache))
            total -= _division_geometry_bytes(_division_geometry_cache.pop(oldest))


def find_places_in_division(
    division_id: str,
    category: str | None = None,
    name: str | None = None,
    min_confidence: float | None = None,
    operating_status: str | None = None,
    brand: str | None = None,
    has_website: bool | None = None,
    has_phone: bool | None = None,
    limit: int = 10,
    offset: int = 0,
) -> list[dict] | None:
    """Places whose point falls inside a division's boundary polygon.

    Boundary-accurate alternative to find_places' point+radius circle: pass
    a division's GERS id (e.g. from admin_lookup's chain) instead of a
    guessed radius, and get back only places truly inside that division's
    shape — not a circle that may clip a coastline or straddle a border.

    Two-step query mirroring divisions.py's admin_lookup + find_places: (1)
    resolve the division's merged polygon and bbox by id
    (_resolve_division_geometry); (2) scan places, prefiltered by a bbox
    intersection against the division's bbox (mandatory — an unfiltered
    ST_Contains over the full places dataset is a full-table scan) and then
    exactly tested with ST_Contains against the merged polygon. category/name
    narrow the search exactly as they do for find_places, as do
    min_confidence/operating_status and brand/has_website/has_phone.
    Results are
    ordered by name then id (there's no reference point to rank by distance
    from, unlike find_places) and rows come back in the same shape as
    find_places' minus distance_m.

    Returns None if division_id doesn't match any division_area row (the
    server tool turns that into a {"error": "not_found", ...}) — distinct
    from an empty list, which means the division resolved but has no
    matching places inside it.

    Raises SchemaDegraded if bbox is missing from the active places dataset,
    or if geometry is missing from the active divisions dataset (either way,
    there's no way to answer), or UpstreamUnavailable if either remote scan
    fails after retries. Non-essential place columns missing from the
    dataset come back as None in their field — see degraded_fields().

    offset skips that many rows of the same name/id ordering before LIMIT
    applies (cursor pagination, ROADMAP feature 4); ordering is already
    stable (name, id), so pages never overlap or skip. limit stays clamped
    to [0, MAX_ROWS] — see find_places' offset docstring for the same
    accepted has_more-at-the-max-page edge case.
    """
    limit = max(0, min(int(limit), MAX_ROWS))
    offset = max(0, int(offset))
    resolved = _resolve_division_geometry(division_id)
    if resolved is None:
        return None
    geom_wkb, div_xmin, div_xmax, div_ymin, div_ymax = resolved

    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))

    # Mandatory bbox prefilter (place bbox intersects the division's bbox)
    # before the expensive exact ST_Contains test — same reasoning as
    # admin_lookup's bbox_prefilter: unfiltered, this is a polygon test
    # against every place on Earth.
    bbox_filter = (
        "bbox.xmin <= $div_xmax AND bbox.xmax >= $div_xmin"
        " AND bbox.ymin <= $div_ymax AND bbox.ymax >= $div_ymin"
    )
    contains_filter = "ST_Contains(ST_GeomFromWKB($geom_wkb), ST_Point(bbox.xmin, bbox.ymin))"
    params = {
        "div_xmin": div_xmin, "div_xmax": div_xmax,
        "div_ymin": div_ymin, "div_ymax": div_ymax,
        "geom_wkb": geom_wkb,
    }
    bbox = (div_xmin, div_ymin, div_xmax, div_ymax)
    from_source, has_recreation = _places_source(bbox)
    filters = [bbox_filter, contains_filter]
    filters.extend(_name_filter(missing, has_recreation))
    filters.extend(_place_category_name_filters(missing, category, name, params))
    filters.extend(
        _place_attribute_filters(missing, min_confidence, operating_status, params)
    )
    filters.extend(
        _place_presence_filters(missing, brand, has_website, has_phone, params)
    )

    exprs = _place_select_exprs(missing)
    with_clause, from_clause, filters = _dedup_rows_sql(from_source, filters, has_recreation)

    sql = f"""
        {with_clause}
        SELECT
            {exprs["id"]}                       AS id,
            {exprs["name"]}                      AS name,
            {exprs["category"]}                  AS category,
            {exprs["basic_category"]}             AS basic_category,
            {exprs["operating_status"]}           AS operating_status,
            {exprs["confidence"]}                AS confidence,
            {exprs["brand"]}                     AS brand,
            {exprs["has_website"]}               AS has_website,
            {exprs["has_phone"]}                 AS has_phone,
            round(bbox.ymin, 6)                 AS lat,
            round(bbox.xmin, 6)                 AS lon
        FROM {from_clause}
        WHERE {' AND '.join(filters)}
        ORDER BY name, id
        LIMIT {limit}
        OFFSET {offset}
    """
    try:
        with _conn_lock:
            rows = _conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    cols = [
        "id", "name", "category", "basic_category", "operating_status",
        "confidence", "brand", "has_website", "has_phone", "lat", "lon",
    ]
    results = [dict(zip(cols, r)) for r in rows]
    for d in results:
        _annotate_place(d)
    return results


def summarize_area(lat: float, lon: float, radius_m: float = 1000) -> dict:
    """Category mix for an area — an answer, not a data dump.

    Raises SchemaDegraded if bbox is missing, or UpstreamUnavailable if the
    remote scan fails after retries. If basic_category is missing, every
    place counts as uncategorized (top_categories comes back empty) rather
    than failing the call — see degraded_fields().
    """
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    bbox_filter, distance_filter, params, bbox, effective_radius_m = area_geometry(
        lat, lon, radius_m
    )
    category_expr = "NULL" if "basic_category" in missing else "basic_category"
    from_source, has_recreation = _places_source(bbox)
    with_clause, from_clause, filters = _dedup_rows_sql(
        from_source, [bbox_filter, distance_filter], has_recreation
    )
    sql = f"""
        {with_clause}
        SELECT
            {category_expr} AS category,
            count(*) AS n,
            sum(count(*)) OVER ()                                     AS total,
            coalesce(sum(count(*)) FILTER (WHERE {category_expr} IS NULL) OVER (), 0)
                                                                      AS uncategorized
        FROM {from_clause}
        WHERE {" AND ".join(filters)}
        GROUP BY 1
        ORDER BY n DESC
    """
    try:
        with _conn_lock:
            rows = _conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    total_places = rows[0][2] if rows else 0
    uncategorized_count = rows[0][3] if rows else 0
    categorized = [r for r in rows if r[0] is not None]
    top = categorized[:MAX_ROWS]
    other_categories_count = sum(n for _, n, _, _ in categorized[MAX_ROWS:])
    return {
        "center": {"lat": lat, "lon": lon},
        "radius_m": effective_radius_m,
        "total_places": total_places,
        "top_categories": [{"category": c, "count": n} for c, n, _, _ in top],
        "other_categories_count": other_categories_count,
        "uncategorized_count": uncategorized_count,
    }


# Array-valued fields on a place row that can be long enough to blow the
# token budget on their own (issue #9) — truncated via budget.truncate_list,
# never dropped silently. Kept small and explicit rather than derived, since
# it's the set place_details actually selects below.
_PLACE_DETAIL_LIST_FIELDS = ("addresses", "websites", "phones", "socials", "sources")

# place_details' own default search radius when resolving by name + lat/lon
# instead of by GERS id — a small "did you mean the place right here" window,
# not a general-purpose area search.
DEFAULT_DETAILS_RADIUS_M = 200

# Issue #41: a location hint (near_lat/near_lon) on an id lookup gets the same
# bbox prefilter treatment as any other query, just with a much wider box —
# the caller usually only knows roughly where the place was found (e.g. a
# find_places row's lat/lon), not that it's within DEFAULT_DETAILS_RADIUS_M.
# 50km comfortably covers "same metro area" while still pruning row groups
# for anything but a very dense, very large release.
ID_HINT_RADIUS_M = 50_000

# (source column that gates this field via `missing`, SQL expression, result alias)
_PLACE_DETAIL_COLUMNS = [
    ("id", "id", "id"),
    ("names", "names.primary", "name"),
    ("taxonomy", "taxonomy.primary", "category"),
    ("basic_category", "basic_category", "basic_category"),
    ("operating_status", "operating_status", "operating_status"),
    ("confidence", "round(confidence, 2)", "confidence"),
    ("brand", "brand.names.primary", "brand"),
    ("addresses", "addresses", "addresses"),
    ("websites", "websites", "websites"),
    ("phones", "phones", "phones"),
    ("socials", "socials", "socials"),
    ("sources", "sources", "sources"),
]
_PLACE_DETAIL_RESULT_COLS = (
    [alias for _, _, alias in _PLACE_DETAIL_COLUMNS]
    + ["lat", "lon", recreation.MARKER_COLUMN]
)


def _place_details_sql(
    from_source: str, filters: list[str], order_by: str, missing: set[str],
    has_recreation: bool = False, with_clause: str = "",
) -> str:
    """The shared place_details SELECT, sourced from from_source.

    Column list and result shape stay identical no matter which of
    place_details' several lookup strategies (cached-tile, hint-constrained,
    full-scan, or name+point) supplies from_source/filters — only the FROM
    and WHERE differ between them. The marker column is selected so the
    caller can label a recreation-layer row's provenance (source_theme:
    base) — and so gers.py can name the right theme for its id; it reads
    FALSE whenever the union isn't active and the column doesn't exist.
    """
    select_list = ",\n            ".join(
        f'{"NULL" if col in missing else expr} AS {alias}'
        for col, expr, alias in _PLACE_DETAIL_COLUMNS
    )
    marker_expr = recreation.MARKER_COLUMN if has_recreation else "FALSE"
    return f"""
        {with_clause}
        SELECT
            {select_list},
            round(bbox.ymin, 6) AS lat,
            round(bbox.xmin, 6) AS lon,
            {marker_expr} AS {recreation.MARKER_COLUMN}
        FROM {from_source}
        WHERE {" AND ".join(filters)}
        ORDER BY {order_by}
        LIMIT 1
    """


def _run_place_details_query(from_source: str, filters: list[str], order_by: str,
                              params: dict, missing: set[str],
                              has_recreation: bool = False,
                              with_clause: str = "") -> tuple | None:
    sql = _place_details_sql(from_source, filters, order_by, missing,
                             has_recreation, with_clause)
    try:
        with _conn_lock:
            return _conn().execute(sql, params).fetchone()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e


def _place_details_by_id(id: str, near_lat: float | None, near_lon: float | None,
                          upstream: str, missing: set[str],
                          bound_to_hint: bool = False) -> tuple | None:
    """Resolve a GERS id, cheapest source first (issue #41).

    1. Whatever tiles the local cache already has on disk — no upstream
       contact at all. Covers the common agent flow (find_places, whose
       results already warmed the relevant tiles, followed by
       place_details on one of those results).
    2. If a location hint was given, upstream constrained by a 50km bbox
       around it — row-group pruning applies, and a hit here also
       materializes/reuses the matching cache tile(s) via _from_source.
    3. Full-dataset scan, last resort — logged as a warning so an agent
       calling place_details(id=...) without a hint (or with a hint that
       missed) is visible in the logs as the slow path it is.

    bound_to_hint stops at step 2: when a hint was given and missed, return
    None instead of scanning the whole dataset. Off by default, so
    place_details' own contract is unchanged; gers.py opts in because it
    probes several themes and a hint-miss there would cost one unbounded
    scan per theme (see gers.py's module docstring).

    The recreation layer joins steps 1 and 3 from local base-theme tiles or
    a pinned dataset only (recreation._from_source): those steps have no
    bbox, and a live base-theme glob with nothing to prune by would be a
    planet-scale read per lookup. A base-theme id therefore resolves via
    warm tiles or a near_lat/near_lon hint (step 2, bounded, which also
    materializes the tiles that make the next lookup warm) — not by
    unbounded scan.
    """
    if cache.enabled():
        tile_paths = cache.claimed_tile_paths(release.resolve_release(), THEME, upstream)
        if tile_paths:
            joined = ", ".join(f"'{p}'" for p in tile_paths)
            source, active = _with_recreation(f"read_parquet([{joined}])", None)
            row = _run_place_details_query(
                source, ["id = $id"], "1", {"id": id}, missing, active,
            )
            if row is not None:
                return row

    if near_lat is not None and near_lon is not None:
        xmin, ymin, xmax, ymax = _bbox_around(near_lat, near_lon, ID_HINT_RADIUS_M)
        bbox_filter, bbox_params = _bbox_filter_sql(xmin, ymin, xmax, ymax)
        params = {"id": id, **bbox_params}
        from_source, active = _places_source((xmin, ymin, xmax, ymax))
        row = _run_place_details_query(from_source, ["id = $id", bbox_filter], "1",
                                       params, missing, active)
        if row is not None:
            return row
        if bound_to_hint:
            logger.info(
                "place_details(id=%s): near-hint missed and the lookup is hint-bounded; "
                "not falling back to a full-dataset scan", id,
            )
            return None

    logger.warning(
        "place_details(id=%s) fell back to a full-dataset scan (no cache hit, "
        "no near_lat/near_lon hint given, or the hint missed) — issue #41", id,
    )
    source, active = _with_recreation(f"read_parquet('{upstream}', hive_partitioning=1)", None)
    return _run_place_details_query(source, ["id = $id"], "1", {"id": id}, missing, active)


def place_details(
    id: str | None = None,
    name: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = DEFAULT_DETAILS_RADIUS_M,
    near_lat: float | None = None,
    near_lon: float | None = None,
    bound_to_hint: bool = False,
) -> dict | None:
    """One place, in full: resolved by GERS id, or by name + a nearby point.

    Exactly one resolution strategy applies: pass id, or pass name together
    with lat/lon (nearest name match within radius_m wins). Raises ValueError
    if neither is given. Returns None if no place matches — callers (the
    server tool) turn that into a structured {"error": "not_found", ...}.

    near_lat/near_lon (issue #41) are an optional location hint for the id
    path only — pass the lat/lon of the row the id came from (e.g. a
    find_places result) to constrain the id lookup with a 50km bbox
    prefilter instead of scanning the whole dataset. Ignored when id isn't
    given. Bare id lookups (no hint) still work exactly as before; they just
    check the local tile cache first and fall back to a full scan, logged,
    if that misses too. bound_to_hint=True drops that last fallback, so a
    hint that misses returns None rather than scanning the dataset — see
    _place_details_by_id.

    Raises SchemaDegraded if bbox is missing (needed for the name+point
    path; an id lookup doesn't strictly need it, but the schema probe
    doesn't distinguish the two calls) or UpstreamUnavailable if the remote
    scan fails after retries. Non-essential columns (addresses, websites,
    phones, socials, brand, sources, confidence, ...) missing from the
    dataset come back as None — see degraded_fields().
    """
    if not id and not (name and lat is not None and lon is not None):
        raise ValueError("place_details requires id, or name together with lat and lon")

    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))

    if id:
        row = _place_details_by_id(id, near_lat, near_lon, upstream, missing, bound_to_hint)
    else:
        bbox_filter, distance_filter, params, bbox, _radius_m = area_geometry(
            lat, lon, radius_m
        )
        filters = [bbox_filter, distance_filter]
        if "names" not in missing:
            filters.append("names.primary ILIKE $name ESCAPE '\\'")
            params["name"] = f"%{_like_escape(name)}%"
        from_source, has_recreation = _places_source(bbox)
        # Deduped like find_places: when the same real-world place matched in
        # both themes, details should come from the richer places row, not
        # from whichever polygon centroid happened to sit closer.
        with_clause, from_clause, filters = _dedup_rows_sql(from_source, filters, has_recreation)
        row = _run_place_details_query(from_clause, filters, _DISTANCE_EXPR, params,
                                       missing, has_recreation, with_clause)

    if row is None:
        return None
    result = dict(zip(_PLACE_DETAIL_RESULT_COLS, row))
    if result.pop(recreation.MARKER_COLUMN):
        # Truthful provenance for a recreation-layer row (docs/RECREATION.md);
        # absent for ordinary places rows, whose theme is the tool's default.
        result["source_theme"] = "base"
    _annotate_place(result)
    for field in _PLACE_DETAIL_LIST_FIELDS:
        kept, omitted = budget.truncate_list(result[field])
        result[field] = kept
        if omitted:
            result[f"{field}_omitted_count"] = omitted
    return result


def within_distance(
    lat: float,
    lon: float,
    max_distance_m: float,
    category: str | None = None,
    name: str | None = None,
) -> dict:
    """Is the nearest matching place within max_distance_m of (lat, lon)?

    category/name narrow the search the same way as find_places — except
    that name stays strictly literal here: #373's alt-spelling/fuzzy
    fallback tiers are disabled (#374), because this tool's yes/no shape
    has nowhere to disclose a spelling correction. The search
    window is capped at max_distance_m * 2 so a "nothing nearby" answer
    doesn't degrade into an unbounded scan — a real nearest match beyond
    that window comes back as nearest: None rather than its true distance.
    Raises the same SchemaDegraded/UpstreamUnavailable as find_places.

    The search radius passed to find_places is itself clamped there to
    geo.MAX_QUERY_RADIUS_M (see area_geometry). When max_distance_m exceeds
    that cap, the search physically cannot reach max_distance_m, so a "no
    match found" outcome does NOT mean "not within max_distance_m" — it only
    means "not within geo.MAX_QUERY_RADIUS_M". In that case within is left
    False (the honest floor: no match was found within what we could
    search) but a "note" is added flagging that this isn't a guaranteed
    negative — see issue #131. A match found at or under max_distance_m is
    still reported as within: True with no caveat.
    """
    search_radius_m = max_distance_m * 2
    # allow_name_fallback=False (#374): this is a yes/no answer with no
    # note surface to carry a spelling correction, so #373's alt-name/fuzzy
    # tiers must not leak in — "is there a Startbucks within 200m" answered
    # from a Starbucks row would be a silent claim about a different name.
    rows = find_places(
        lat, lon, search_radius_m, category=category, name=name, limit=1,
        allow_name_fallback=False,
    )
    if not rows:
        result = {"within": False, "nearest": None, "distance_m": None}
        if max_distance_m > geo.MAX_QUERY_RADIUS_M:
            result["note"] = (
                f"max_distance_m ({max_distance_m}m) exceeds the maximum searchable "
                f"radius ({geo.MAX_QUERY_RADIUS_M}m); no match was found within "
                f"{geo.MAX_QUERY_RADIUS_M}m, but 'within: False' cannot be guaranteed "
                "beyond that radius."
            )
        return result
    nearest = rows[0]
    return {
        "within": nearest["distance_m"] <= max_distance_m,
        "nearest": nearest,
        "distance_m": nearest["distance_m"],
    }


def _count_places_by_category(
    lat: float, lon: float, radius_m: float, categories: list[str]
) -> dict[str, int] | None:
    """Counts of places within radius_m of (lat, lon), one per category.

    One scan per area for all counted categories at once via
    count(*) FILTER (issue #354) rather than one full parquet scan per
    (area x category). Category matching is exact / hierarchy via
    _place_categories_or_filter, never substring ILIKE — "park" must not
    count parking garages, "bank" must not count food banks. Unnamed
    places are counted (no _name_filter) so these counts describe the
    same population as summarize_area's category_counts in the same
    compare_areas response.

    Returns None when the category predicates can't be built at all (both
    basic_category and taxonomy degraded/missing) — an unfiltered count
    of every place in the radius would fabricate a confident verdict.
    """
    if not categories:
        return {}
    upstream = _upstream_glob()
    missing = set(_check_schema(upstream))
    bbox_filter, distance_filter, params, bbox, _effective_radius_m = area_geometry(
        lat, lon, radius_m
    )
    from_source, has_recreation = _places_source(bbox)
    filters = [bbox_filter, distance_filter]
    predicates = []
    for j, category in enumerate(categories):
        pred = _place_categories_or_filter(missing, [category], params, prefix=f"p{j}cat")
        if not pred:
            return None
        predicates.append(pred[0])
    with_clause, from_clause, filters = _dedup_rows_sql(from_source, filters, has_recreation)
    select_list = ", ".join(
        f"count(*) FILTER (WHERE {pred}) AS c{j}" for j, pred in enumerate(predicates)
    )
    sql = f"""
        {with_clause}
        SELECT {select_list} FROM {from_clause}
        WHERE {" AND ".join(filters)}
    """
    try:
        with _conn_lock:
            row = _conn().execute(sql, params).fetchone()
    except duckdb.Error as e:
        raise UpstreamUnavailable(str(e)) from e
    return {c: (row[j] if row else 0) for j, c in enumerate(categories)}


# Special priority category value: measure overall place density instead of
# a single category's count — a foot-traffic proxy (see _build_verdict's
# measured_note), not a measurement of actual foot traffic.
DENSITY_PRIORITY_CATEGORY = "__density__"


def _extreme_idx(values: list[float], pick_max: bool) -> int | None:
    """Index of the single max (or min) value, or None if it's tied."""
    target = max(values) if pick_max else min(values)
    idxs = [i for i, v in enumerate(values) if v == target]
    return idxs[0] if len(idxs) == 1 else None


def _priority_reason(label: str, prefer: str, measures: list[float], winner_idx: int | None) -> str:
    measures_str = ", ".join(f"area {i}={m:g}" for i, m in enumerate(measures))
    if winner_idx is None:
        return f"{label}: {measures_str} — tied, no winner for this criterion."
    verb = "more" if prefer == "more" else "fewer"
    return f"{label}: {measures_str} — area {winner_idx} wins ({verb} is better)."


def _build_verdict(
    areas: list[tuple[float, float]],
    per_area: list[dict],
    radius_m: float,
    priorities: list[dict],
) -> dict:
    """Weighted verdict across priorities (issue #304).

    Per priority: raw measure = category count (or density for
    "__density__") within radius_m; per-priority winner is whoever's
    better on that raw measure, ties get no winner. Per-area score = sum
    of weight * normalized share against the best area — measure/max for
    prefer="more", min/measure for prefer="fewer" — so the best area on a
    priority always gets share 1.0; when every area measures 0 all shares
    are 1.0 (equally tied). Overall winner is the highest score; a tie
    leaves winner_idx null.

    Category counts are made honestly or not at all: when the dataset's
    category columns are all degraded (see _count_places_by_category) a
    count-based priority can't be measured, and the verdict comes back
    with winner_idx/scores null and "degraded": True instead of scoring
    every place in the radius as if it matched (issue #354).
    """
    n = len(areas)
    count_categories = list(dict.fromkeys(
        p["category"] for p in priorities if p["category"] != DENSITY_PRIORITY_CATEGORY
    ))
    counts_per_area = [
        _count_places_by_category(lat, lon, radius_m, count_categories)
        for lat, lon in areas
    ]
    if any(counts is None for counts in counts_per_area):
        return {
            "winner_idx": None,
            "scores": None,
            "reasons": [],
            "degraded": True,
            "measured_note": (
                "The active dataset's category columns are degraded/missing, "
                "so the count-based priorities cannot be measured and no "
                "verdict was scored — an unfiltered place count would have "
                "fabricated one."
            ),
        }
    reasons = []
    totals = [0.0] * n
    for p in priorities:
        label, category, prefer, weight = p["label"], p["category"], p["prefer"], p["weight"]
        if category == DENSITY_PRIORITY_CATEGORY:
            measures = [per_area[i]["density_per_km2"] for i in range(n)]
        else:
            measures = [counts_per_area[i][category] for i in range(n)]
        winner_idx = _extreme_idx(measures, pick_max=(prefer == "more"))
        if prefer == "more":
            max_val = max(measures)
            shares = [1.0 if max_val == 0 else m / max_val for m in measures]
        else:
            min_val = min(measures)
            shares = [1.0 if m == min_val else min_val / m for m in measures]
        for i in range(n):
            totals[i] += weight * shares[i]
        reasons.append(_priority_reason(label, prefer, measures, winner_idx))

    scores = [round(t, 3) for t in totals]
    verdict = {
        "winner_idx": _extreme_idx(scores, pick_max=True),
        "scores": scores,
        "reasons": reasons,
        "measured_note": (
            "Priority scores are built from open-data place counts (or place "
            'density for "__density__") near each area\'s center — not '
            "revenue, rent, actual foot traffic, or demographics. "
            '"__density__" is a foot-traffic proxy, not a measurement of '
            "real foot traffic."
        ),
    }
    if n > 1:
        top_two = sorted(scores, reverse=True)[:2]
        verdict["margin"] = round(top_two[0] - top_two[1], 3)
    return verdict


def compare_areas(
    areas: list[tuple[float, float]],
    radius_m: float = 1000,
    priorities: list[dict] | None = None,
) -> dict:
    """Side-by-side category mix for 2-5 area centers sharing one radius_m.

    Reuses summarize_area per area, then aligns counts across areas for the
    top ~10 categories by combined count, adds place density per km^2 per
    area, and flags the categories with the largest relative difference
    between areas ("differentiators") — the most useful signal for "how is
    area A different from area B."

    priorities (issue #304), if given, is a list of already-validated
    {"label", "category", "prefer": "more"|"fewer", "weight"} dicts (see
    server.compare_areas for the input contract and validation) and adds a
    "verdict" key scoring the areas against them — see _build_verdict.

    Raises ValueError if areas isn't 2-5 centers. Propagates
    SchemaDegraded/UpstreamUnavailable from the first area whose
    summarize_area call fails — a partial comparison isn't returned.
    """
    if not 2 <= len(areas) <= 5:
        raise ValueError("compare_areas takes between 2 and 5 area centers")

    summaries = [summarize_area(lat, lon, radius_m) for lat, lon in areas]

    combined_counts: dict[str, int] = {}
    for s in summaries:
        for row in s["top_categories"]:
            cat, n = row["category"], row["count"]
            combined_counts[cat] = combined_counts.get(cat, 0) + n
    top_categories = sorted(combined_counts, key=lambda c: combined_counts[c], reverse=True)[:10]

    effective_radius_m = geo.clamp_radius_m(radius_m)
    area_km2 = math.pi * (effective_radius_m / 1000) ** 2
    per_area = []
    for (lat, lon), s in zip(areas, summaries):
        counts = {row["category"]: row["count"] for row in s["top_categories"]}
        per_area.append({
            "center": {"lat": lat, "lon": lon},
            "total_places": s["total_places"],
            "density_per_km2": round(s["total_places"] / area_km2, 2) if area_km2 else 0,
            "category_counts": {c: counts.get(c, 0) for c in top_categories},
        })

    differentiators = []
    for c in top_categories:
        values = [a["category_counts"][c] for a in per_area]
        lo, hi = min(values), max(values)
        differentiators.append({
            "category": c,
            "min_count": lo,
            "max_count": hi,
            "relative_difference": round((hi - lo) / hi, 2) if hi else 0,
        })
    differentiators.sort(key=lambda d: d["relative_difference"], reverse=True)

    result = {
        "areas": per_area,
        "categories": top_categories,
        "differentiators": differentiators,
    }
    if priorities:
        result["verdict"] = _build_verdict(areas, per_area, effective_radius_m, priorities)
    return result
