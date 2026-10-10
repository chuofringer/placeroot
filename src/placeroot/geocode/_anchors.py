"""Anchor resolution for qualified names and the places fallback search."""

import sys
import threading
from collections import OrderedDict

import duckdb

from placeroot import geo, home_region, manifest, trace

_pkg = sys.modules["placeroot.geocode"]


def _fallback_anchor_candidates(
    search_query: str,
    divisions: list[dict],
    region_code: str | None,
    local_table: str | None,
    alt_table: str | None = None,
    region_population: dict[str, int] | None = None,
) -> list[tuple[float, float, str | None]]:
    """Ranked (lat, lon, name_query) candidates to bound/aim the places
    fallback (#83) — best reading first, empty when no location context can
    be derived from the query at all. In the empty case the
    caller (geocode()) then runs the fallback unbounded, same as before #83
    (row-capped via DIVISION_OVERFETCH's LIMIT, but not bbox-pruned) rather
    than dropping a genuine name-only query (e.g. "Blue Bottle Roastery",
    with no city/region in it anywhere) to no results.

    Prefers the best division match already in hand (`divisions`, already
    ranked by _rank_key) — name_query stays the full search_query in that
    case. Failing that, tries the query's trailing one/two words as a
    division lookup of their own — e.g. "Westfield Valley Fair San Jose"
    never matches a division as a whole string, but its trailing "San Jose"
    does (the #83 bug's actual repro case: a big-box place name followed by
    the city it's in). Mirrors the trailing-token idea _split_region_suffix
    already uses for region suffixes, just aimed at finding *any* location
    anchor rather than a region code specifically.

    When the anchor comes from trailing tokens like this, name_query is the
    remaining prefix ("Westfield Valley Fair") rather than the full query —
    matching the *whole* query (which appends the city name onto the place
    name) against a places row's own name would otherwise never match
    anything, defeating the point of finding an anchor at all.

    #216: that trailing-token split is only worth acting on when what's
    left over is something a place could plausibly be *named*. name_query
    comes back None when it isn't — "the Met" splits into anchor "Met"
    (which substring-matches a real division) and residual "the", and
    ILIKE '%the%' matches a large fraction of every place on Earth: 50.2s
    measured live, answering "the Met" with "The Core IAS". The caller
    treats a None name_query as "skip the places half entirely and say so",
    which is strictly better than that. The rule is _nothing_but_stopwords':
    reject only when *every* word left is a _STOPWORD. That is deliberately
    looser than _significant_tokens' >=3-chars-and-not-a-stopword rule --
    rejecting here means returning nothing at all, and short is not the same
    as empty ("H&M Brooklyn", or a two-character Chinese name, has to reach
    the anchored scan). See _nothing_but_stopwords.

    #472 widened that gate by one set: a residual made only of feature nouns
    ("Station", left over once "Shibuya Station" anchors on Shibuya) is
    refused the same way. '%Station%' inside the anchor's box matched every
    station in Tokyo -- 5.5s, answering "Nakameguro Station" -- and the
    caller's skip-and-say-so is again the better answer. The predicate
    actually applied is _nothing_but_generic; the stopword rule above is
    kept intact inside it.

    Note this gate is about *emptiness*, not correctness: a misspelling
    like "Sna Francisco" (anchor "Francisco", residual "Sna") clears it
    just fine — "Sna" is not a stopword. Typos are handled a step earlier
    instead: #215's fuzzy tier retries the whole query by edit distance
    and, when it finds a correction, stands the places fallback down
    before this function is ever reached. Without a local divisions table
    for that tier to read (cache off), such a query still lands here and
    still reaches the substring scan of its own typo.

    #464: a split anchor is a *guess* about which words locate the query,
    and for a query that is nothing but a POI's name the guess is always
    wrong — there is no city in "Grand Central Terminal" or "Mall of
    America" to find. Traced live: "Grand Central Terminal" anchored on
    Chelyabinsk (its leading word "Central" exact-matches Центральный
    район's alternate name, and 101k people beat Grand Central, PA's
    none), and "Mall of America" anchored on a Virginia division whose
    name merely *contains* "of America". Two things follow. First, a
    trailing split whose division match is only a substring (the trailing
    words are a fragment of some division's longer name, and that name is
    not itself in the query) is not acted on at all when the residual is
    the larger part of the query — the user did not type that division,
    so the words were never a location. Second, every contender now says
    how much it should be trusted (see _fallback_anchor_details' `strong`):
    resolve_place uses that to decide whether a place found near the
    anchor must account for the anchor's words too, or only for the rest.
    A trailing exact/prefix match on a city-level division ("San Jose",
    "Chicago", "Rio de Janeiro" typed whole) is strong — the caller named
    the city, and a place inside it is not expected to repeat it. A
    leading-word anchor is never strong: #268's own premise is that the
    location word is part of the place's name ("Stanford Shopping Center",
    "Palo Alto Caltrain Station"), so a genuine match contains it, and a
    coincidence ("Grand Royal", 20 km from a district called Central) does
    not. A country- or region-level match is never strong either: its
    centroid locates nothing (see _pick_anchor_row), and "America" as a
    prefix of the United States' Min Nan name is not the caller naming
    Kansas.
    """
    return [(d["lat"], d["lon"], d["name_query"]) for d in _pkg._fallback_anchor_details(
        search_query, divisions, region_code, local_table,
        alt_table=alt_table, region_population=region_population,
    )]



