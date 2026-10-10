"""Division name search: literal, variant and fuzzy passes over the divisions table."""

import sys
from pathlib import Path

import duckdb

from placeroot import geo, trace

_pkg = sys.modules["placeroot.geocode"]


# #476: a (lat, lon, radius_m) constraint on the division rows a name query
# may return. resolve_place passes its city pin here so the division pass —
# literal, alternate-name, variant and fuzzy — is bounded the same way the
# "City, ST" region filter already bounds it, instead of matching the whole
# planet and filtering afterwards.
NearConstraint = tuple[float, float, float]



def _near_filter_sql(
    near: NearConstraint | None, lat_col: str, lon_col: str, params: dict
) -> str:
    """AND-clause keeping rows whose point lies in `near`'s bounding box, or
    "" when there is no constraint. Adds its parameters to `params`.

    A box, not the exact circle: this is a row prefilter on the divisions
    table (tiny relative to a places scan), and the caller that passes a
    constraint (resolve_place, #476) still applies the haversine radius to
    what comes back. Antimeridian-safe the same way geo.bbox_filter_sql is:
    a box that ran past +/-180 becomes an OR of the two in-range halves.
    """
    if near is None:
        return ""
    lat, lon, radius_m = near
    xmin, ymin, xmax, ymax = geo.bbox_around(lat, lon, radius_m)
    params["near_ymin"], params["near_ymax"] = ymin, ymax
    lat_clause = f"AND {lat_col} BETWEEN $near_ymin AND $near_ymax"
    if xmin >= -180.0 and xmax <= 180.0:
        params["near_xmin"], params["near_xmax"] = xmin, xmax
        return f"{lat_clause} AND {lon_col} BETWEEN $near_xmin AND $near_xmax"
    params["near_xmin"] = xmin + 360.0 if xmin < -180.0 else xmin
    params["near_xmax"] = xmax - 360.0 if xmax > 180.0 else xmax
    return f"{lat_clause} AND ({lon_col} >= $near_xmin OR {lon_col} <= $near_xmax)"



def _query_divisions_from_local(
    table_path: str,
    query: str,
    region_code: str | None,
    name_match_expr: str = "name",
    country_code: str | None = None,
    near: NearConstraint | None = None,
) -> list[dict]:
    """name_match_expr (#53) is the SQL expression matched against $pattern
    /$exact/$prefix — "name" for a plain literal search, or
    "strip_accents(name)" for the diacritic-folded second-pass query (caller
    passes an already diacritic-stripped `query` to match against it).

    country_code (#457) narrows the same way region_code does, on the row's
    own `country` column — filtering by hierarchy membership, not a second
    string match. Both filters may be given together (a region-suffix
    parse combined with an explicit `country=`).

    near (#476): bound the rows to a (lat, lon, radius_m) box — see
    _near_filter_sql."""
    filters = []
    if region_code:
        filters.append("AND region = $region_code")
    if country_code:
        filters.append("AND country = $country_code")
    region_filter = " ".join(filters)
    q = _pkg.overture._like_escape(query)
    params: dict = {"pattern": f"%{q}%", "exact": q, "prefix": f"{q}%"}
    if region_code:
        params["region_code"] = region_code
    if country_code:
        params["country_code"] = country_code
    near_filter = _pkg._near_filter_sql(near, "lat", "lon", params)
    sql = f"""
        SELECT id, name, subtype, country, region, lat, lon, admin_chain, population
        FROM read_parquet('{table_path}')
        WHERE {name_match_expr} ILIKE $pattern ESCAPE '\\'
        {region_filter}
        {near_filter}
        ORDER BY {_pkg._match_tier_order_sql(name_match_expr)}
        LIMIT {_pkg.DIVISION_OVERFETCH}
    """
    try:
        with _pkg.overture._conn_lock:
            rows = _pkg.overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise _pkg.overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _pkg._admin_chain_context(r[7], self_name=r[1]),
            "population": r[8],
        })
    return result



