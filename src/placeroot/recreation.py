"""Recreation places read live from Overture's base theme.

Overture's *places* theme is substantially derived from business listings.
That is the right provenance for "the nearest pharmacy" and the wrong one
for the places a family goes on a Saturday: a playground has no storefront,
a beach has no owner to claim it, a neighbourhood park has no phone number.
Those categories exist in the places theme but under-count badly.

They are not missing from Overture, though — they are in a *different
theme*. Overture's `base` theme is a direct conflation of OpenStreetMap,
not of listings, and it carries exactly these features as polygons:
`type=land_use` has class `playground`, `park`, `dog_park`,
`recreation_ground`, `nature_reserve`; `type=land` has class `beach`.
Measured against the 2026-07-22.0 release over a box covering New York City
(-74.05,40.55 to -73.70,40.85):

    playgrounds in theme=places .................  674
    playgrounds in theme=base/type=land_use ..... 1552
    of those, with no places playground within 150 m .. 1013

So this layer is a *2.5x* increase in playground coverage, and PlaceRoot
already knows how to read it: land_use.py and infrastructure.py query these
same datasets, keylessly, over the same DuckDB + S3 GeoParquet path as
every other theme. Nothing is downloaded, nothing is built, nothing is
hosted, and the rows are always exactly as current as the pinned release —
the layer is a `UNION ALL` of one more S3 scan onto the places query, not a
copy of anything.

This is deliberately *not* a live call to the Overpass API or a locally
built OSM extract. Both were tried and rejected:

  - **Overpass** is a JSON HTTP API, not a queryable columnar store. Putting
    it on the query path makes every places answer depend on a volunteer-run
    service with no SLA and aggressive rate limits, which CONTRIBUTING.md's
    design rule #3 (nothing on the critical path depends on someone else's
    quota) rules out.
  - **Raw OSM in Parquet** exists publicly — Meta's Daylight distribution
    publishes it at `s3://daylight-openstreetmap/parquet/osm_features/`, no
    credentials needed — but it is ordered by OSM element id, not spatially.
    Its row-group statistics for `min_lon`/`max_lon` span (-180, 180) in
    every group, so a bbox predicate prunes *nothing* and a "playgrounds
    near me" query degrades to a full scan of 155 GB of ways. Overture's
    base theme is the same OSM data, already spatially sorted and
    bbox-pruned, which is the whole reason it is queryable interactively.

## What the layer does and does not change

**On by default.** `PLACEROOT_RECREATION_LAYER=0` (or `false`/`no`/`off`)
turns it back off; anything else, including leaving the variable unset,
leaves it on. The variable is an opt-*out*.

The cost it buys the coverage with is one extra dataset scan per places
query. That is not free, and an install that cares more about `find_places`
latency than about playgrounds should set the variable to `0`. It goes
through the same tile cache as everything else, under land_use.py's
existing `base_<type>` keys, so the second and later queries over an area
are served locally.

It also adds a failure surface: a places query now depends on the base
theme being readable as well as the places theme. A base type whose schema
drifted is dropped and logged (see union_branches) rather than failing the
query, but an unreachable dataset fails the way an unreachable places
dataset already does. That is the trade default-on makes.

Rows arrive with real GERS ids (base-theme features are GERS-registered
like any other Overture feature), so a row composes with `gers_lookup` and
`place_details` unchanged, and `sources` carries Overture's own OSM
provenance per row. There is no separate licence question to answer: this
is the same Overture release under the same terms PlaceRoot already
publishes in docs/DATA-LICENSE.md.

Three honest limitations, all documented in docs/RECREATION.md rather than
papered over:

  - **No confidence, no operating_status.** Base-theme features carry
    neither. A `min_confidence=` or `operating_status=` filter therefore
    excludes every row from this layer — the filter is doing what it says,
    but the caller should know the layer drops out of those queries.
    Synthesising a confidence value would be inventing a number.
  - **Most are unnamed.** 77% of base-theme playgrounds have no name (a
    playground usually isn't named at all). `find_places` requires a
    non-null name for places rows; that filter is relaxed *for this layer
    only*, so a nameless playground 120 m away is a returned answer with
    `name: null` rather than a silently dropped one. No name is
    synthesised from the category — a fabricated "Playground" is not the
    place's name.
  - **A polygon is reported at its centre.** These are areas, not points.
    Each row's position is the centre of its bbox, so `distance_m` is the
    distance to the middle of the park, not to its nearest edge — you can
    be standing inside a large park it reports as 400 m away. That matches
    how places rows behave (a point per feature) and is the same
    approximation buildings.py accepts; infrastructure.py's
    closest-point-on-geometry treatment is the alternative, and belongs in
    a follow-up that applies it to the whole places path rather than to
    half of one union.

## Why only these classes

The map below is deliberately short. It carries the destinations whose
provenance business listings genuinely get wrong, and stops there.
`pitch` (5,513 rows in the NYC box), `track`, and the eleven golf
sub-features (`bunker`, `fairway`, `green`, `tee`, ...) are *parts of*
facilities rather than destinations, and unioning them in would bury the
playgrounds under football-pitch polygons. `garden` is excluded for the
same reason at larger scale: it is OSM's `leisure=garden`, which is mostly
someone's front yard — 4,908 rows in Manhattan alone. Marinas, stadiums,
zoos and golf courses are excluded because they *are* businesses, and the
places theme already covers them well.
"""