# Memo for _fallback_anchor_details' split-derived anchors, keyed on its
# inputs. One resolve asks the same question up to three times — the
# bundled-recall gate, the places-fallback anchor, and resolve_place's own
# reference — and each answer costs ~22 division lookups. Cleared whenever
# a local table is republished and by clear_resolve_session.
_ANCHOR_MEMO_MAX = 128

_anchor_memo: OrderedDict[tuple, list[dict]] = OrderedDict()

_anchor_memo_lock = threading.Lock()



def _anchor_memo_key(
    search_query: str,
    region_code: str | None,
    local_table: str | None,
    alt_table: str | None,
    region_population: dict[str, int] | None,
) -> tuple:
    # The ranking inside (_rank_key via _pick_anchor_row) reads the #406
    # home region, so a home resolved between two calls must miss.
    home = home_region.get_home_region()
    home_key = (home.get("lat"), home.get("lon")) if home else None
    pop_key = frozenset(region_population.items()) if region_population else None
    return (search_query, region_code, local_table, alt_table, pop_key, home_key)



def _fallback_anchor_details(
    search_query: str,
    divisions: list[dict],
    region_code: str | None,
    local_table: str | None,
    alt_table: str | None = None,
    region_population: dict[str, int] | None = None,
) -> list[dict]:
    """_fallback_anchor_candidates, with each anchor's provenance kept.

    Memoized on its inputs (see _anchor_memo) once `divisions` is empty —
    the only branch that does any lookups.

    One dict per candidate, best first: "lat", "lon", "name_query" (as the
    tuple form), plus "candidate" (the query words the anchor was derived
    from — empty when a division in hand anchored the whole query),
    "split" (True when the anchor came from a trailing/leading split rather
    than from `divisions`), and "strong" (#464: whether the caller may
    treat the anchor's own words as accounted for by *location* rather than
    requiring a place's name to contain them — see the paragraph above).
    """
    if divisions:
        top = divisions[0]
        return [{
            "lat": top["lat"], "lon": top["lon"], "name_query": search_query,
            "candidate": "", "split": False, "strong": True,
        }]
    memo_key = _anchor_memo_key(
        search_query, region_code, local_table, alt_table, region_population
    )
    with _anchor_memo_lock:
        cached = _anchor_memo.get(memo_key)
        if cached is not None:
            _anchor_memo.move_to_end(memo_key)
            return [dict(d) for d in cached]
    out = _derive_split_anchors(
        search_query, region_code, local_table, alt_table, region_population
    )
    with _anchor_memo_lock:
        _anchor_memo[memo_key] = [dict(d) for d in out]
        _anchor_memo.move_to_end(memo_key)
        while len(_anchor_memo) > _ANCHOR_MEMO_MAX:
            _anchor_memo.popitem(last=False)
    return out



