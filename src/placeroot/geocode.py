"""geocode / reverse_geocode on Overture data — no Nominatim, no third-party calls (#10).

geocode ranks divisions (locality/neighborhood/region/country) by name match
quality, falling back to places when divisions alone don't fill `limit`.
reverse_geocode finds the nearest address point and its containing division
chain, degrading to divisions-only when the addresses theme is unreachable
or missing (addresses is a much newer, less complete Overture theme than
places/divisions, so this is the realistic failure mode, not a hypothetical
one).

Ranking is deterministic and cheap on purpose: exact name match beats
prefix beats substring; ties break by a fixed subtype-size ordering
(locality is what most free-text queries mean by "a place"), then
alphabetically by id for full determinism. No Nominatim/geocoding API, no
scoring model — just SQL ILIKE plus Python-side ranking.
"""

import logging

import duckdb

from placeroot import overture

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
MAX_LIMIT = 25
DIVISION_OVERFETCH = 50  # rows pulled per theme before Python-side ranking trims to `limit`

# Bigger number = ranked higher among same name-match tier. Chosen so a
# free-text query like "Springfield" surfaces the city before a same-named
# neighborhood or the containing state, which is the common case for an
# agent asking "where is X".
_SUBTYPE_WEIGHT = {
    "locality": 4,
    "localadmin": 3,
    "neighborhood": 2,
    "region": 1,
    "county": 1,
    "country": 0,
    "dependency": 0,
}


def _match_tier(name: str, query: str) -> int:
    """3 = exact, 2 = prefix, 1 = substring, 0 = no match (caller already filtered those out)."""
    n, q = name.lower(), query.lower()
    if n == q:
        return 3
    if n.startswith(q):
        return 2
    return 1


def _rank_key(row: dict, query: str):
    tier = _match_tier(row["name"], query)
    weight = _SUBTYPE_WEIGHT.get(row.get("subtype"), 0)
    # Sort descending on tier/weight, ascending on id for a stable tiebreak.
    return (-tier, -weight, row["id"])


def _rank_score(row: dict, query: str) -> float:
    tier = _match_tier(row["name"], query)
    tier_score = {3: 1.0, 2: 0.7, 1: 0.4}[tier]
    weight = _SUBTYPE_WEIGHT.get(row.get("subtype"), 0)
    return round(tier_score + weight * 0.01, 3)


def _admin_context(hierarchies, self_name: str | None = None) -> list[str]:
    """Containing-chain names from the first hierarchy path, self excluded.

    hierarchies comes back from DuckDB as plain Python lists/dicts:
    list-of-paths, each path a list of {division_id, name, subtype} dicts
    ordered top-level ancestor first, the division itself last (verified
    against live Overture divisions data). self_name strips that trailing
    self-entry so admin_context is only what *contains* the result, not the
    result itself. Any structural surprise (schema drift across releases)
    degrades to an empty chain rather than raising, matching overture.py's
    degrade-don't-crash approach.
    """
    try:
        if not hierarchies:
            return []
        path = hierarchies[0]
        names = [entry["name"] for entry in path if entry and entry.get("name")]
        if self_name and names and names[-1] == self_name:
            names = names[:-1]
        return names
    except (TypeError, KeyError, AttributeError):
        return []


def _query_divisions(query: str) -> list[dict]:
    # type=division (points + hierarchies), not divisions.py's type=division_area
    # (polygons) — the two share a theme but are read from different fixtures/gloms.
    glob = overture.upstream_glob(theme="divisions", type_="division")
    cols = overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return []
    sql = f"""
        SELECT id, names.primary AS name, subtype, country, region,
               bbox.ymin AS lat, bbox.xmin AS lon, hierarchies
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE names.primary ILIKE $pattern
        LIMIT {DIVISION_OVERFETCH}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, {"pattern": f"%{query}%"}).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _admin_context(r[7], self_name=r[1]),
        })
    return result


def _query_places_fallback(query: str) -> list[dict]:
    """Supplement divisions with named places when divisions alone don't fill limit."""
    glob = overture.upstream_glob(theme="places", type_="place")
    cols = overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return []
    sql = f"""
        SELECT id, names.primary AS name, bbox.ymin AS lat, bbox.xmin AS lon,
               coalesce(confidence, 0) AS confidence
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE names.primary ILIKE $pattern
        ORDER BY confidence DESC
        LIMIT {DIVISION_OVERFETCH}
    """
    try:
        with overture._conn_lock:
            rows = overture.conn().execute(sql, {"pattern": f"%{query}%"}).fetchall()
    except duckdb.Error as e:
        raise overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": "place",
            "country": None, "region": None,
            "lat": round(r[2], 6), "lon": round(r[3], 6),
            "admin_context": [], "_confidence": r[4],
        })
    return result