def _query_divisions_from_upstream(
    query: str,
    region_code: str | None,
    name_match_expr: str = "names.primary",
    country_code: str | None = None,
    near: NearConstraint | None = None,
) -> list[dict]:
    """Direct upstream scan — the pre-#43 path, used when no local table is
    available (PLACEROOT_CACHE=off, or materialization failed).

    name_match_expr: see _query_divisions_from_local (#53).
    country_code: see _query_divisions_from_local (#457).
    """
    # type=division (points + hierarchies), not divisions.py's type=division_area
    # (polygons) — the two share a theme but are read from different fixtures/globs.
    glob = _pkg.overture.upstream_glob(theme="divisions", type_="division")
    cols = _pkg.overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return []
    population_expr = "population" if cols is None or "population" in cols else "NULL AS population"
    filters = []
    q = _pkg.overture._like_escape(query)
    params: dict = {"pattern": f"%{q}%", "exact": q, "prefix": f"{q}%"}
    if region_code and (cols is None or "region" in cols):
        filters.append("AND region = $region_code")
        params["region_code"] = region_code
    if country_code and (cols is None or "country" in cols):
        filters.append("AND country = $country_code")
        params["country_code"] = country_code
    region_filter = " ".join(filters)
    # #476: with a constraint the scan is still a name search over the whole
    # theme (nothing to prune files by), but the rows it returns are bounded.
    near_filter = _pkg._near_filter_sql(near, "bbox.ymin", "bbox.xmin", params)
    sql = f"""
        SELECT id, names.primary AS name, subtype, country, region,
               bbox.ymin AS lat, bbox.xmin AS lon, hierarchies, {population_expr}
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE {name_match_expr} ILIKE $pattern ESCAPE '\\'
        {region_filter}
        {near_filter}
        ORDER BY {_pkg._match_tier_order_sql(name_match_expr)}
        LIMIT {_pkg.DIVISION_OVERFETCH}
    """
    try:
        # Unbounded by construction: a name search over the divisions theme
        # has no bbox to prune by. That is exactly why the callers gate it.
        with trace.scan("divisions name scan (upstream)", bounded=False, source=glob), \
                _pkg.overture._conn_lock:
            rows = _pkg.overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise _pkg.overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _pkg._admin_context(r[7], self_name=r[1]),
            "population": r[8],
        })
    return result



def _query_alt_names(
    alt_table: str,
    table_path: str,
    query: str,
    region_code: str | None,
    country_code: str | None = None,
    near: NearConstraint | None = None,
) -> list[dict]:
    """#214: divisions whose *alternate* (names.common) spelling matches
    `query`, joined back to the local divisions table for the real row.

    The stored alt_name column is already folded (see
    _materialize_alt_names_table), so the query is folded the same way in
    Python and the predicate stays a plain ILIKE on a stored column —
    0.19s measured over 4.29M alternates, against the 197MB primary table.

    Rows come back tagged `_variant` like #53's retry hits — found through a
    spelling Overture doesn't call canonical — with `_tier` recorded against
    the alternate they actually matched (see _effective_tier: "Munich" is an
    exact match for München's alternate, and re-deriving a tier from the
    canonical "München" at rank time would silently demote it to the
    substring tier). `_matched_name` carries one real spelling of the
    alternate for the caller-visible `matched_name`.

    One row per division, not per matching alternate. A division has as many
    alternate rows as it has distinct folded spellings — Москва carries
    "moscow", "moskau", "moskva", "moskwa" — and a substring query ("Mosk")
    matches several of them at once, which without the QUALIFY below returns
    the same GERS id several times over and lets one division fill the
    caller's whole `limit`. The de-duplication is done in SQL rather than in
    Python so DIVISION_OVERFETCH bounds *divisions* and not near-duplicate
    rows: the same ranking that orders the result picks which alternate
    survives per id (best tier, then alt_name for determinism), so the row
    kept is the one that matched `query` best.

    Returns [] when the query folds away to nothing (a query of only
    combining marks does: _fold_alt_name strips them). The pattern would
    otherwise be a bare '%%' matching every alternate in the table, which
    answers a nonsense query with arbitrary prominent divisions — the
    literal search, matching raw names, returns nothing for those.
    """
    folded = _pkg._fold_alt_name(query)
    if not folded:
        return []
    q = _pkg.overture._like_escape(folded)
    params: dict = {"pattern": f"%{q}%", "exact": q, "prefix": f"{q}%"}
    filters = []
    if region_code:
        filters.append("AND d.region = $region_code")
        params["region_code"] = region_code
    if country_code:
        filters.append("AND d.country = $country_code")
        params["country_code"] = country_code
    region_filter = " ".join(filters)
    near_filter = _pkg._near_filter_sql(near, "d.lat", "d.lon", params)
    match_order = _pkg._match_tier_order_sql("a.alt_name")
    sql = f"""
        SELECT d.id, d.name, d.subtype, d.country, d.region, d.lat, d.lon,
               d.admin_chain, d.population, a.alt_name, a.alt_display
        FROM read_parquet('{alt_table}') a
        JOIN read_parquet('{table_path}') d ON d.id = a.id
        WHERE a.alt_name ILIKE $pattern ESCAPE '\\'
        {region_filter}
        {near_filter}
        QUALIFY row_number() OVER (
            PARTITION BY d.id ORDER BY {match_order}, a.alt_name
        ) = 1
        ORDER BY {match_order}
        LIMIT {_pkg.DIVISION_OVERFETCH}
    """
    try:
        with _pkg.overture._conn_lock:
            rows = _pkg.overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise _pkg.overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _pkg._admin_chain_context(r[7], self_name=r[1]),
            "population": r[8],
            "_variant": True,
            "_tier": _pkg._match_tier(r[9], folded),
            "_matched_name": r[10] or r[9],
        })
    return result