def _derive_split_anchors(
    search_query: str,
    region_code: str | None,
    local_table: str | None,
    alt_table: str | None,
    region_population: dict[str, int] | None,
) -> list[dict]:
    """The uncached body of _fallback_anchor_details for a query no
    division matched: which of the caller's own words locate it."""
    tokens = search_query.strip().split()
    pop = region_population or {}

    # Splits to try, in precedence order. First the trailing one/two words —
    # the "PLACE NAME + CITY" shape this heuristic was built for. Then, only
    # against a local divisions table (where a lookup is a local parquet read
    # rather than a remote scan we must not multiply), each *leading* word on
    # its own.
    #
    # #268: "Stanford Shopping Center" has no city suffix at all. Its head is
    # the location word and its tail is part of the mall's own name — and
    # "Center" matches Center, Pennsylvania, which aimed a Palo Alto query at
    # Pittsburgh and answered nothing after 61s. Generic tails (Center, Park,
    # Plaza, Village, Springs) are common division names *and* common
    # place-name endings, so trailing-first must not mean trailing-only.
    # Leading candidates are gated to real words so a stopword head ("the
    # Met") can never become an anchor of its own.
    splits = [
        (" ".join(tokens[-n:]), " ".join(tokens[:-n]).strip(), False)
        for n in (2, 1)
        if len(tokens) > n
    ]
    if local_table is not None and len(tokens) <= _pkg._MAX_ANCHOR_TOKENS:
        for i, token in enumerate(tokens[:-1]):
            if (
                len(token) >= 3
                and not _pkg._nothing_but_generic(token)
                and token.lower().strip(".,") not in _pkg._NAME_PREFIX_WORDS
            ):
                splits.append((token, " ".join(tokens[:i] + tokens[i + 1:]).strip(), True))
            # ...and the pair starting here, because a place name's location
            # half is usually two words ("San Jose", "New York", "Santa
            # Clara") and neither word locates anything alone. Only leading
            # pairs: the trailing ones are already covered above.
            if i + 2 <= len(tokens) - 1:
                pair = " ".join(tokens[i:i + 2])
                splits.append((pair, " ".join(tokens[:i] + tokens[i + 2:]).strip(), True))

    # alt_table is the load-bearing part of these lookups: the location token
    # is a *city name as the user writes it*, and for many big cities that is
    # an alternate spelling — Japan's Tokyo is primarily 東京都, so a
    # primary-names-only lookup for "Tokyo" does not even contain the row
    # every user means, and the measured failure was "Shibuya Crossing Tokyo"
    # anchoring on a small Papua New Guinea division named Tokyo (the only
    # primary-name match), aiming the bounded places search at the wrong
    # hemisphere. Ranking within one candidate's rows is _rank_key, which
    # orders by each row's own population first; the region_population map
    # (passed down from geocode's main path rather than re-scanned here) only
    # breaks region-level ties.
    contenders: list[dict] = []
    for precedence, (candidate, base, is_leading) in enumerate(splits):
        if base and candidate.strip().lower().strip(".,") in _pkg._GENERIC_PLACE_WORDS:
            # A feature noun, with the rest of the query still unexplained:
            # this word describes the place, it does not locate it (#268).
            continue
        rows = _pkg._query_divisions(candidate, region_code, local_table, alt_table=alt_table)
        if is_leading:
            # A leading word is the *speculative* reading — the query shape it
            # exists for ("Stanford Shopping Center") is the exception, not the
            # rule — so it has to name a division outright to displace the
            # trailing-suffix reading. Substrings need not apply: "Griffith
            # Observatory Los Angeles" split on the fragment "Los", which
            # substring-matched its way to Österreich, whose 8.9M population
            # then beat the 4.0M of the Los Angeles the query actually named.
            rows = [r for r in rows if _pkg._effective_tier(r, candidate) >= 3]
        if not rows:
            continue
        row = _pick_anchor_row(rows, candidate, pop)
        if (
            not is_leading
            and _anchor_is_weak(row, candidate, search_query)
            and len(base.split()) > len(candidate.split())
        ):
            # #464: the trailing words are a fragment of some division's
            # longer name, that name is nowhere in the query, and most of
            # the query is still unexplained — "of America" inside
            # "Tradiations of America" does not make "Mall of America" a
            # query about Virginia. Not a location word; not an anchor.
            continue
        contenders.append({
            "row": row, "candidate": candidate, "base": base,
            "precedence": precedence,
            "broad": _pkg._SUBTYPE_WEIGHT.get(row.get("subtype"), 2) <= 1,
            "leading": is_leading,
        })
        # The same word's next two cities, as lower-precedence contenders:
        # ambiguous city names lose coin flips — "cambridge" is the UK's,
        # Ontario's and Massachusetts's, and the answer to "harvard square
        # cambridge" sits in the THIRD of those — and the retry-on-empty
        # path can only try cities that exist in this list at all. Distinct
        # region each: a city's land/water polygon variants are not an
        # alternate.
        seen_regions = {(row.get("country"), row.get("region"))}
        alt_rank = 0
        for _ in range(2):
            alt_rows = [
                r for r in rows
                if (r.get("country"), r.get("region")) not in seen_regions
            ]
            if not alt_rows:
                break
            alt = _pick_anchor_row(alt_rows, candidate, pop)
            seen_regions.add((alt.get("country"), alt.get("region")))
            alt_rank += 1
            contenders.append({
                "row": alt, "candidate": candidate, "base": base,
                "precedence": precedence + 100 * alt_rank,
                "broad": _pkg._SUBTYPE_WEIGHT.get(alt.get("subtype"), 2) <= 1,
                "leading": is_leading,
            })
    ranked = _rank_anchor_contenders(contenders)
    if not ranked:
        return []
    out = []
    for c in ranked:
        # #472: "Station" left over from "Shibuya Station" is as empty a
        # thing to search for as "the" -- see _nothing_but_generic.
        name_query = None if _pkg._nothing_but_generic(c["base"]) else c["base"]
        out.append({
            "lat": c["row"]["lat"], "lon": c["row"]["lon"], "name_query": name_query,
            "candidate": c["candidate"], "split": True,
            # #464: see _fallback_anchor_candidates. Trailing, city-level,
            # and the caller typed the division's name (exact/prefix, or
            # the whole matched name sits in the query): the anchor words
            # are a location, not part of the place's name.
            "strong": (
                not c["leading"]
                and not c["broad"]
                and not _anchor_is_weak(c["row"], c["candidate"], search_query)
            ),
        })
    return out



