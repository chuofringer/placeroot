"""reverse_geocode() and its nearest-address and nearest-division lookups."""

import sys as _sys

import duckdb

_pkg = _sys.modules["placeroot.geocode"]


def _nearest_address(lat: float, lon: float) -> dict | None:
    """Nearest address point within an expanding search radius, or None if none found nearby
    or the addresses theme is unreachable/missing (degrade, don't raise).

    Reads through addresses._from_source, so this hop shares the tile cache
    (and the tiles themselves) with address_at rather than re-scanning S3 on
    every call — issue #189. Cache resolution can itself raise
    UpstreamUnavailable, which is caught alongside the query's own DB errors:
    this function's contract is to degrade to a divisions-only answer on any
    addresses-side failure, and "the cache could not reach upstream to
    materialize a tile" is one of those.
    """
    glob = _pkg.addresses._upstream_glob()
    cols = _pkg.overture.probe_schema(glob)
    if cols is not None and "street" not in cols:
        return None
    for radius_m in (200, 1000, 5000):
        bbox_filter, distance_filter, params, bbox, _radius_m = _pkg.overture.area_geometry(
            lat, lon, radius_m
        )
        try:
            sql = f"""
                SELECT street, number, postcode, bbox.ymin AS lat, bbox.xmin AS lon,
                       round({_pkg.overture.DISTANCE_EXPR}, 1) AS distance_m
                FROM {_pkg.addresses._from_source(bbox)}
                WHERE {bbox_filter} AND {distance_filter}
                ORDER BY distance_m
                LIMIT 1
            """
            with _pkg.overture._conn_lock:
                row = _pkg.overture.conn().execute(sql, params).fetchone()
        except (duckdb.Error, _pkg.overture.UpstreamUnavailable) as e:
            _pkg.logger.warning("addresses theme query failed, degrading to divisions-only: %s", e)
            return None
        if row:
            return {
                "street": row[0], "number": row[1], "postcode": row[2],
                "lat": round(row[3], 6), "lon": round(row[4], 6), "distance_m": row[5],
            }
    return None



def _nearest_division(lat: float, lon: float, country: str | None = None) -> dict | None:
    """Nearest locality-ish division to a point, scanning the divisions theme.

    `country` (#223) restricts the search to one country, for callers that
    already know which one the point is in and would contradict themselves by
    naming a place across the border. Left None by reverse_geocode, which
    knows only the coordinate and so wants the nearest division full stop.

    Also returns the row's own "country" (ISO 3166-1 alpha-2) and "region"
    (ISO 3166-2), when the active dataset carries those columns, so
    reverse_geocode can surface them without a second query (#446) — the
    same columns this query already reads for the `country` filter above.
    """
    glob = _pkg.overture.upstream_glob(theme="divisions", type_="division")
    cols = _pkg.overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return None
    has_country = cols is None or "country" in cols
    has_region = cols is None or "region" in cols
    country_filter = ""
    if country and has_country:
        country_filter = "AND country = $country"
    country_expr = "country" if has_country else "NULL"
    region_expr = "region" if has_region else "NULL"
    for radius_m in (2000, 20000, 100000):
        bbox_filter, distance_filter, params, _bbox, _radius_m = _pkg.overture.area_geometry(
            lat, lon, radius_m
        )
        if country_filter:
            params = {**params, "country": country}
        sql = f"""
            SELECT names.primary AS name, subtype, hierarchies,
                   {country_expr} AS country, {region_expr} AS region,
                   round({_pkg.overture.DISTANCE_EXPR}, 1) AS distance_m
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE {bbox_filter} AND {distance_filter}
              AND subtype IN ('locality', 'localadmin', 'neighborhood')
              {country_filter}
            ORDER BY distance_m
            LIMIT 1
        """
        try:
            with _pkg.overture._conn_lock:
                row = _pkg.overture.conn().execute(sql, params).fetchone()
        except duckdb.Error as e:
            _pkg.logger.warning("divisions theme query failed: %s", e)
            return None
        if row:
            chain = _pkg._admin_context(row[2], self_name=row[0])
            result = {"name": row[0], "subtype": row[1], "admin_context": [*chain, row[0]]}
            if row[3] is not None:
                result["country"] = row[3]
            if row[4] is not None:
                result["region"] = row[4]
            return result
    return None



def reverse_geocode(lat: float, lon: float) -> dict:
    """Nearest address (street/number/postcode) plus its containing division chain.

    Degrades to a divisions-only result — noting it via "source" and
    "note" — if the addresses theme is unreachable or missing, rather than
    failing the call: addresses is Overture's newest, least complete theme,
    so this is the expected degraded path, not a rare edge case.

    Also carries top-level "country" (ISO 3166-1 alpha-2) and "region"
    (ISO 3166-2), off the same nearest-division row admin_context is built
    from, when the active dataset has those columns — omitted, not
    null-filled, otherwise (#446).
    """
    address = _nearest_address(lat, lon)
    division = _pkg._nearest_division(lat, lon)
    admin_context = division["admin_context"] if division else []

    if address is not None:
        result = {
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
    else:
        result = {
            "address": None,
            "lat": lat,
            "lon": lon,
            "distance_m": None,
            "admin_context": admin_context,
            "source": "divisions_only",
            "note": (
                "no nearby address found (addresses theme unavailable, missing, or sparse here)"
            ),
        }
    if division:
        if "country" in division:
            result["country"] = division["country"]
        if "region" in division:
            result["region"] = division["region"]
    return result
