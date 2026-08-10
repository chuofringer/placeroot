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


def _from_source(bbox: tuple[float, float, float, float] | None, type_: str) -> str:
    """FROM-clause source for one base-theme type: cache tiles, or upstream.

    Mirrors land_use.py's own _from_source (and buildings.py's before it).

    bbox is None for the id-lookup path (place_details resolving a GERS id
    with no location to bound it). There is no box to enumerate tiles for
    there, so it takes whatever tiles are already on disk — the same
    already-paid-for shortcut overture._place_details_by_id takes for the
    places theme — and falls back to the upstream glob. It deliberately
    does *not* materialize new tiles for an unbounded lookup: that would be
    a world-sized fetch to answer one id.
    """
    from placeroot import overture

    upstream = _upstream_glob(type_)
    if cache.enabled():
        try:
            if bbox is None:
                paths = cache.cached_tile_paths(
                    release.resolve_release(), _cache_theme(type_), upstream
                )
            else:
                with db.conn_lock:
                    paths = cache.local_paths_for_query(
                        db.shared_conn(), release.resolve_release(), _cache_theme(type_), bbox,
                        upstream, db.new_connection,
                    )
        except duckdb.Error as e:
            raise overture.UpstreamUnavailable(str(e)) from e
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    return f"read_parquet('{upstream}', hive_partitioning=1)"


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


def _projection(source: str, class_map: dict[str, str], missing: set[str]) -> str:
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
    class_list = ", ".join(db._sql_str(c) for c in class_map)
    return f"""SELECT
            {id_expr} AS id,
            {names_expr} AS names,
            {sources_expr} AS sources,
            {{'xmin': (bbox.xmin + bbox.xmax) / 2,
              'xmax': (bbox.xmin + bbox.xmax) / 2,
              'ymin': (bbox.ymin + bbox.ymax) / 2,
              'ymax': (bbox.ymin + bbox.ymax) / 2}} AS bbox,
            {_taxonomy_expr(class_map)} AS taxonomy,
            {_basic_category_expr(class_map)} AS basic_category,
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
    from placeroot import overture

    branches = []
    for type_, class_map in SOURCES.items():
        if _reaches_past_a_pinned_deployment(type_):
            continue
        glob = _upstream_glob(type_)
        missing = set(overture.missing_columns(glob, REQUIRED_COLUMNS))
        essential_missing = sorted(missing & ESSENTIAL_COLUMNS)
        if essential_missing:
            logger.warning(
                "recreation layer: theme=%s/type=%s is missing %s — skipping that branch; "
                "places results will be Overture-places-only for its categories",
                THEME, type_, ", ".join(essential_missing),
            )
            continue
        branches.append(_projection(_from_source(bbox, type_), class_map, missing))
    return branches


# One warning per (type, places dataset) pair, not one per query: a pinned
# deployment would otherwise log this on every find_places call forever.
_pinned_warned: set[tuple[str, str]] = set()


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
    key = (type_, overture._upstream_glob(overture.THEME, "place"))
    if key not in _pinned_warned:
        _pinned_warned.add(key)
        logger.warning(
            "recreation layer: theme=places is pinned to a local dataset but "
            "theme=%s/type=%s is not, so reading it would mean a live S3 scan this "
            "deployment did not ask for — skipping that branch. Set "
            "PLACEROOT_DATA_PATH_BASE to include it, or %s=0 to silence this.",
            THEME, type_, ENV_VAR,
        )
    return True


def degraded_types() -> list[str]:
    """Base-theme types the layer cannot currently read, for data_version.

    Empty when the layer is off (nothing is being read, so nothing is
    degraded) or when every branch is healthy.
    """
    if not enabled():
        return []
    from placeroot import overture

    degraded = []
    for type_ in SOURCES:
        if _reaches_past_a_pinned_deployment(type_):
            continue
        missing = set(overture.missing_columns(_upstream_glob(type_), REQUIRED_COLUMNS))
        if missing & ESSENTIAL_COLUMNS:
            degraded.append(type_)
    return degraded