def _anchor_is_weak(row: dict, candidate: str, search_query: str) -> bool:
    """#464: whether `row` matched the split words `candidate` only as a
    substring of a longer division name that the query does not contain.

    "de Janeiro" is a substring of "Rio de Janeiro", but the caller typed
    "Copacabana Rio de Janeiro" — the whole name is there, the split just
    landed a word short, and the anchor is as good as an exact one. "of
    America" is a substring of "Tradiations of America", which appears
    nowhere in "Mall of America": the trailing words coincide with a
    fragment of an unrelated name.
    """
    if _pkg._effective_tier(row, candidate) >= _pkg._STRONG_TIER:
        return False
    q = _pkg._normalize_for_match(search_query)
    for name in (row.get("_matched_name"), row.get("name")):
        if name and _pkg._normalize_for_match(name) in q:
            return False
    return True



# When two splits disagree on which word is the location, more of the query
# matching is stronger evidence — but only between comparably prominent
# places. "palo alto caltrain" offers "Palo Alto" (68k) and "Palo" (Leyte,
# ~70k): comparable, so the longer match wins. "notre dame paris" offers
# "Notre Dame" (an Indiana CDP, ~8k) and "Paris" (2.1M): the shorter
# candidate is ~260x more prominent, and a two-word coincidence does not
# outrank a world city. The ratio is the boundary between those two cases —
# generous, because the longer reading is usually right when it exists at
# all.
_ANCHOR_LONGER_MATCH_POP_RATIO = 50