def _alt_rows_not_already_found(
    alt_table: str,
    table_path: str,
    query: str,
    region_code: str | None,
    found: list[dict],
    country_code: str | None = None,
    near: NearConstraint | None = None,
) -> list[dict]:
    """#214 alternate-name matches for `query`, minus the divisions the
    literal pass already returned — a division found under its canonical
    name is not also reported as an alternate hit.

    A vanished alt table degrades to primary-only rather than failing the
    query, the same #230 shape _query_divisions handles for the primary
    table one level up. It is milder here: no alt table at all is already
    a supported state (a cache directory predating #214 is in it), so
    there is nothing to fall back *to* and nothing lost but the alternate
    spellings.
    """
    try:
        rows = _pkg._query_alt_names(alt_table, table_path, query, region_code, country_code, near=near)  # noqa: E501
    except _pkg.overture.UpstreamUnavailable:
        if Path(alt_table).exists():
            raise
        _pkg.logger.warning(
            "alternate-name table vanished mid-query (%s); answering from "
            "primary names only", alt_table,
        )
        return []
    # A division already found literally keeps its literal row — but it must
    # not keep a *worse tier* than the alternate hit actually achieved.
    #
    # #268: Overture's primary name for Casablanca is the three-script
    # "Casablanca ⵜⴰⴷⴷⴰⵔⵜ ⵜⵓⵎⵍⵉⵍⵜ الدار البيضاء", so the literal pass matches
    # "Casablanca" as a mere prefix of it (tier 2) while the alternate pass
    # matches the alternate "casablanca" exactly (tier 3). Dropping the
    # alternate row outright left the city ranked below Casablanca, Chile
    # (pop 17,948) — tier outranks population by design, so an artifact of
    # how the canonical name is spelled decided the answer, and geocode
    # confidently pointed 10,000 km away. Carrying the better tier across
    # costs nothing and is what _effective_tier exists to express.
    by_id = {r["id"]: r for r in found}
    fresh = []
    for r in rows:
        prior = by_id.get(r["id"])
        if prior is None:
            fresh.append(r)
        elif (r.get("_tier") or 0) > _pkg._effective_tier(prior, query):
            prior["_tier"] = r["_tier"]
            prior.setdefault("_matched_name", r.get("_matched_name"))
    return fresh