import functools
import logging
import os

import duckdb

from placeroot import cache, db, release

logger = logging.getLogger(__name__)

ENV_VAR = "PLACEROOT_RECREATION_LAYER"

THEME = "base"

# Base-theme class -> Overture category slug. Slugs are the same ones
# search_categories answers from (src/placeroot/data/overture_categories.csv),
# so find_places(category="playground") reaches these rows through the
# ordinary taxonomy path with no special case. See the module docstring for
# why the list is this short.
LAND_USE_CLASSES = {
    "playground": "playground",
    "dog_park": "dog_park",
    "park": "park",
    "village_green": "park",
    "recreation_ground": "park",
    "nature_reserve": "nature_reserve",
    "national_park": "nature_reserve",
    "state_park": "nature_reserve",
    "wilderness_area": "nature_reserve",
    "protected_landscape_seascape": "nature_reserve",
}

LAND_CLASSES = {
    "beach": "beach",
}

# type -> {base class: category slug}, in the order the union appends them.
SOURCES: dict[str, dict[str, str]] = {
    "land_use": LAND_USE_CLASSES,
    "land": LAND_CLASSES,
}

# Every category slug this layer can produce, for docs and data_version.
CATEGORIES = sorted({slug for m in SOURCES.values() for slug in m.values()})

# A base-theme feature within this distance of a places row carrying the same
# category is treated as the same real-world place, and the places row (the
# richer one: confidence, addresses, the listing's own name) wins. 150 m is
# the radius the coverage measurements in the module docstring were taken
# at: of 1,552 base-theme playgrounds in the NYC box, the 539 with a places
# playground inside 150 m are the both-themes duplicates this suppresses.
DEDUP_RADIUS_M = 150


def type_for_category(slug: str | None) -> str | None:
    """Which base-theme type produces rows carrying this category slug.

    For gers.py: a place_details row marked source_theme=base resolves its
    Overture type (land for beaches, land_use for everything else in the
    map) from the same tables the projection was built from.
    """
    for type_, class_map in SOURCES.items():
        if slug in class_map.values():
            return type_
    return None

# Marker column distinguishing this layer's rows from Overture places rows
# inside the union. find_places reads it to relax its "named places only"
# filter for these rows (see the module docstring). overture._with_recreation
# spells it FALSE on the places branch rather than letting UNION ALL BY NAME
# fill NULL, so every call site can treat it as a plain boolean.
MARKER_COLUMN = "is_recreation_layer"