def _anchor_contender_better(challenger: dict, incumbent: dict) -> bool:
    """Whether `challenger` should anchor instead of `incumbent`.

    Specificity first (a city beats the state sharing its name — "Times
    Square New York" must not anchor near Utica), then the longer-match rule
    bounded by prominence, then population, then split order.
    """
    if challenger["broad"] != incumbent["broad"]:
        return not challenger["broad"]
    ch_len = len(challenger["candidate"].split())
    in_len = len(incumbent["candidate"].split())
    ch_pop = challenger["row"].get("population") or 0
    in_pop = incumbent["row"].get("population") or 0
    if ch_len != in_len:
        longer, shorter = (
            (challenger, incumbent) if ch_len > in_len else (incumbent, challenger)
        )
        longer_pop = longer["row"].get("population") or 0
        shorter_pop = shorter["row"].get("population") or 0
        longer_wins = shorter_pop <= max(longer_pop, 1) * _ANCHOR_LONGER_MATCH_POP_RATIO
        return (challenger is longer) == longer_wins
    if ch_pop != in_pop:
        return ch_pop > in_pop
    return challenger["precedence"] < incumbent["precedence"]



def _rank_anchor_contenders(contenders: list[dict]) -> list[dict]:
    """Best anchor first, by repeated selection with the pairwise rule —
    the longer-vs-prominence comparison is not a total order, so this is a
    tournament rather than a sort key. n is the split count (single digits).
    """
    remaining = list(contenders)
    ranked = []
    while remaining:
        best = remaining[0]
        for c in remaining[1:]:
            if _anchor_contender_better(c, best):
                best = c
        ranked.append(best)
        remaining.remove(best)
    return ranked



def _fallback_anchor(*args, **kwargs):
    """The best anchor candidate, or None — the shape every existing caller
    takes. _fallback_anchor_candidates carries the full ranked list for the
    one caller that retries on an empty anchored result (#272: "harvard
    square cambridge" anchored on Cambridge, UK, found nothing, and gave up
    with Cambridge, Massachusetts sitting in second place)."""
    candidates = _pkg._fallback_anchor_candidates(*args, **kwargs)
    return candidates[0] if candidates else None



def _names_a_feature(query: str) -> bool:
    """Whether the query names a *thing* — a tower, a terminal, a museum —
    rather than a populated place. Divisions are never called these, so a
    lookup against the divisions theme can only come back empty."""
    words = {w.strip(".,").lower() for w in query.split()}
    return bool(words & _pkg._GENERIC_PLACE_WORDS)



def _pick_anchor_row(rows: list[dict], query: str, region_population: dict[str, int]) -> dict:
    """The best of `rows` to use as a point to *search near*, rather than the
    best one to return as an answer.

    The difference is that a state or a country is a fine answer to "where is
    Kansas" and a useless anchor for anything: the places fallback searches
    _PLACES_FALLBACK_RADIUS_M around the point, and a region's centroid is
    usually nowhere near the city that shares its name. #268 measured it —
    "Times Square New York" anchored on New York *State* (pop 20.2M, centroid
    near Utica) and searched 250 km from Manhattan, returning nothing.

    So a substantially-populated city outranks the region it sits in. The
    "substantially" matters: it must not let any namesake hamlet displace a
    genuine region, which is the failure mode in the other direction (a
    locality named Tokyo somewhere must never displace 東京都 as the anchor
    for "Tokyo"). _ANCHOR_SPECIFIC_SHARE is what separates New York City's
    43% of its state from a 0.04% coincidence.
    """
    def _best(group):
        return min(group, key=lambda r: _pkg._rank_key(r, query, region_population), default=None)

    def _is_broad(row):
        return _pkg._SUBTYPE_WEIGHT.get(row.get("subtype"), 2) <= 1

    broad = _best([r for r in rows if _is_broad(r)])
    specific = _best([r for r in rows if not _is_broad(r)])
    if specific is None:
        return broad
    if broad is None:
        return specific
    if (specific.get("population") or 0) >= _pkg._ANCHOR_SPECIFIC_SHARE * (broad.get("population") or 0):  # noqa: E501
        return specific
    return broad



