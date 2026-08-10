"""Resolve a GERS id to whichever Overture theme owns it (issue #173).

GERS (Global Entity Reference System) is Overture's stable-id layer: every
place, building, division and segment carries an id that stays the same
across releases, and the same id is the join key between themes. PlaceRoot
already hands GERS ids back from nearly every tool but, until this module,
had no way to take one *back* — an agent holding an id from an earlier turn
could not ask "what is this, and what is it inside?".

gers_lookup() answers exactly that: it probes the themes in likelihood
order (places -> divisions -> buildings), returns the first match as a
compact entity, and hangs the cheap cross-theme joins we can already
compute off "related" (containing division for everything; the building at
the point for places).

Cost, and why the near-hint matters
-----------------------------------
Every probe is an id-equality scan, and an id-equality scan with no spatial
predicate cannot be row-group pruned — the same "slow but works" caveat
place_details' id path documents (issue #41). Worst case here is three of
those, one per theme, for an id that belongs to none of them. Passing
near_lat/near_lon (the lat/lon of the row the id came from) narrows every
probe to a 50km bbox — overture.ID_HINT_RADIUS_M, the same window and the
same reasoning as place_details — which is the difference between a
row-group-pruned read and a full-theme scan. Callers that have a coordinate
should always pass it.

A hint that *misses* does NOT fall back to the unbounded scan. This is a
deliberate departure from place_details' single-theme contract (issue #41),
where one hint-miss costs one extra scan: here a hint-miss would cost one
unbounded scan *per theme*, and the worst of the three is the buildings
glob — billions of rows. So the hint is treated as a boundary, not a
preference: if no theme has the id inside the box, the lookup returns
not_found carrying HINT_MISS_NOTE, which says the search was bounded and
tells the caller to retry without the hint for an exhaustive lookup. The
trade is explicit — a stale or wrong hint turns a resolvable id into a
not_found, and the note is what makes that recoverable rather than a lie.

An *unhinted* lookup still scans each theme unbounded, as before. That is
the exhaustive path and it is genuinely slow; the only protection is the
negative cache below, which makes the *second* lookup of a junk id free.

Negative cache
--------------
An id no theme claims is the expensive case, and agents retry. A small
in-process (release, id) -> miss set, mirroring overture.py's division
geometry miss-cache, means a repeated junk id costs nothing after the
first. Only *exhaustive* (unhinted) misses are cached: a hinted miss only
proves the id isn't in that one box, so caching it would poison the
exhaustive lookup the note just told the caller to make.

The probe order is by likelihood, not cost: places is by far the theme
whose ids agents actually hold (it's what find_places, geocode, resolve_place
and place_details all return), it is the smallest of the three datasets, and
its lookup is the only one that also gets to check the local tile cache
before touching upstream (via overture.place_details). Divisions comes next
— a small dataset, and division ids are the other id an agent commonly
carries (admin_lookup, resolve_area). Buildings is last: it is the largest
theme in Overture by an order of magnitude, so an unhinted scan there is
the most expensive thing this module can do, and it is worth reaching only
after the two cheaper themes have missed.

Not covered in v1: the transportation theme (segments/connectors). Segment
ids resolve against a dataset larger than buildings, the summary fields
worth returning for a segment (class, subclass, road surface, access
restrictions) don't overlap with anything here, and routing.py's own data
path is separate from overture.py's theme override — enough surface that it
belongs in its own pass rather than bolted on as a fourth probe. A segment
id handed to gers_lookup today comes back not_found, which is honest: this
tool genuinely cannot resolve it yet.
"""

import logging
import re
import threading

import duckdb

from placeroot import buildings, db, divisions, geo, overture, release

logger = logging.getLogger(__name__)

# Sanity bound on the id string itself. Real GERS ids are short (32-char
# hex tokens); anything wildly longer is a caller mistake, and rejecting
# it up front beats spending three theme scans discovering it matches
# nothing. Generous enough not to reject a longer id format later.
MAX_ID_LENGTH = 128