def _query_divisions(
    query: str,
    region_code: str | None,
    local_table: str | None,
    fold_diacritics: bool = False,
    alt_table: str | None = None,
    country_code: str | None = None,
    near: NearConstraint | None = None,
) -> list[dict]:
    """fold_diacritics (#53): match strip_accents(name) against `query`
    (which the caller must already have run through _strip_diacritics) —
    the diacritic-folded half of the second-pass variant retry.

    alt_table (#214): when given, union in the alternate-name matches for
    the same query. Passed only for the primary literal search — the #53
    variant retries leave it None, because the alternate side already folds
    case and diacritics itself, so re-running it on a diacritic-stripped
    spelling of the same query can only return rows this pass already has.

    country_code (#457): same role as region_code, filtering candidates by
    the row's own `country` column. The two may be combined.

    near (#476): a (lat, lon, radius_m) box the rows must fall inside —
    the city pin resolve_place already holds. Applied to every side of the
    search (primary, alternate, upstream) so a pinned query never has a
    far-away namesake in its candidate pool to begin with.
    """
    if local_table is not None:
        name_expr = "strip_accents(name)" if fold_diacritics else "name"
        try:
            rows = _query_divisions_from_local(
                local_table, query, region_code, name_expr, country_code, near=near
            )
        except _pkg.overture.UpstreamUnavailable:
            if Path(local_table).exists():
                raise
            # The table was deleted out from under us between the exists()
            # check in _local_divisions_table() and the read (external
            # cleanup, or a cache sweep — #230). Upstream is not known to be
            # unavailable, so don't report it as such: degrade to the direct
            # upstream scan, same as if the table had never materialized.
            _pkg.logger.warning(
                "local divisions table vanished mid-query (%s); falling back "
                "to a direct upstream scan", local_table,
            )
        else:
            if alt_table is not None:
                rows = rows + _alt_rows_not_already_found(
                    alt_table, local_table, query, region_code, rows, country_code, near=near
                )
            return rows
    name_expr = "strip_accents(names.primary)" if fold_diacritics else "names.primary"
    return _pkg._query_divisions_from_upstream(query, region_code, name_expr, country_code, near=near)  # noqa: E501



# --- #215: fuzzy fallback tier ------------------------------------------

# Minimum jaro_winkler_similarity (0..1) between a folded division name and
# the folded query for a fuzzy row to be offered at all.
#
# Calibrated against the live 2026-07-22.0 release: the three probes
# this tier exists for resolve top-1 correct well above it — "Berekley" ->
# Berkeley at 0.97, "Cinncinati" -> Cincinnati at 0.98, "Sna Francisco" ->
# San Francisco at 0.98 — so 0.92 keeps headroom for longer or two-typo
# spellings without reaching down into the ~0.85 band, where short,
# unrelated names ("Erie"/"Eire", "Lima"/"Lome") start pairing up. Raising
# it towards the measured 0.97 would buy nothing but lost corrections;
# lowering it trades a real answer for a plausible-looking wrong one, which
# for a geocoder is the worse failure.
_FUZZY_SIMILARITY_THRESHOLD = 0.92


# The folded name expression fuzzy matching compares against: same
# case-and-diacritic folding _normalize_for_match applies to the query in
# Python (#53's strip_accents, plus lower()), so "Sao Paulo" and "São
# Paulo" are the same string to the similarity function.
_FOLDED_NAME_SQL = "lower(strip_accents(name))"



def _has_like_metacharacter(query: str) -> bool:
    """True if `query` contains an ILIKE wildcard character.

    #165 made those literal, so "Brook_yn" matches nothing and stays
    visibly a non-match. Jaro-winkler doesn't know "_" from a letter and
    would quietly answer that query with "Brooklyn" — the same output a
    wildcard would have produced, which is precisely the behavior #165
    removed. Real typos don't contain "%" or "_", so declining to fuzzy
    these costs nothing and keeps that guarantee legible.
    """
    return "%" in query or "_" in query