def _query_places_multi_anchor(
    query: str, anchors: list[tuple[float, float]], also: str | None = None
) -> tuple[list[dict], tuple[float, float] | None]:
    """The places fallback across several candidate cities in ONE statement.

    #272: an ambiguous city name's candidates span continents — "cambridge"
    is the UK's, Ontario's and Massachusetts's, and "harvard square
    cambridge" anchored on the wrong two before running out of retries.

    Shape matters more than it looks: the first draft OR-ed the per-city
    (bbox AND distance) groups into one WHERE, and a disjunction of trig
    predicates defeats parquet row-group pruning — the scan read three
    places files nearly whole, 73.8s traced. UNION ALL branches keep each
    city's predicate a simple prunable conjunction with its own
    manifest-pruned file list, and DuckDB runs the branches in one go: the
    cost of N small bounded scans sharing one statement, not one huge
    unprunable one.

    Returns (rows, winning_anchor) — the anchor nearest the top row, so the
    caller can rank distances against the city that actually answered.
    """
    glob = _pkg.overture.upstream_glob(theme="places", type_="place")
    cols = _pkg.overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return [], None

    name_filters = ["names.primary ILIKE $pattern ESCAPE '\\'"]
    params: dict = {"pattern": f"%{_pkg.overture._like_escape(query)}%"}
    if also and also != query:
        name_filters.append("names.primary ILIKE $pattern_full ESCAPE '\\'")
        params["pattern_full"] = f"%{_pkg.overture._like_escape(also)}%"
    tokens = [t for t in _pkg._significant_tokens(query) if len(t) >= 3][:8]
    if len(tokens) >= 2:
        for i, token in enumerate(tokens):
            params[f"tok{i}"] = f"%{_pkg.overture._like_escape(token)}%"
        name_filters.append(
            "(" + " AND ".join(
                f"names.primary ILIKE $tok{i} ESCAPE '\\'" for i in range(len(tokens))
            ) + ")"
        )
    name_clause = "(" + " OR ".join(name_filters) + ")"
    category_expr = "taxonomy.primary" if cols is not None and "taxonomy" in cols else "NULL"

    branches = []
    for n, (lat, lon) in enumerate(anchors[:4]):
        bbox_f, dist_f, geo_params, bbox, _r = _pkg.overture.area_geometry(
            lat, lon, _pkg._PLACES_FALLBACK_RADIUS_M
        )
        # area_geometry's fragments use fixed param names; suffix them so N
        # branches coexist in one statement.
        for key, value in list(geo_params.items()):
            bbox_f = bbox_f.replace(f"${key}", f"${key}_a{n}")
            dist_f = dist_f.replace(f"${key}", f"${key}_a{n}")
            params[f"{key}_a{n}"] = value
        from_source = (
            manifest.pruned_source_sql(glob, bbox)
            or f"read_parquet('{glob}', hive_partitioning=1)"
        )
        branches.append(f"""
            SELECT id, names.primary AS name, bbox.ymin AS lat, bbox.xmin AS lon,
                   coalesce(confidence, 0) AS confidence,
                   {category_expr} AS category
            FROM {from_source}
            WHERE {name_clause} AND {bbox_f} AND {dist_f}""")
    sql = f"""
        SELECT * FROM ({" UNION ALL ".join(branches)})
        ORDER BY confidence DESC
        LIMIT {_pkg.DIVISION_OVERFETCH}
    """
    try:
        with trace.scan(
            "places name scan (multi-anchor)", bounded=True,
            source=f"places/place x{len(branches)} cities", anchors=len(branches),
        ), _pkg.overture._conn_lock:
            rows = _pkg.overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise _pkg.overture.UpstreamUnavailable(str(e)) from e
    result = [{
        "id": r[0], "name": r[1], "subtype": "place",
        "country": None, "region": None,
        "lat": round(r[2], 6), "lon": round(r[3], 6),
        "admin_context": [], "population": None,
        "_confidence": r[4], "category": r[5],
    } for r in rows]
    if not result:
        return [], None
    best = max(result, key=lambda r: r["_confidence"])
    winner = min(
        anchors[:4],
        key=lambda a: geo.haversine_m(a[0], a[1], best["lat"], best["lon"]),
    )
    return result, winner