# Character gate on the id, deliberately looser than the real format.
# Live GERS ids are 32 lowercase hex characters, and gating on exactly that
# would be the tightest useful check — but the committed test fixtures use
# readable synthetic division ids ("gers-div-brooklyn"), and so does
# scripts/build_geocode_fixture.py which generates them, so a strict hex
# gate would reject the fixtures wholesale. This charset gate is the
# conservative middle: it admits every real GERS id and every fixture id,
# while rejecting the shapes that actually indicate a caller mistake —
# whitespace inside the id, quotes, path separators, glob characters, URLs,
# whole sentences. Those are bad_request; a well-formed id that matches
# nothing stays not_found, which is the distinction that matters.
ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Returned as "note" on a not_found when the caller passed a near-hint —
# see the module docstring on why a hint bounds the search rather than
# merely ordering it.
HINT_MISS_NOTE = (
    "near_lat/near_lon bounded this lookup to a ~50km box around the hint; an id "
    "outside that box is reported as not found without an exhaustive scan. Retry "
    "without near_lat/near_lon to search every theme in full (much slower)."
)

# Exhaustive misses only (see the module docstring). Bounded by entry count:
# the entries are short strings, so a flat cap is enough, and the whole
# thing is dropped rather than LRU-evicted — a negative cache exists to
# absorb retry storms, and rebuilding it costs exactly what not having it
# would have cost anyway.
_MISS_CACHE_MAX_ENTRIES = 4096
_miss_cache: set[tuple[str, str]] = set()
_miss_cache_lock = threading.Lock()


def clear_miss_cache() -> None:
    """Drop every cached not-found id. For tests and hot-reloads."""
    with _miss_cache_lock:
        _miss_cache.clear()


def _miss_cached(id: str) -> bool:
    with _miss_cache_lock:
        return (release.resolve_release(), id) in _miss_cache


def _cache_miss(id: str) -> None:
    with _miss_cache_lock:
        if len(_miss_cache) >= _MISS_CACHE_MAX_ENTRIES:
            _miss_cache.clear()
        _miss_cache.add((release.resolve_release(), id))


# Divisions probe: the type=division rows (the division *entity* — point,
# name, subtype, country/region), not the type=division_area polygon rows.
# That is deliberate: division_area rows are polygon variants (land vs.
# maritime) with their own row ids, while the id every PlaceRoot tool hands
# out for a division is the entity's division_id — see divisions.py's
# admin_lookup, which coalesces to exactly that.
DIVISION_COLUMNS = ["id", "names", "subtype", "country", "region", "bbox"]
BUILDING_COLUMNS = ["id", "geometry", "bbox", "subtype", "class", "height", "num_floors"]

# An id lookup is impossible without an id column; everything else degrades
# to a null field.
_ESSENTIAL = {"id"}


def _validate_id(id: str) -> str:
    """The trimmed id, or ValueError if it could not be a GERS id at all.

    Cheap structural check only — a well-formed id that matches nothing is
    a not_found, not a bad_request, and that distinction is the whole point
    of validating here rather than letting an empty string scan three
    themes and come back empty-handed.
    """
    if not isinstance(id, str):
        raise ValueError(f"id must be a string, got {type(id).__name__}")
    trimmed = id.strip()
    if not trimmed:
        raise ValueError("id must be a non-empty GERS id")
    if len(trimmed) > MAX_ID_LENGTH:
        raise ValueError(f"id is longer than {MAX_ID_LENGTH} characters; that is not a GERS id")
    if not ID_CHARSET_RE.match(trimmed):
        raise ValueError(
            f"{id!r} is not a GERS id: expected an opaque token of letters, digits, "
            "'-' and '_' (a real GERS id is 32 lowercase hex characters, e.g. "
            "'41b764a2b9ea71e97088d733d7f5898c')"
        )
    return trimmed


def _hint_bbox(
    near_lat: float | None, near_lon: float | None
) -> tuple[float, float, float, float] | None:
    """The 50km box around a near-hint, or None when no hint was given."""
    if near_lat is None or near_lon is None:
        return None
    return geo.bbox_around(near_lat, near_lon, overture.ID_HINT_RADIUS_M)