# Columns the projection reads from a base-theme dataset. bbox is what the
# distance math runs on and class is what the mapping keys off, so both are
# essential: without them there is no row to build. names/sources/id degrade
# to NULL the way every other theme's optional columns do.
REQUIRED_COLUMNS = ["id", "bbox", "class", "names", "sources"]
ESSENTIAL_COLUMNS = {"bbox", "class"}

# Test-side override, mirroring overture._data_path_overrides. Sentinel
# rather than None so a test can force the layer *off* again.
_UNSET = object()
_enabled_override = _UNSET

# Env values that turn the layer *off*. The layer is on by default, so this
# variable is an opt-out and anything not in this set — including an unset
# variable and one a shell exported empty — leaves the layer on. An empty
# value reading as "on" is deliberate: `FOO= placeroot` is a shell accident,
# not a considered "no", and the safe reading of an accident is the default.
_FALSEY = {"0", "false", "no", "off"}


def enabled() -> bool:
    """Whether the recreation layer is active for this process. Default True."""
    if _enabled_override is not _UNSET:
        return bool(_enabled_override)
    return os.environ.get(ENV_VAR, "").strip().lower() not in _FALSEY


def set_enabled(value: bool | None) -> None:
    """Force the layer on/off regardless of the env var (tests).

    Pass None to restore the default (read ENV_VAR), mirroring
    overture.set_data_path's None-restores-the-default contract.
    """
    global _enabled_override
    _enabled_override = _UNSET if value is None else bool(value)


def _upstream_glob(type_: str) -> str:
    """The base-theme dataset for type_, honouring test data-path overrides."""
    from placeroot import overture

    return overture._upstream_glob(THEME, type_=type_)


def _cache_theme(type_: str) -> str:
    """Composite tile-cache key, matching land_use.py's "base_<type>" convention.

    Sharing land_use.py's key on purpose: it is the same release, the same
    dataset and the same schema, so a tile either module materialises is
    directly reusable by the other. Underscore rather than ':' because
    cache.tile_path uses the string verbatim as a directory component.
    """
    return f"{THEME}_{type_}"


def _from_source(
    bbox: tuple[float, float, float, float] | None, type_: str,
    upstream_readable: bool = True,
) -> str | None:
    """FROM-clause source for one base-theme type, or None to skip the branch.

    Delegates to cache.source_sql — the shared cached-tiles-else-upstream
    resolution — with two policies of its own. For an *unbounded* lookup
    (bbox None: place_details resolving an id with no location hint,
    geocode's name-only fallback) the branch is served from tiles already
    on disk or a pinned local dataset, and otherwise skipped: falling back
    to the live glob there would be a scan of the whole base theme with no
    bbox to prune row groups by — a planet-scale read to answer one id —
    so the layer degrades to places-only for that one query instead. And
    when the upstream itself is unreadable (upstream_readable=False, from
    _branch_missing's probe) there is no upstream fallback at all: a query
    whose box the cached tiles don't cover skips the branch rather than
    handing the scan a glob known to fail — which would take the whole
    places query down with it.
    """
    from placeroot import overture

    upstream = _upstream_glob(type_)
    fallback_ok = upstream_readable and (
        bbox is not None or overture.dataset_is_pinned(THEME, type_)
    )
    try:
        return cache.source_sql(
            _cache_theme(type_), upstream, bbox, upstream_fallback=fallback_ok
        )
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e


@functools.cache
def _case_exprs(type_: str) -> tuple[str, str]:
    """(taxonomy CASE, basic_category CASE) for one base type, built once.

    Both fragments derive only from the static class maps and the bundled
    taxonomy CSV — process constants — while _projection runs per query;
    without this cache every places query re-walks the ~2,000-row taxonomy
    list ~22 times to rebuild two identical strings.
    """
    class_map = SOURCES[type_]
    return _taxonomy_expr(class_map), _basic_category_expr(class_map)