def _schedule_places_tiles_near(anchor: tuple[float, float]) -> None:
    """Schedule the places (and recreation-layer base) tiles for the
    fallback box around `anchor` — the post-hoc half of #476's
    schedule-only-the-winner rule. Same resolution overture.find_places
    runs before every scan (cache.source_sql through _places_source), so
    the tiles that land are exactly the ones a repeat query reads. Never
    raises: scheduling is an optimization, the answer is already in hand.
    """
    try:
        _bbox_f, _dist_f, _params, bbox, _r = _pkg.overture.area_geometry(
            anchor[0], anchor[1], _pkg._PLACES_FALLBACK_RADIUS_M
        )
        _pkg.overture._places_source(bbox)
    except Exception:  # noqa: BLE001 - cache scheduling must never fail a query
        # The anchor is a coordinate the caller typed a place name for; keep it
        # out of the log line (CodeQL: clear-text logging of location data).
        _pkg.logger.debug("post-hoc tile scheduling failed for a fallback anchor", exc_info=True)



def _query_places_fallback(
    query: str, anchor: tuple[float, float] | None = None, also: str | None = None,
    schedule_tiles: bool = True,
) -> list[dict]:
    """Supplement divisions with named places when divisions alone don't fill limit.

    schedule_tiles=False (#476) reads the anchor's box without scheduling
    its missing places/base tiles for background materialization — cached
    tiles still serve, and a miss falls through to the manifest-pruned
    upstream files as always, so the answer is the same either way. The
    caller passes False for a speculative anchor (one derived by splitting
    the query rather than stated by the caller or matched outright) and
    schedules afterwards for the anchor that actually answered.

    #83: an unconstrained ILIKE scan over the places theme — Overture's
    largest, least row-group-prunable theme — measured 100s+ live against a
    query with no bbox pushdown to exploit (a global name search touches
    every row, worldwide). `anchor` (lat, lon), when the caller
    (geocode(), via _fallback_anchor) can derive one, bounds the search to a
    vicinity via the same bbox+distance predicate every other point-radius
    query in this codebase already uses (overture.area_geometry) — same fix
    resolve_place already applies to its own places search. Without an
    anchor, this still runs (row-capped by the existing LIMIT, same as
    before #83) rather than dropping a genuine name-only query to nothing.

    Reads through overture._with_recreation so the recreation layer
    (docs/RECREATION.md) is in scope here too: a playground find_places
    returns but geocode/resolve_place can't name is a surface-dependent
    answer, which is worse than not having the layer. The anchor bbox —
    when there is one — is passed through to the layer, so its reads are
    bounded (and tile-cached) exactly like find_places' are; an unanchored
    name search has no box to bound by, and there the layer serves only
    from base-theme tiles already on disk (or a pinned local dataset)
    rather than paying two unprunable scans of the live base theme per
    lookup — see recreation._from_source.
    """
    glob = _pkg.overture.upstream_glob(theme="places", type_="place")
    cols = _pkg.overture.probe_schema(glob)
    if cols is not None and "names" not in cols:
        return []
    # `also` (#268) is the *whole* user query, OR'd alongside the residual
    # `query` the anchor split left behind. When the location word is part of
    # the place's own name — "Stanford Shopping Center", where anchoring on
    # Stanford leaves the residual "Shopping Center" — the residual alone
    # matches every mall in the metro and ranks one of them first. Matching
    # both patterns costs nothing (same scan, same box) and lets the caller
    # rank a whole-query hit above a residual-only one.
    alternatives = ["names.primary ILIKE $pattern ESCAPE '\\'"]
    params: dict = {"pattern": f"%{_pkg.overture._like_escape(query)}%"}
    if also and also != query:
        alternatives.append("names.primary ILIKE $pattern_full ESCAPE '\\'")
        params["pattern_full"] = f"%{_pkg.overture._like_escape(also)}%"

    # #270: every-token-present, as a last alternative. A substring match
    # requires the user to reproduce the name contiguously, and real queries
    # drop a word out of the middle constantly: "BASIS Silicon Valley Lower
    # School" is not a substring of "BASIS Independent Silicon Valley Lower
    # School", so the place the user was plainly asking for came back empty
    # while the same query with "Independent" restored found it instantly.
    # Requiring each significant word *somewhere* in the name survives that,
    # and costs nothing extra: it is more predicates on the one scan that was
    # already running, over the same box.
    #
    # Two or more tokens only. A single-token AND is just the substring match
    # again, and a common single word ("school") would match a large fraction
    # of the places in the anchor's box and rank noise above nothing.
    # From the residual, never the whole query: the whole query still carries
    # the city the anchor was split off from ("... Lower School Sunnyvale"),
    # and no school's name contains the city it sits in — ANDing that token in
    # rejects every real match.
    tokens = [t for t in _pkg._significant_tokens(query) if len(t) >= 3][:8]
    if len(tokens) >= 2:
        for i, token in enumerate(tokens):
            params[f"tok{i}"] = f"%{_pkg.overture._like_escape(token)}%"
        alternatives.append(
            "(" + " AND ".join(
                f"names.primary ILIKE $tok{i} ESCAPE '\\'" for i in range(len(tokens))
            ) + ")"
        )
    filters = ["(" + " OR ".join(alternatives) + ")"]
    anchor_bbox = None
    if anchor is not None:
        lat, lon = anchor
        bbox_filter, distance_filter, geo_params, anchor_bbox, _radius_m = _pkg.overture.area_geometry(  # noqa: E501
            lat, lon, _pkg._PLACES_FALLBACK_RADIUS_M
        )
        filters += [bbox_filter, distance_filter]
        params.update(geo_params)
    # Anchored: cache.source_sql resolves tiles-else-manifest-else-glob, so
    # the search reads only the files/tiles its box touches instead of
    # paying the per-file footer pass over the whole places theme (the cost
    # the manifest layer exists to remove — and this is the path the
    # anchored "Shibuya Crossing Tokyo" fix drives traffic into).
    # Unanchored: the glob, deliberately. source_sql with no bbox serves
    # cached tiles alone whenever any exist, which would silently narrow a
    # worldwide name search to whatever areas this install happens to have
    # touched — an empty answer for a place the dataset contains. The
    # unanchored scan's cost is already governed by its own gate
    # (_skip_unanchored_places_scan), not by the cache.
    if anchor_bbox is not None:
        try:
            base_source = _pkg.cache.source_sql(
                "places", glob, anchor_bbox, schedule_missing=schedule_tiles
            )
        except Exception:  # noqa: BLE001 - cache resolution is an optimization here
            base_source = f"read_parquet('{glob}', hive_partitioning=1)"
    else:
        base_source = f"read_parquet('{glob}', hive_partitioning=1)"
    from_source, _ = _pkg.overture._with_recreation(
        base_source, anchor_bbox, schedule_missing=schedule_tiles
    )
    category_expr = "taxonomy.primary" if cols is not None and "taxonomy" in cols else "NULL"
    sql = f"""
        SELECT id, names.primary AS name, bbox.ymin AS lat, bbox.xmin AS lon,
               coalesce(confidence, 0) AS confidence,
               {category_expr} AS category
        FROM {from_source}
        WHERE {' AND '.join(filters)}
        ORDER BY confidence DESC
        LIMIT {_pkg.DIVISION_OVERFETCH}
    """
    try:
        # Bounded exactly when an anchor gave us a box to search inside; the
        # unanchored case is the planet-wide name scan #105 gates against.
        with trace.scan(
            "places name scan", bounded=anchor_bbox is not None, source=from_source,
            anchored=anchor is not None,
        ), _pkg.overture._conn_lock:
            rows = _pkg.overture.conn().execute(sql, params).fetchall()
    except duckdb.Error as e:
        raise _pkg.overture.UpstreamUnavailable(str(e)) from e
    result = []
    for r in rows:
        result.append({
            "id": r[0], "name": r[1], "subtype": "place",
            "country": None, "region": None,
            "lat": round(r[2], 6), "lon": round(r[3], 6),
            "admin_context": [], "_confidence": r[4],
            "_category": r[5],
        })
    return result
