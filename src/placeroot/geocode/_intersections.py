"""Intersections: "A & B, City" parsing and geocode_intersection()."""

import sys as _sys

from placeroot import geo, routing

_pkg = _sys.modules["placeroot.geocode"]


def _looks_like_street(half: str, *, strict: bool) -> bool:
    """Is this half of a split query a street name and not a fragment?

    Loose (symbol separators): non-empty, has a letter, is not a bare number.
    Strict (word separators): additionally carries a street-type word or an
    ordinal token, so "Rock" (of "Rock and Roll ...") fails and the query
    falls through to today's single-street scan.
    """
    tokens = half.split()
    if not tokens or not any(ch.isalpha() for ch in half):
        return False
    if not strict:
        return True
    for tok in tokens:
        key = tok.strip(".").lower()
        if key in _pkg._STREET_TYPE_WORDS or _pkg._ORDINAL_RE.match(key) or key in _pkg._WORD_ORDINALS:  # noqa: E501
            return True
    return False



def _split_intersection(street: str) -> tuple[str, str] | None:
    """"5th Ave & 42nd St" -> ("5th Ave", "42nd St"); a plain street -> None.

    Exactly two halves, each street-looking (see _looks_like_street); any
    other shape — three pieces, an empty side, a half that is only a number
    — is not an intersection this parser will vouch for, and the caller
    scans the text as one street as it always has.
    """
    for pattern, strict in (
        (_pkg._INTERSECTION_SYMBOL_SPLIT_RE, False),
        (_pkg._INTERSECTION_WORD_SPLIT_RE, True),
    ):
        parts = [p.strip() for p in pattern.split(street)]
        if len(parts) != 2:
            continue
        if all(_looks_like_street(p, strict=strict) for p in parts):
            return parts[0], parts[1]
        return None
    return None



# --- #448: geocode_intersection --------------------------------------------

INTERSECTION_MAX_LIMIT = 5

# Divided roads and signalled junctions are several Overture connectors per
# physical corner (15-40 m apart); matched nodes closer than this to one
# already kept are the same crossing, not another one.
INTERSECTION_CLUSTER_M = 50.0

# Floor on the walk-graph extraction radius: a village whose boundary is a
# few hundred metres across still gets a graph big enough to hold its
# streets, and the cache tile is never smaller than a walk isochrone's.
INTERSECTION_MIN_RADIUS_M = 1000.0


# The trailing tokens an Overture street name may carry beyond what the
# caller typed and still be *the same street*: the cardinal/quadrant suffix
# DC, Atlanta, Calgary and Portland put on every name ("Pennsylvania Avenue
# NW" for a query of "Pennsylvania Avenue"). Nothing else — "Broadway
# Terrace" is a different street from "Broadway", "Park Ave Ext" from "Park
# Ave", "Main St Bridge" from "Main St" — and a lone "Main" must not sweep
# up Main St, Main Ave and Main Street North the way geocode_address's
# ILIKE prefix deliberately does over address rows: there a wide match is
# a longer list to pick from, here it is a wrong coordinate.
_STREET_DIRECTIONAL_SUFFIXES: frozenset[str] = frozenset({
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "north", "south", "east", "west",
    "northeast", "northwest", "southeast", "southwest",
})



def _query_has_directional(variants: set[str]) -> bool:
    """Did the caller's street carry a directional token ("East 42nd St",
    "Main St N")? _street_variants only respells the tokens the query has,
    so a directional in any variant means one in the query."""
    return any(
        tok in _STREET_DIRECTIONAL_SUFFIXES for v in variants for tok in v.split()
    )



def _matches_street(edge_name: str | None, variants: set[str]) -> bool:
    """Whether an edge's street name is one of the caller's street's spellings.

    `variants` is _street_variants' lowercase output for the query (USPS
    suffixes, cardinals, ordinals). Equality, or equality followed only by
    directional tokens (_STREET_DIRECTIONAL_SUFFIXES) — never an arbitrary
    longer name.

    #465: when the query carries no directional at all, a single *leading*
    directional on the map's name is dropped too, so "42nd St" reaches
    Manhattan's "East 42nd Street" and "West 42nd Street" (5th Avenue is
    the E/W divide; no segment is named plain "42nd Street" there). A query
    that does name a side keeps exact matching — "East 42nd St" never
    matches "West 42nd Street" — and only whole tokens strip, so "Main St"
    still does not match "Main Street North Extension".
    """
    if not edge_name:
        return False
    enl = " ".join(edge_name.lower().split())
    if enl in variants:
        return True
    for v in variants:
        if len(enl) > len(v) and enl.startswith(v) and enl[len(v)] == " ":
            rest = enl[len(v) + 1:].split()
            if all(tok in _STREET_DIRECTIONAL_SUFFIXES for tok in rest):
                return True
    tokens = enl.split()
    if (
        len(tokens) > 1
        and tokens[0] in _STREET_DIRECTIONAL_SUFFIXES
        and not _query_has_directional(variants)
    ):
        return " ".join(tokens[1:]) in variants
    return False