def _taxonomy_expr(class_map: dict[str, str]) -> str:
    """CASE expression building a places-shaped `taxonomy` struct from `class`.

    The struct must match theme=places' own column type field-for-field
    (primary VARCHAR, hierarchy VARCHAR[], alternates VARCHAR[]) or the
    UNION ALL BY NAME cannot line the two branches up. hierarchy comes from
    the bundled taxonomy CSV rather than a second hand-written table, so
    these rows and search_categories can never disagree about where a slug
    sits in the tree.
    """
    from placeroot import categories

    arms = []
    for class_, slug in class_map.items():
        hierarchy = categories.hierarchy_for(slug) or [slug]
        hierarchy_sql = ", ".join(db._sql_str(h) for h in hierarchy)
        arms.append(
            f"WHEN {db._sql_str(class_)} THEN "
            f"{{'primary': {db._sql_str(slug)}, "
            f"'hierarchy': [{hierarchy_sql}]::VARCHAR[], "
            f"'alternates': NULL::VARCHAR[]}}"
        )
    return "CASE class " + " ".join(arms) + " END"


def _basic_category_expr(class_map: dict[str, str]) -> str:
    """CASE expression filling `basic_category` (the taxonomy's root branch).

    Same source as _taxonomy_expr's hierarchy, so a category search that
    matches on basic_category and one that matches on taxonomy.primary find
    the same rows.
    """
    from placeroot import categories

    arms = []
    for class_, slug in class_map.items():
        hierarchy = categories.hierarchy_for(slug)
        branch = hierarchy[0] if hierarchy else slug
        arms.append(f"WHEN {db._sql_str(class_)} THEN {db._sql_str(branch)}")
    return "CASE class " + " ".join(arms) + " END"


def _projection(source: str, type_: str, missing: set[str]) -> str:
    """One base-theme dataset, projected into the places row shape.

    Only the columns the places tools actually read are projected; UNION ALL
    BY NAME fills every other places column (confidence, operating_status,
    addresses, websites, phones, socials, brand) with NULL, which is the
    truthful value — a base-theme polygon has none of them.

    bbox is collapsed to the polygon's centre point on both axes. Every
    places tool reads position and distance off bbox.ymin/bbox.xmin (see
    overture.DISTANCE_EXPR), which for an un-collapsed polygon is its
    south-west corner — a park would report the distance to its corner and
    plot at its corner. Collapsing to the centre makes the row behave
    exactly like a point feature, at the cost documented in the module
    docstring.
    """
    id_expr = "NULL" if "id" in missing else "id"
    names_expr = "NULL" if "names" in missing else "names"
    sources_expr = "NULL" if "sources" in missing else "sources"
    class_list = ", ".join(db._sql_str(c) for c in SOURCES[type_])
    taxonomy_expr, basic_category_expr = _case_exprs(type_)
    return f"""SELECT
            {id_expr} AS id,
            {names_expr} AS names,
            {sources_expr} AS sources,
            {{'xmin': (bbox.xmin + bbox.xmax) / 2,
              'xmax': (bbox.xmin + bbox.xmax) / 2,
              'ymin': (bbox.ymin + bbox.ymax) / 2,
              'ymax': (bbox.ymin + bbox.ymax) / 2}} AS bbox,
            {taxonomy_expr} AS taxonomy,
            {basic_category_expr} AS basic_category,
            TRUE AS {MARKER_COLUMN}
        FROM {source}
        WHERE class IN ({class_list})"""


def union_branches(bbox: tuple[float, float, float, float] | None) -> list[str]:
    """Projected SELECTs to union onto a places query, one per base type.

    bbox bounds the tile-cache lookup; pass None for an unbounded id
    lookup (see _from_source).

    Empty when the layer is off, or when a base-theme dataset is missing a
    column the projection cannot do without — a base theme whose schema
    drifted out from under us drops that branch and logs it, rather than
    failing the places query that would otherwise have answered fine from
    Overture alone. Degrading rather than failing is the right call here
    specifically because the layer reads a *live* dataset: a missing column
    means Overture changed shape, not that anything local is broken, the
    places answer is still complete for the places theme, and taking the
    whole tool down over a supplementary layer would be the worse failure.
    data_version reports the dropped type so the gap is visible.
    """
    if not enabled():
        return []

    branches = []
    for type_ in SOURCES:
        if _reaches_past_a_pinned_deployment(type_):
            continue
        missing, upstream_readable = _branch_missing(type_)
        if missing is None:
            continue
        source = _from_source(bbox, type_, upstream_readable)
        if source is None:
            # Nothing local can serve this branch for this query (an
            # unbounded lookup, or an unreadable upstream whose tiles don't
            # cover the box) — see _from_source. Not a degradation of the
            # layer, just of this one query.
            continue
        branches.append(_projection(source, type_, missing))
    return branches