def _bbox_or_degraded(
    theme: str, missing: set[str], near_lat: float | None, near_lon: float | None
) -> tuple[float, float, float, float] | None:
    """The probe's hint bbox — treating a hint that *cannot* bound as degraded.

    The bbox column is what a near-hint prunes on. When the dataset is
    missing it, a hinted probe has no bounded path left: silently running
    the unbounded full-theme scan instead would be exactly the read the
    hint exists to prevent (for buildings, a multi-billion-row one), and it
    would make the hint-miss note a lie ("the search was bounded" when it
    wasn't). So a hinted probe on a bbox-less dataset is SchemaDegraded —
    the caller sees which theme couldn't be checked — while an unhinted
    probe still scans unbounded, which is the exhaustive path anyway.
    """
    if near_lat is None or near_lon is None:
        return None
    if "bbox" in missing:
        logger.warning(
            "gers_lookup: %s dataset has no bbox column; a hinted probe cannot "
            "be bounded, reporting the theme as degraded instead of scanning it in full",
            theme,
        )
        raise overture.SchemaDegraded(["bbox"], dataset=f"{theme} dataset")
    return _hint_bbox(near_lat, near_lon)


def _compact(fields: dict) -> dict:
    """Drop null summary fields — an absent attribute costs nothing to omit."""
    return {k: v for k, v in fields.items() if v is not None}


def _run_id_query(select_sql: str, from_source: str, params: dict, bbox_filter: str | None):
    """One id-equality SELECT, optionally bbox-narrowed. Row tuple or None."""
    filters = ["id = $id"] + ([bbox_filter] if bbox_filter else [])
    sql = f"""
        SELECT {select_sql}
        FROM {from_source}
        WHERE {" AND ".join(filters)}
        LIMIT 1
    """
    try:
        with db.conn_lock:
            return db.shared_conn().execute(sql, params).fetchone()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e


def _lookup_row(
    theme: str, select_sql: str, glob: str, id: str, bbox, from_source_fn=None
):
    """Hint-narrowed id lookup, or the unbounded scan when there is no hint.

    The shared body of the divisions and buildings probes — the same shape
    overture._place_details_by_id(bound_to_hint=True) uses for places, so
    all three themes have one contract (see the module docstring). A bbox
    is a *boundary*: a miss inside it returns None rather than escalating
    to a full-theme scan. from_source_fn lets a theme route the narrowed
    read through its tile cache; the unbounded read is always straight
    upstream, since there is no bbox to resolve tiles from.
    """
    if bbox is not None:
        bbox_filter, bbox_params = geo.bbox_filter_sql(*bbox)
        unbounded = f"read_parquet('{glob}', hive_partitioning=1)"
        source = from_source_fn(bbox) if from_source_fn is not None else unbounded
        row = _run_id_query(select_sql, source, {"id": id, **bbox_params}, bbox_filter)
        if row is None:
            logger.info(
                "gers_lookup(%s): %s near-hint missed; not escalating to a full-theme scan",
                id, theme,
            )
        return row
    return _run_id_query(
        select_sql, f"read_parquet('{glob}', hive_partitioning=1)", {"id": id}, None
    )


# Ids minted by the opt-in supplemental places layer (docs/SUPPLEMENT.md).
# place_details resolves them — it queries the union — but gers_lookup must
# not: a GERS id is a cross-dataset identity claim Overture makes, and these
# are local row keys that mean nothing outside one operator's file.
# _validate_id already rejects them today, since neither ':' nor '/' is in
# ID_CHARSET_RE; this is the explicit statement of the rule, so loosening
# that charset later can't quietly start certifying supplement rows as GERS
# entities.
_SUPPLEMENT_ID_PREFIXES = ("osm:", "imls:")


def _is_supplement_id(id: str) -> bool:
    return id.startswith(_SUPPLEMENT_ID_PREFIXES)