def geocode_intersection(
    street_a: str = "",
    street_b: str = "",
    city: str = "",
) -> dict:
    """"5th Avenue", "Main Street", "Portland" -> coordinate where they cross.

    Locates where two named streets intersect within a resolved city anchor.

    Four steps:
    1. Parse and validate street_a, street_b, and city. Reject self-intersections
       where both street names normalize to the same street.
    2. Resolve the city anchor extent (_resolve_city_anchor, shared with
       geocode_address). No extent or too broad returns an empty list plus a note.
    3. Build or retrieve the walk graph around the anchor center, sized to the
       city bbox and clamped to routing.WALK_MAX_RADIUS_M. The search covers
       the part of the bbox within that radius; when the city is larger the
       note says so, so the caller can pass a neighborhood as `city` instead.
    4. Find junction nodes (three or more neighbors — a degree-2 node is one
       road changing name, not two roads meeting) inside that extent with an
       incident edge matching street_a and a different incident edge matching
       street_b, through the normalized USPS variants plus a directional
       suffix the caller left off ("Pennsylvania Avenue" -> "Pennsylvania
       Avenue NW"). Cluster the connectors of a divided-road junction within
       INTERSECTION_CLUSTER_M, keeping the one nearest the city center.

    Returns {"results": [{"lat", "lon", "streets": [name_a, name_b]}, ...],
    "anchor": {"name", "id", "country", "admin_context"}, "note": ...}.
    `streets` carries the map's own spelling of the two streets (Overture
    names.primary of the matched edges, in street_a/street_b order), which is
    how a caller learns the "NW" or "Avenue" they left off. `anchor` is the
    place the search ran in, once, the same shape geocode_address returns.
    When multiple crossings exist, results are ordered nearest to the city
    center first and capped at INTERSECTION_MAX_LIMIT. If one or both streets
    do not resolve in the city, returns empty results and a note naming the
    unresolved street. When the extracted graph hit routing.MAX_GRAPH_SEGMENTS
    the answer carries "truncated": true and says so, since a street missing
    from a partial graph is not a street missing from the city.
    """
    street_a = (street_a or "").strip()
    street_b = (street_b or "").strip()
    city = (city or "").strip()

    if not city:
        return {"results": [], "note": _pkg._INTERSECTION_NO_ANCHOR_NOTE}
    if not street_a and not street_b:
        return {
            "results": [],
            "note": (
                "no streets to search for. Pass two street names and a city to "
                "find their intersection."
            ),
        }
    if not street_a:
        return {
            "results": [],
            "note": (
                f"no first street provided to cross with \"{street_b}\". Pass two street "
                "names and a city to find their intersection."
            ),
        }
    if not street_b:
        return {
            "results": [],
            "note": (
                f"no second street provided to cross with \"{street_a}\". Pass two street "
                "names and a city to find their intersection."
            ),
        }

    variants_a = {v.lower() for v in _pkg._street_variants(street_a)}
    variants_b = {v.lower() for v in _pkg._street_variants(street_b)}

    if (
        variants_a & variants_b
        or _pkg._matches_street(street_a, variants_b)
        or _pkg._matches_street(street_b, variants_a)
    ):
        return {
            "results": [],
            "note": (
                f"\"{street_a}\" and \"{street_b}\" refer to the same street. "
                "Pass two different street names to find their intersection."
            ),
        }

    resolved = _pkg._resolve_city_anchor(city, action_label="search")
    if resolved.bbox is None:
        return {
            "results": [],
            "note": _pkg._intersection_unresolved_anchor_note(
                city, resolved.top, resolved.rejected, resolved.too_broad
            ),
        }
    anchor = resolved.anchor
    bbox = resolved.bbox
    notes: list[str] = list(resolved.notes)

    center_lat = anchor["lat"]
    center_lon = anchor["lon"]
    min_lon, min_lat, max_lon, max_lat = bbox

    max_corner_dist = max(
        geo.haversine_m(center_lat, center_lon, lat, lon)
        for lat in (min_lat, max_lat)
        for lon in (min_lon, max_lon)
    )
    radius_m = max(
        min(max_corner_dist, routing.WALK_MAX_RADIUS_M), INTERSECTION_MIN_RADIUS_M
    )

    graph = routing._get_or_build_graph(
        center_lat,
        center_lon,
        radius_m,
        mode="walk",
        speed_m_s=None,
        want_shapes=False,
    )

    found_a = False
    found_b = False
    crossings: list[dict] = []
    # A graph has hundreds of thousands of edge visits and a few thousand
    # distinct street names: match each name once, then it is a dict lookup.
    name_hits: dict[str, tuple[bool, bool]] = {}

    for node_id, (nlat, nlon) in graph.coords.items():
        # The served graph can be larger than the city — a cached 5 km walk
        # graph centered elsewhere, or the GRAPH_CACHE_MARGIN-padded build —
        # and a crossing in the next town over is not an answer about this
        # one. Bound by the anchor's own extent, as geocode_address's scan is.
        if not (min_lat <= nlat <= max_lat and min_lon <= nlon <= max_lon):
            continue
        nbrs = graph._undirected_neighbors.get(node_id, set())
        edges_a: list[tuple[str, str]] = []
        edges_b: list[tuple[str, str]] = []
        for nbr in nbrs:
            edge_name = graph.name_between(node_id, nbr) or graph.name_between(nbr, node_id)
            if not edge_name:
                continue
            hit = name_hits.get(edge_name)
            if hit is None:
                hit = (
                    _pkg._matches_street(edge_name, variants_a),
                    _pkg._matches_street(edge_name, variants_b),
                )
                name_hits[edge_name] = hit
            if hit[0]:
                found_a = True
                edges_a.append((nbr, edge_name))
            if hit[1]:
                found_b = True
                edges_b.append((nbr, edge_name))

        # Two streets meeting is a junction: at least three ways leave it.
        # A degree-2 node with one matching edge on each side is a single
        # road changing its name (Main St becoming Broadway at the city
        # line), which is not where they cross.
        if len(nbrs) < 3 or not edges_a or not edges_b:
            continue
        matched_pair: tuple[str, str] | None = None
        for nbr_a, name_a in edges_a:
            for nbr_b, name_b in edges_b:
                if nbr_a != nbr_b:
                    matched_pair = (name_a, name_b)
                    break
            if matched_pair:
                break
        if matched_pair is None:
            continue
        dist_m = geo.haversine_m(center_lat, center_lon, nlat, nlon)
        # #465: a junction sitting on the rim of the extracted graph is still
        # a junction in this city -- 5th Avenue & 42nd Street lies 5,003 m
        # from New York's anchor point against a 5,000 m walk radius and was
        # dropped by 3 m. Allow one cluster width of slack past the radius;
        # the graph itself already bounds how far out a node can be.
        if dist_m > radius_m + INTERSECTION_CLUSTER_M:
            continue
        crossings.append({
            "lat": nlat,
            "lon": nlon,
            "streets": [matched_pair[0], matched_pair[1]],
            "_dist": dist_m,
        })

    crossings.sort(key=lambda c: c["_dist"])
    clustered: list[dict] = []
    for c in crossings:
        if not any(
            geo.haversine_m(c["lat"], c["lon"], acc["lat"], acc["lon"]) < INTERSECTION_CLUSTER_M
            for acc in clustered
        ):
            clustered.append(c)
            if len(clustered) >= _pkg.INTERSECTION_MAX_LIMIT:
                break

    results = [
        {k: v for k, v in c.items() if k != "_dist"}
        for c in clustered
    ]

    payload: dict = {
        "results": results,
        # Once, at the top, same shape as geocode_address: the anchor is the
        # one thing that decides *which* Portland this answers about, and
        # repeating it on every row adds bytes, not information.
        "anchor": {
            "name": anchor["name"],
            "id": anchor["id"],
            "country": anchor.get("country"),
            "admin_context": anchor.get("admin_context") or [],
        },
    }
    if not results:
        # A street absent from a graph that stopped at its segment cap is a
        # fact about the extraction, not the city; say "the extracted part"
        # rather than asserting the negative about the map.
        where = (
            f"the extracted part of {anchor['name']}" if graph.truncated else anchor["name"]
        )
        if not found_a and not found_b:
            notes.append(f"neither \"{street_a}\" nor \"{street_b}\" resolved in {where}")
        elif not found_a:
            notes.append(f"\"{street_a}\" did not resolve in {where}")
        elif not found_b:
            notes.append(f"\"{street_b}\" did not resolve in {where}")
        else:
            notes.append(f"\"{street_a}\" and \"{street_b}\" do not intersect in {where}")
        if max_corner_dist > routing.WALK_MAX_RADIUS_M:
            notes.append(
                f"(the search covered only the {routing.WALK_MAX_RADIUS_M / 1000.0:.1f} km "
                f"around the center of {anchor['name']}; pass a neighborhood or district "
                f"as `city` to search elsewhere in it)"
            )
    if graph.truncated:
        payload["truncated"] = True
        notes.append(
            f"the street graph around {anchor['name']} hit its "
            f"{routing.MAX_GRAPH_SEGMENTS:,}-segment cap, so this is a partial view of "
            f"the network and a street or crossing may be missing from it"
        )
    if notes:
        payload["note"] = " ".join(notes)
    return payload