# Warn once per (reason, type, glob) for a condition that holds for every
# query in a degraded deployment and would otherwise log on each one,
# forever. Keyed by glob so re-pointing the upstream (the fix) warns afresh
# if the new target is broken too, and by reason so a dataset that heals
# from one failure mode into a different one still gets that mode's warning.
_warned_once: set[tuple[str, str, str]] = set()


def _warn_once(reason: str, type_: str, glob: str, message: str, *args) -> None:
    key = (reason, type_, glob)
    if key in _warned_once:
        return
    _warned_once.add(key)
    logger.warning(message, *args)


def _branch_missing(type_: str) -> tuple[set[str] | None, bool]:
    """(missing REQUIRED_COLUMNS or None, whether upstream itself is readable).

    A None first element means the branch must be dropped: the dataset is
    missing an essential column (schema drift), or its schema cannot be
    probed at all and no cached tiles exist to serve instead. The
    unreadable case is what a partial mirror looks like —
    PLACEROOT_UPSTREAM_BASE pointing at a bucket that carries theme=places
    but not theme=base resolves this layer's globs to paths that do not
    exist, and before this check that read "assume nothing missing"
    (missing_columns' contract) and failed every places query at scan time.

    Cached tiles still count as *servable* — their schema matched the
    dataset when they were written, which keeps the layer answering from
    cache through an upstream outage the same way the places theme does —
    but the second element stays False so _from_source knows never to fall
    back to (or materialize from) the unreadable glob for a box the tiles
    don't cover. That partial state is warned about and reported in
    degraded_types: some queries answering from tiles doesn't make the
    silent gaps outside them acceptable.

    A failed probe is memoized briefly at the db layer
    (db.PROBE_FAILURE_RETRY_S), so a degraded deployment pays the probe's
    network timeout about once a minute rather than per query and heals
    within a minute of its upstream; each warning here fires once per
    (reason, type, glob).
    """
    from placeroot import overture

    glob = _upstream_glob(type_)
    present = overture.probe_schema(glob)
    if present is None:
        tiles = (
            cache.cached_tile_paths(release.resolve_release(), _cache_theme(type_), glob)
            if cache.enabled() else []
        )
        if not tiles:
            _warn_once(
                "unreadable", type_, glob,
                "recreation layer: theme=%s/type=%s is unreadable at %s and nothing is "
                "cached for it — skipping that branch; places results will be "
                "Overture-places-only for its categories. A mirror that carries only "
                "theme=places causes this: mirror theme=base too (scripts/mirror_theme.py) "
                "or set %s=0.",
                THEME, type_, glob, ENV_VAR,
            )
            return None, False
        # The tiles' own schema decides `missing`, not an assumption that
        # they carry every REQUIRED column: tiles materialized while the
        # dataset lacked a non-essential column (missing={'sources'}, say)
        # must project NULL for it, not reference a column that isn't there
        # and fail the query. All tiles share a fingerprint dir, so probing
        # one (a local file; the probe caches successes) speaks for all —
        # claimed first so eviction can't delete it mid-probe, and only
        # that one, so a mere schema check doesn't shield the whole theme
        # from eviction. Walk the list until one claims: the listing takes
        # no claims, so any individual tile may vanish to eviction between
        # the listing and here without the rest becoming unservable.
        tile_schema = None
        for tile in tiles:
            claimed = cache.claim_existing_paths([tile])
            if not claimed:
                continue
            tile_schema = overture.probe_schema(str(claimed[0]))
            if tile_schema is not None:
                break
            # The probe itself failed — another process can evict a tile
            # between our claim and the read (claims are per-process). The
            # next tile may still serve; only give up when none does.
        missing = (
            {c for c in REQUIRED_COLUMNS if c not in tile_schema}
            if tile_schema is not None else set(REQUIRED_COLUMNS)
        )
        if tile_schema is None or missing & ESSENTIAL_COLUMNS:
            _warn_once(
                "unreadable-tiles-unusable", type_, glob,
                "recreation layer: theme=%s/type=%s is unreadable at %s and its "
                "cached tiles cannot serve either (unreadable tile, or a schema "
                "missing an essential column) — skipping that branch; places "
                "results will be Overture-places-only for its categories.",
                THEME, type_, glob,
            )
            return None, False
        _warn_once(
            "unreadable-cached", type_, glob,
            "recreation layer: theme=%s/type=%s is unreadable at %s — serving "
            "that branch from already-cached tiles only; queries outside their "
            "coverage will be Overture-places-only for its categories. A mirror "
            "that carries only theme=places causes this: mirror theme=base too "
            "(scripts/mirror_theme.py) or set %s=0.",
            THEME, type_, glob, ENV_VAR,
        )
        return missing, False
    missing = {c for c in REQUIRED_COLUMNS if c not in present}
    essential_missing = sorted(missing & ESSENTIAL_COLUMNS)
    if essential_missing:
        _warn_once(
            "schema-drift", type_, glob,
            "recreation layer: theme=%s/type=%s is missing %s — skipping that branch; "
            "places results will be Overture-places-only for its categories",
            THEME, type_, ", ".join(essential_missing),
        )
        return None, True
    return missing, True