def _probe_places(id: str, near_lat: float | None, near_lon: float | None) -> dict | None:
    """Places probe, delegating wholesale to place_details' id resolution.

    place_details already implements the exact strategy this module wants
    (local tile cache -> hint-constrained upstream -> full scan, logged);
    reimplementing it here would be a second copy that drifts. We just take
    its rich row and keep the handful of fields that belong in a compact
    cross-theme answer. bound_to_hint drops its last stage so this probe
    treats a hint as a boundary like the other two do.
    """
    if _is_supplement_id(id):
        return None
    row = overture.place_details(
        id=id, near_lat=near_lat, near_lon=near_lon, bound_to_hint=True
    )
    if row is None:
        return None
    return {
        "theme": "places",
        "type": "place",
        "name": row.get("name"),
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "summary": _compact({
            "category": row.get("category"),
            "basic_category": row.get("basic_category"),
            "confidence": row.get("confidence"),
            "operating_status": row.get("operating_status"),
            "brand": row.get("brand"),
        }),
    }


def _probe_divisions(id: str, near_lat: float | None, near_lon: float | None) -> dict | None:
    """Divisions probe against theme=divisions/type=division."""
    glob = overture.upstream_glob(theme="divisions", type_="division")
    missing = set(overture.missing_columns(glob, DIVISION_COLUMNS))
    essential_missing = [c for c in missing if c in _ESSENTIAL]
    if essential_missing:
        raise overture.SchemaDegraded(essential_missing)

    bbox = _bbox_or_degraded("divisions", missing, near_lat, near_lon)

    name_expr = "NULL" if "names" in missing else "names.primary"
    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    country_expr = "NULL" if "country" in missing else "country"
    region_expr = "NULL" if "region" in missing else "region"
    if "bbox" in missing:
        lat_expr, lon_expr = "NULL", "NULL"
    else:
        lat_expr, lon_expr = "round(bbox.ymin, 6)", "round(bbox.xmin, 6)"

    select_sql = (
        f"{name_expr} AS name, {subtype_expr} AS subtype, "
        f"{country_expr} AS country, {region_expr} AS region, "
        f"{lat_expr} AS lat, {lon_expr} AS lon"
    )
    row = _lookup_row("divisions", select_sql, glob, id, bbox)
    if row is None:
        return None
    name, subtype, country, region, lat, lon = row
    return {
        "theme": "divisions",
        "type": "division",
        "name": name,
        "lat": lat,
        "lon": lon,
        "summary": _compact({"subtype": subtype, "country": country, "region": region}),
    }


def _probe_buildings(id: str, near_lat: float | None, near_lon: float | None) -> dict | None:
    """Buildings probe — the footprint's centroid stands in for its point.

    Same centroid-not-bbox-corner reasoning as buildings.buildings_at: a
    building's bbox spans its whole footprint, so a corner is a poor
    stand-in for where it is.
    """
    buildings._ensure_spatial()
    glob = buildings._upstream_glob()
    missing = set(overture.missing_columns(glob, BUILDING_COLUMNS))
    essential_missing = [c for c in missing if c in _ESSENTIAL | {"geometry"}]
    if essential_missing:
        raise overture.SchemaDegraded(essential_missing)

    bbox = _bbox_or_degraded("buildings", missing, near_lat, near_lon)

    geom_expr = geo.geom_expr(glob)
    subtype_expr = "NULL" if "subtype" in missing else "subtype"
    class_expr = "NULL" if "class" in missing else "class"
    height_expr = "NULL" if "height" in missing else "height"
    floors_expr = "NULL" if "num_floors" in missing else "num_floors"

    select_sql = (
        f"{subtype_expr} AS subtype, {class_expr} AS class, "
        f"{height_expr} AS height, {floors_expr} AS num_floors, "
        f"round(ST_Y(ST_Centroid({geom_expr})), 6) AS lat, "
        f"round(ST_X(ST_Centroid({geom_expr})), 6) AS lon"
    )
    # A hint also lets the narrowed read go through the local tile cache,
    # the same way buildings_at's bbox-bounded queries do.
    row = _lookup_row(
        "buildings", select_sql, glob, id, bbox, from_source_fn=buildings._from_source
    )
    if row is None:
        return None
    subtype, class_, height, num_floors, lat, lon = row
    return {
        "theme": "buildings",
        "type": "building",
        # Overture building footprints are overwhelmingly unnamed; there is
        # no names column in the required set, so this is honestly null
        # rather than fabricated from the address or the class.
        "name": None,
        "lat": lat,
        "lon": lon,
        "summary": _compact({
            "subtype": subtype,
            "class": class_,
            "height_m": height,
            "num_floors": num_floors,
        }),
    }


