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

A hint that *misses* still falls back to the unbounded scan, per theme, and
logs that it did. That is place_details' existing contract (issue #41) and
all three probes here follow it, so the three themes behave identically and
a stale or wrong hint can never turn a resolvable id into a false
not_found — it only costs the scan the unhinted call would have cost.

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

import duckdb

from placeroot import buildings, db, divisions, geo, overture

logger = logging.getLogger(__name__)

# Sanity bound on the id string itself. Real GERS ids are short (32-char
# hex-ish tokens); anything wildly longer is a caller mistake, and rejecting
# it up front beats spending three theme scans discovering it matches
# nothing. Generous enough not to reject a longer id format later.
MAX_ID_LENGTH = 128

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
    return trimmed


def _hint_bbox(
    near_lat: float | None, near_lon: float | None
) -> tuple[float, float, float, float] | None:
    """The 50km box around a near-hint, or None when no hint was given."""
    if near_lat is None or near_lon is None:
        return None
    return geo.bbox_around(near_lat, near_lon, overture.ID_HINT_RADIUS_M)


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
    """Hint-narrowed id lookup, falling back to the unbounded scan on a miss.

    The shared body of the divisions and buildings probes — the same
    two-stage shape overture._place_details_by_id uses for places, so all
    three themes have one contract (see the module docstring). from_source_fn
    lets a theme route the *narrowed* read through its tile cache; the
    fallback read is always straight upstream, since there is no bbox to
    resolve tiles from.
    """
    unbounded = f"read_parquet('{glob}', hive_partitioning=1)"
    if bbox is not None:
        bbox_filter, bbox_params = geo.bbox_filter_sql(*bbox)
        source = from_source_fn(bbox) if from_source_fn is not None else unbounded
        row = _run_id_query(select_sql, source, {"id": id, **bbox_params}, bbox_filter)
        if row is not None:
            return row
        logger.warning(
            "gers_lookup(%s): %s near-hint missed, falling back to a full-theme scan", id, theme
        )
    return _run_id_query(select_sql, unbounded, {"id": id}, None)


def _probe_places(id: str, near_lat: float | None, near_lon: float | None) -> dict | None:
    """Places probe, delegating wholesale to place_details' id resolution.

    place_details already implements the exact strategy this module wants
    (local tile cache -> hint-constrained upstream -> full scan, logged);
    reimplementing it here would be a second copy that drifts. We just take
    its rich row and keep the handful of fields that belong in a compact
    cross-theme answer.
    """
    row = overture.place_details(id=id, near_lat=near_lat, near_lon=near_lon)
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
    bbox = None if "bbox" in missing else _hint_bbox(near_lat, near_lon)
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
    bbox = None if "bbox" in missing else _hint_bbox(near_lat, near_lon)
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


_PROBES = (_probe_places, _probe_divisions, _probe_buildings)


def _related_division(lat: float | None, lon: float | None, self_id: str) -> dict:
    """Smallest division containing the entity's point, as {division_id, ...}.

    Degrades to {} rather than failing the whole lookup: the entity itself
    resolved fine, and a related-join outage (or a point no division covers,
    which is a perfectly valid answer) should not turn a good answer into an
    error. An entry whose id *is* the entity we looked up is skipped — for a
    division, "contained by itself" is noise, not a join.
    """
    if lat is None or lon is None:
        return {}
    try:
        chain = divisions.admin_lookup(lat, lon)["chain"]
    except (overture.UpstreamUnavailable, overture.SchemaDegraded) as e:
        logger.warning("gers_lookup: containing-division join unavailable: %s", e)
        return {}
    for entry in chain:
        if entry.get("id") == self_id:
            continue
        return {
            "division_id": entry.get("id"),
            "division_name": entry.get("name"),
            "division_type": entry.get("type"),
        }
    return {}


def _related_building(lat: float | None, lon: float | None) -> dict:
    """Nearest building footprint to a place's point, as {building_id, ...}.

    Only meaningful for places (a building's related building is itself; a
    division's is meaningless), so only the places probe asks for it. The
    distance comes back with it because "nearest within 100m" is not the
    same claim as "the building this place is in" — the caller needs the
    number to judge which one it got. Degrades to {} like the division join.
    """
    if lat is None or lon is None:
        return {}
    try:
        rows = buildings.buildings_at(
            lat, lon, radius_m=buildings.DEFAULT_NEAREST_RADIUS_M, limit=1
        )
    except (overture.UpstreamUnavailable, overture.SchemaDegraded) as e:
        logger.warning("gers_lookup: building-at-point join unavailable: %s", e)
        return {}
    if not rows or rows[0].get("id") is None:
        return {}
    return {"building_id": rows[0]["id"], "building_distance_m": rows[0]["distance_m"]}


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
    what an unhinted lookup costs.

    Raises ValueError for an id that could not be a GERS id at all,
    UpstreamUnavailable if a probe's remote scan fails, or SchemaDegraded
    only if *every* theme is too degraded to look an id up in.
    """
    id = _validate_id(id)

    entity, degraded = None, []
    for probe in _PROBES:
        try:
            entity = probe(id, near_lat, near_lon)
        except overture.SchemaDegraded as e:
            # One theme missing its id column shouldn't stop the others from
            # answering; only an all-themes-degraded dataset is fatal.
            degraded.append(e)
            continue
        if entity is not None:
            break

    if entity is None:
        if len(degraded) == len(_PROBES):
            raise degraded[0]
        return None

    lat, lon = entity["lat"], entity["lon"]
    related = _related_division(lat, lon, id)
    if entity["theme"] == "places":
        related.update(_related_building(lat, lon))
    return {"id": id, **entity, "related": related}