def _reaches_past_a_pinned_deployment(type_: str) -> bool:
    """Whether reading this base type would ignore the deployment's own config.

    The layer follows the places dataset. If an operator (or the offline
    demo, or a mirror deployment) pointed places at a local extract via
    PLACEROOT_DATA_PATH and said nothing about theme=base, resolving base to
    the live S3 release would reach straight past that configuration — and
    a query that was meant to be local and fast becomes a planet-scale
    remote scan. `examples/site_selection/run_demo.py --offline` is exactly
    this shape, and it is how the case was found.

    So: places pinned and this base type not pinned means the branch is
    skipped, with one warning naming the variable that would fix it. Pin
    base too (PLACEROOT_DATA_PATH_BASE, or scripts/mirror_theme.py) and the
    layer comes back.
    """
    from placeroot import overture

    if not overture.dataset_is_pinned(overture.THEME, "place"):
        return False
    if overture.dataset_is_pinned(THEME, type_):
        return False
    _warn_once(
        "pinned", type_, overture._upstream_glob(overture.THEME, "place"),
        "recreation layer: theme=places is pinned to a local dataset but "
        "theme=%s/type=%s is not, so reading it would mean a live S3 scan this "
        "deployment did not ask for — skipping that branch. Set "
        "PLACEROOT_DATA_PATH_BASE to include it, or %s=0 to silence this.",
        THEME, type_, ENV_VAR,
    )
    return True


def degraded_types() -> list[str]:
    """Base-theme types the layer cannot currently read *in full*, for
    data_version. A listed type is contributing nothing (drift, unreadable
    with nothing cached, pinned places without a base pin) or serving from
    cached tiles only while its upstream is unreadable — partial coverage
    with silent gaps outside the tiles, which is exactly what the field
    exists to make visible.

    Empty when the layer is off (nothing is being read, so nothing is
    degraded) or when every branch is healthy.
    """
    if not enabled():
        return []

    degraded = []
    for type_ in SOURCES:
        if _reaches_past_a_pinned_deployment(type_):
            degraded.append(type_)
            continue
        missing, upstream_readable = _branch_missing(type_)
        # An unreadable upstream still serving from cached tiles counts as
        # degraded: queries outside the tiles' coverage silently drop the
        # branch, and data_version is where that has to be visible.
        if missing is None or not upstream_readable:
            degraded.append(type_)
    return degraded