def geocode(query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Free-text place name -> ranked candidates, from Overture divisions (and places fallback).

    Never more than `limit` results. Each result: {name, type, lat, lon, id
    (GERS), admin_context, rank_score}. Raises overture.UpstreamUnavailable
    if the remote scan fails after retries; the caller (server.py) turns
    that into a structured error like the other tools.
    """
    query = query.strip()
    limit = max(1, min(limit, MAX_LIMIT))
    if not query:
        return []

    divisions = _query_divisions(query)
    divisions.sort(key=lambda r: _rank_key(r, query))

    candidates = divisions
    if len(candidates) < limit:
        seen_names = {(c["name"].lower()) for c in candidates}
        places = _query_places_fallback(query)
        places = [p for p in places if p["name"].lower() not in seen_names]
        places.sort(key=lambda r: (-_match_tier(r["name"], query), -r["_confidence"], r["id"]))
        candidates = candidates + places

    out = []
    for row in candidates[:limit]:
        out.append({
            "name": row["name"],
            "type": row["subtype"],
            "lat": row["lat"],
            "lon": row["lon"],
            "id": row["id"],
            "admin_context": row["admin_context"],
            "rank_score": _rank_score(row, query) if "_confidence" not in row else round(
                0.4 + row["_confidence"] * 0.3, 3
            ),
        })
    return out


def _nearest_address(lat: float, lon: float) -> dict | None:
    """Nearest address point within an expanding search radius, or None if none found nearby
    or the addresses theme is unreachable/missing (degrade, don't raise)."""
    glob = overture.upstream_glob(theme="addresses", type_="address")
    cols = overture.probe_schema(glob)
    if cols is not None and "street" not in cols:
        return None
    for radius_m in (200, 1000, 5000):
        bbox_filter, distance_filter, params = overture.area_geometry(lat, lon, radius_m)
        sql = f"""
            SELECT street, number, postcode, bbox.ymin AS lat, bbox.xmin AS lon,
                   round({overture.DISTANCE_EXPR}, 1) AS distance_m
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND {distance_filter}
            ORDER BY distance_m
            LIMIT 1
        """
        try:
            with overture._conn_lock:
                row = overture.conn().execute(sql, params).fetchone()
        except duckdb.Error as e:
            logger.warning("addresses theme query failed, degrading to divisions-only: %s", e)
            return None
        if row:
            return {
                "street": row[0], "number": row[1], "postcode": row[2],
                "lat": round(row[3], 6), "lon": round(row[4], 6), "distance_m": row[5],
            }
    return None


def _nearest_division(lat: float, lon: float) -> dict | None:
    glob = overture.upstream_glob(theme="divisions", type_="division")
    cols = overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return None
    for radius_m in (2000, 20000, 100000):
        bbox_filter, distance_filter, params = overture.area_geometry(lat, lon, radius_m)
        sql = f"""
            SELECT names.primary AS name, subtype, hierarchies,
                   round({overture.DISTANCE_EXPR}, 1) AS distance_m
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND {distance_filter}
              AND subtype IN ('locality', 'localadmin', 'neighborhood')
            ORDER BY distance_m
            LIMIT 1
        """
        try:
            with overture._conn_lock:
                row = overture.conn().execute(sql, params).fetchone()
        except duckdb.Error as e:
            logger.warning("divisions theme query failed: %s", e)
            return None
        if row:
            chain = _admin_context(row[2], self_name=row[0])
            return {"name": row[0], "subtype": row[1], "admin_context": [*chain, row[0]]}
    return None


def reverse_geocode(lat: float, lon: float) -> dict:
    """Nearest address (street/number/postcode) plus its containing division chain.

    Degrades to a divisions-only result — noting it via "source" and
    "note" — if the addresses theme is unreachable or missing, rather than
    failing the call: addresses is Overture's newest, least complete theme,
    so this is the expected degraded path, not a rare edge case.
    """
    address = _nearest_address(lat, lon)
    division = _nearest_division(lat, lon)
    admin_context = division["admin_context"] if division else []

    if address is not None:
        return {
            "address": {
                "street": address["street"],
                "number": address["number"],
                "postcode": address["postcode"],
            },
            "lat": address["lat"],
            "lon": address["lon"],
            "distance_m": address["distance_m"],
            "admin_context": admin_context,
            "source": "address",
        }
    return {
        "address": None,
        "lat": lat,
        "lon": lon,
        "distance_m": None,
        "admin_context": admin_context,
        "source": "divisions_only",
        "note": "no nearby address found (addresses theme unavailable, missing, or sparse here)",
    }