# (theme, probe) pairs, in the likelihood order the module docstring
# explains. The theme label rides along so a failed probe can be named in
# the error a caller sees.
_PROBES = (
    ("places", _probe_places),
    ("divisions", _probe_divisions),
    ("buildings", _probe_buildings),
)

# What "related" carries when the join itself broke, as opposed to an empty
# {} meaning "this entity is genuinely in no division". Those two are
# different answers and used to be indistinguishable.
RELATED_UNAVAILABLE_NOTE = "related lookups unavailable (upstream error)"
BUILDING_UNAVAILABLE_NOTE = "related building lookup unavailable (upstream error)"


def _containing_entry(chain: list[dict], self_id: str, theme: str) -> dict | None:
    """The chain entry that *contains* the entity, or None if there is none.

    admin_lookup's chain is smallest-division-first. For a point entity (a
    place or a building) nothing in the chain is the entity itself, so the
    first entry is the smallest division containing it.

    For a division entity the chain's own first entry is usually the entity
    — a division contains its own centre — and its container is the entry
    *after* it, not merely the next one that isn't itself. Taking "the next
    entry that isn't self" is what made a locality report its own
    neighborhood as the division containing it: the neighborhood sorts
    smaller, so it comes first, and skipping only the self entry walked
    downward through the hierarchy instead of upward. If the entity isn't
    in the chain at all (a division whose polygon doesn't cover its own
    reference point, or one absent from the division_area dataset) there is
    no container we can name honestly, and neither is there if it is the
    chain's last (largest) entry.
    """
    if theme != "divisions":
        return chain[0] if chain else None
    for i, entry in enumerate(chain):
        if entry.get("id") == self_id:
            return chain[i + 1] if i + 1 < len(chain) else None
    return None


def _related_division(lat: float | None, lon: float | None, self_id: str, theme: str) -> dict:
    """The division containing the entity's point, as {division_id, ...}.

    Degrades rather than failing the whole lookup: the entity itself
    resolved fine, and a related-join outage should not turn a good answer
    into an error. An outage returns {"note": ...} though, not {} — {} is
    reserved for "no division contains this", which is a real answer and
    must not be confused with "we couldn't check".
    """
    if lat is None or lon is None:
        return {}
    try:
        chain = divisions.admin_lookup(lat, lon)["chain"]
    except (overture.UpstreamUnavailable, overture.SchemaDegraded) as e:
        logger.warning("gers_lookup: containing-division join unavailable: %s", e)
        return {"note": RELATED_UNAVAILABLE_NOTE}
    entry = _containing_entry(chain, self_id, theme)
    if entry is None:
        return {}
    return {
        "division_id": entry.get("id"),
        "division_name": entry.get("name"),
        "division_type": entry.get("type"),
    }


def _related_building(lat: float | None, lon: float | None) -> dict:
    """Nearest building footprint to a place's point, as {building_id, ...}.

    Only meaningful for places (a building's related building is itself; a
    division's is meaningless), so only the places probe asks for it. The
    distance comes back with it because "nearest within 100m" is not the
    same claim as "the building this place is in" — the caller needs the
    number to judge which one it got. Degrades like the division join: {}
    for "no building within range" (a real answer), a note for an outage.
    """
    if lat is None or lon is None:
        return {}
    try:
        rows = buildings.buildings_at(
            lat, lon, radius_m=buildings.DEFAULT_NEAREST_RADIUS_M, limit=1
        )
    except (overture.UpstreamUnavailable, overture.SchemaDegraded) as e:
        logger.warning("gers_lookup: building-at-point join unavailable: %s", e)
        return {"note": BUILDING_UNAVAILABLE_NOTE}
    if not rows or rows[0].get("id") is None:
        return {}
    return {"building_id": rows[0]["id"], "building_distance_m": rows[0]["distance_m"]}