def _query_divisions_fuzzy(
    table_path: str,
    query: str,
    region_code: str | None = None,
    country_code: str | None = None,
    near: NearConstraint | None = None,
) -> list[dict]:
    """Divisions whose folded name is within _FUZZY_SIMILARITY_THRESHOLD
    jaro-winkler of the folded query — the #215 typo tier.

    `region_code` narrows the pass to one region, the same filter the
    literal queries take. The caller passes the code parsed off the
    query's own "City, ST" suffix and retries without it if that comes
    back empty, mirroring what the literal search one step up already does
    — a region-constrained miss can mean the suffix was misread as a
    region, and answering nothing would be the worse failure.

    `near` (#476) bounds the pass to a (lat, lon, radius_m) box the same
    way. This is the tier that answered "Marina Bay Sands" — pinned to
    Singapore by its caller — with five "Marina Bay" neighbourhoods in
    Florida, California, Massachusetts and Nebraska: a typo correction
    12,000 km from where the caller said they meant is not a correction.
    Under a pin the similarity scan runs over the pin's box only, and a
    miss there is a miss (no unconstrained retry — the pin is the caller's
    statement, not a parsed suffix that might have been misread).

    Local table only, by construction: the caller passes a materialized
    table path or doesn't call this at all. A similarity predicate has
    nothing an upstream parquet reader can prune by, so running it against
    S3 would read the whole divisions theme over the network; locally the
    same full scan is 0.26s (measured, 4.65M names).

    Rows come back tagged `_fuzzy` with their `_similarity`, and `_tier` 1
    so tier-reading code (_effective_tier, and through it _rank_score)
    grades them as the weak match they are rather than re-deriving a tier
    from a name that doesn't contain the query at all. Ordering is
    similarity first, then population — between two names equally close to
    a typo, the one people are more likely to have meant is the bigger
    place.

    Primary names only, deliberately, even though #214 put 4.29M alternate
    spellings within reach of the same scan. Two reasons, neither of them
    cost: this predicate can't use an index either way, so the extra table
    roughly doubles a 0.26s pass, which is affordable. But
    _FUZZY_SIMILARITY_THRESHOLD was calibrated against primary names — 4.65M
    mostly-distinct strings — and Overture's alternates are a much denser,
    much shorter population (every language's rendering of every division),
    which is exactly the shape that produces spurious >=0.92 pairs. Choosing
    a threshold for it needs its own measurement, and a wrong fuzzy answer
    is the failure mode this tier is most careful about. Left for a
    follow-up with numbers behind it.

    Returns [] when the query folds to the empty string — a query of only
    combining marks does, since folding strips exactly those. DuckDB's
    jaro_winkler_similarity(name, '') scores above the threshold rather
    than at 0, so without this the tier answers a nonsense query with the
    most populous divisions in the dataset, presented as spelling
    corrections.
    """
    folded_query = _pkg._normalize_for_match(query)
    if not folded_query:
        return []
    params: dict = {"folded": folded_query}
    filters = []
    if region_code:
        filters.append("AND region = $region_code")
        params["region_code"] = region_code
    if country_code:
        filters.append("AND country = $country_code")
        params["country_code"] = country_code
    region_filter = " ".join(filters)
    near_filter = _pkg._near_filter_sql(near, "lat", "lon", params)
    sql = f"""
        SELECT id, name, subtype, country, region, lat, lon, admin_chain, population,
               jaro_winkler_similarity({_FOLDED_NAME_SQL}, $folded) AS similarity
        FROM read_parquet('{table_path}')
        WHERE jaro_winkler_similarity({_FOLDED_NAME_SQL}, $folded)
              >= {_pkg._FUZZY_SIMILARITY_THRESHOLD}
        {region_filter}
        {near_filter}
        ORDER BY similarity DESC, population DESC NULLS LAST
        LIMIT {_pkg.DIVISION_OVERFETCH}
    """
    try:
        with _pkg.overture._conn_lock:
            rows = _pkg.overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise _pkg.overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": r[2], "country": r[3], "region": r[4],
            "lat": round(r[5], 6), "lon": round(r[6], 6),
            "admin_context": _pkg._admin_chain_context(r[7], self_name=r[1]),
            "population": r[8],
            "_fuzzy": True, "_similarity": r[9], "_tier": 1,
        })
    return result



def _fuzzy_correction_note(rows: list[dict], query: str) -> str:
    """Note naming the spelling(s) a fuzzy answer corrected `query` to.

    Same idiom as the other notes in this module — plain prose saying what
    the search did and what the caller can do about it — but this one rides
    along with *results*, not instead of them: the results are real, they
    just answer a different spelling than the one asked for, and an agent
    that can't see the correction has no way to catch a wrong guess.
    """
    names = []
    for row in rows:
        if row["name"] not in names:
            names.append(row["name"])
    spellings = ", ".join(f'"{n}"' for n in names)
    return (
        f'no division is named "{query}"; these matched by close spelling instead '
        f"({spellings}). If that is not the place you meant, re-run geocode with the "
        "exact spelling, or use find_places with lat/lon to search a known area."
    )