def _merge_related(division: dict, building: dict) -> dict:
    """The two joins as one "related" dict, keeping both degradation notes."""
    merged = {**division, **building}
    notes = [n for n in (division.get("note"), building.get("note")) if n]
    if notes:
        merged["note"] = "; ".join(notes)
    return merged


def gers_lookup(
    id: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
) -> dict | None:
    """One GERS id -> the entity it names, plus its cheap cross-theme joins.

    Returns {"id", "theme", "type", "name", "lat", "lon", "summary",
    "related"} for the first theme that claims the id, or None if no theme
    does (the server tool turns that into {"error": "not_found"}). summary
    carries a few compact theme-specific fields (place: category/confidence/
    brand; division: subtype/country/region; building: class/height/floors),
    never geometry.

    near_lat/near_lon is an optional location hint — pass the lat/lon of the
    row the id came from and every probe is narrowed to a 50km bbox instead
    of scanning the theme. Strongly recommended: see the module docstring on
    what an unhinted lookup costs. Note that the hint *bounds* the search:
    an id outside the box comes back as a miss, not as a slow full scan.

    Raises ValueError for an id that could not be a GERS id at all. When no
    theme claims the id but some theme could not actually be checked, the
    miss is not confirmed and the lookup raises instead of returning None:
    UpstreamUnavailable if a probe's remote scan failed, SchemaDegraded if
    a theme was too degraded to look an id up in — either error naming the
    unchecked theme(s). A hinted probe whose dataset is missing the bbox
    column counts as degraded (see _bbox_or_degraded).
    """
    id = _validate_id(id)
    hinted = near_lat is not None and near_lon is not None
    if not hinted and _miss_cached(id):
        logger.info("gers_lookup(%s): known-miss, skipping all theme scans", id)
        return None

    entity, degraded, failed = None, [], []
    for theme, probe in _PROBES:
        try:
            entity = probe(id, near_lat, near_lon)
        except overture.SchemaDegraded as e:
            # One theme too degraded to check shouldn't stop the others from
            # answering; a lookup only errors if nothing resolved (below).
            degraded.append((theme, e))
            continue
        except overture.UpstreamUnavailable as e:
            # Likewise one theme being unreachable: an id the *next* theme
            # owns still resolves. Only a lookup where nothing resolved and
            # something broke is an upstream error (below).
            logger.warning("gers_lookup(%s): %s probe failed: %s", id, theme, e)
            failed.append((theme, e))
            continue
        if entity is not None:
            break

    if entity is None:
        if failed:
            # A miss plus a failure is not a miss: the themes that broke
            # might have owned the id. Name them so the caller knows what
            # was and wasn't actually checked.
            names = ", ".join(theme for theme, _ in failed)
            raise overture.UpstreamUnavailable(
                f"could not check the {names} theme(s) for GERS id {id!r} "
                f"({failed[0][1].detail}); no other theme claimed it, so this is "
                "an unavailable lookup rather than a confirmed not-found"
            )
        if degraded:
            # Same reasoning for a degraded theme: it might have owned the
            # id, so a miss elsewhere is not a confirmed not-found. Name the
            # unchecked theme(s) rather than answering with false confidence.
            names = ", ".join(theme for theme, _ in degraded)
            columns = sorted({c for _, e in degraded for c in e.missing})
            raise overture.SchemaDegraded(columns, dataset=f"{names} dataset(s)")
        if not hinted:
            # Only an exhaustive, fully-healthy miss is authoritative enough
            # to cache — see the module docstring.
            _cache_miss(id)
        return None

    lat, lon = entity["lat"], entity["lon"]
    related = _related_division(lat, lon, id, entity["theme"])
    if entity["theme"] == "places":
        related = _merge_related(related, _related_building(lat, lon))
    return {"id": id, **entity, "related": related}
