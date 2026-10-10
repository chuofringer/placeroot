"""resolve_place(): merges division and places results into one typed, ranked list."""

import sys as _sys
from collections.abc import Callable

from placeroot import geo

_pkg = _sys.modules["placeroot.geocode"]


def resolve_place(
    query: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
    limit: int = 3,
    city: str | None = None,
    lang: str | None = None,
    country: str | None = None,
) -> list[dict]:
    """Free-text place reference -> ranked, typed GERS ids an agent can hold onto.

    The point of this tool: "the Whole Foods on Lamar" or "Travis County"
    are free text, not stable references — resolve_place turns either shape
    into a GERS id a caller can pass to place_details/other tools later.
    Merges two sources: geocode()'s division matches (a name/region/county/
    country), and find_places searches over the places theme (a business or
    POI), one per significant word in the query (so a query that names a
    place plus extra context — "Mañana coffee Austin" — still finds a place
    literally named just "Mañana Coffee") — bbox-limited to near_lat/near_lon
    if given, else to a 20km vicinity around the top division match (so a
    location hint isn't required when the query itself names a place, e.g.
    "Travis County, TX"). Place candidates unrelated to the query beyond
    incidentally sharing one word with something nearby are dropped, not
    just down-ranked — see _place_match_label.

    Each candidate: {"id" (GERS), "kind": "division" | "place", "name",
    "lat", "lon", "match": "exact" | "prefix" | "contains" | "substring" |
    "fuzzy", plus "admin_context" (division) or "category" (place)}.
    "contains" (#475, places only) means the name contains the whole query
    or the query contains the whole name; "substring" for a place can mean
    as little as one shared significant word, and for a division the usual
    literal substring. "fuzzy" (#215 for divisions, #373 for places) means
    the name doesn't contain the query at all and was reached by close
    spelling instead — the caller asked for one string and is being handed
    the answer to another, so it ranks below every literal label. A place
    candidate reached through #373's fallback tiers (an alt-spelling or
    fuzzy match on the underlying find_places call) additionally carries
    "matched_by": "alt_name" | "fuzzy", absent on an ordinary literal match.
    Ranked by match tier first — kind-agnostic, an exact place beats a
    prefix-matched division — then by prominence
    (division rank_score / place confidence, both roughly 0-1 scales), then
    id for determinism. Never more than `limit` results.

    One exception to tier-first (#481): when the query hit the bundled
    landmark alias table, candidates within _ALIAS_PIN_RADIUS_M of that
    curated pin rank ahead of every other candidate, whatever their tier.
    The alias coordinate is the strongest evidence this module has about
    where the caller means — "notre dame paris" pinned on the cathedral
    must answer the cathedral, not a same-named parish church 4 km away
    that happened to earn a prefix label. Tier, distance and prominence
    still order the pinned rows among themselves; a pin with nothing
    inside its radius changes nothing.

    No match is a valid answer, not an error: an unresolvable query returns
    an empty list. Raises overture.UpstreamUnavailable if a remote scan
    fails after retries, or overture.SchemaDegraded if the places dataset
    is missing bbox — the caller (server.py) turns either into a structured
    error like every other tool.

    #469: a query that ends in a generic type word — "Shibuya Station",
    "Yoyogi Park", "Heathrow Airport" — names a *kind* of feature plus the
    word that picks it out, and Overture files the kind under a category
    rather than in the name: the station is "Gare de Shibuya", category
    train_station, and nothing is literally named "Shibuya Station". So
    alongside the name scans, one bounded find_places scan filtered to that
    kind's categories and to the distinctive word runs from the same
    reference (_TYPE_WORD_CATEGORIES, _type_scan_rows). A row it returns
    whose name covers every distinctive word has answered both halves of
    the query — the category is the type word — and is graded "exact";
    among such rows the one with other rows of its kind within
    _TYPE_SCAN_SUPPORT_RADIUS_M ranks first, because a station is many rows
    (its exits, lines, operators) while a mis-pinned duplicate stands alone.
    The relatedness gate (_place_match_label) meanwhile no longer accepts
    the type word itself as the shared word, so when nothing of the kind is
    found the answer is [] — which server.py turns into `need: location` —
    rather than the nearest business with "Station" in its name.

    lang (#410) requests Overture's language-tagged names.common variant,
    the same as geocode() — but only for "kind": "division" candidates
    (threaded through the internal geocode() call this function already
    makes). "kind": "place" candidates come from find_places, which is out
    of scope for #410 this round (the same scope line find_places' own
    docstring draws), so they always carry their primary name. A division
    candidate's `name_primary` is present under the same rule as
    geocode()'s: only when the variant actually differs from the primary.

    #464: with no near/city hint and no division matching the whole query,
    the reference is a *guess* (_fallback_anchor_candidates' split of the
    caller's own words), and the shared-one-word relatedness gate above is
    too loose next to a guess — "Grand Central Terminal" anchored on a
    Chelyabinsk district called Central and returned a restaurant named
    "Grand Royal". Near a split anchor a place must account for every
    significant word of the query (see _fallback_anchor_details' `strong`
    for which words the anchor itself may account for). When nothing
    survives, the query is treated as the bare name it is: one unanchored,
    LIMIT-capped scan for the whole query, keeping exact/prefix rows only,
    under the same remote-dataset gate as geocode()'s own (#105) — so
    against the live S3 release a bare famous name with no bundled alias
    returns [] (the server answers need: location) rather than a place on
    the wrong continent.

    country (#457) composes with `city`: it constrains the divisions half
    of the merge (threaded through the internal geocode() calls this
    function makes), the same ISO 3166-1 filter geocode() itself takes.
    The places half is left unconstrained by it — find_places has no
    country column to filter on here, and the bbox this function already
    derives from `city`/near_lat/near_lon does the same job for that half.
    May raise ValueError for an unrecognized country or one that conflicts
    with a country/region qualifier parsed off `query` itself.
    """
    query = query.strip()
    limit = max(1, min(limit, _pkg.MAX_LIMIT))
    if not query:
        return []
    if country is not None:
        country = _pkg.normalize_country(country)

    # #329: parse a trailing city / POI alias, or reuse the last good city
    # for a POI-shaped query. Infer *before* the LRU lookup so the key
    # includes the effective hint — otherwise "Observation Tower" after
    # Brooklyn is cached under a bare query and replayed after Paris.
    place_query = query
    city_bounded = False
    # #481: the pin _extract_city_hint hands back comes only from the bundled
    # alias table (a plain city split or a caller's near hint yields None
    # here) — kept separately from near_lat/near_lon because the ranking
    # below treats it differently from any other reference.
    alias_pin: tuple[float, float] | None = None
    if city is None and near_lat is None and near_lon is None:
        place_query, inferred_city, inferred_coords = _pkg._extract_city_hint(query)
        if inferred_coords is not None:
            near_lat, near_lon = inferred_coords
            alias_pin = inferred_coords
            city = inferred_city or city
            city_bounded = True
        elif inferred_city:
            city = inferred_city
        elif _pkg._query_is_poi_shaped(query):
            last_city, last_coords = _pkg._last_good()
            if last_city:
                city = last_city
                if last_coords is not None:
                    near_lat, near_lon = last_coords
                    city_bounded = True

    cache_city, cache_lat, cache_lon = city, near_lat, near_lon
    cached = _pkg._resolve_cache_get(query, cache_city, cache_lat, cache_lon, lang, country)
    if cached is not None:
        return cached[:limit]

    # #271: a caller-supplied (or now inferred) city is the location half
    # of the query, stated rather than guessed at. Resolving it first and
    # using its coordinates as the reference skips _fallback_anchor's
    # whole "which of these words is the place" problem — the problem
    # behind every wrong-hemisphere answer this module has had. It is a
    # hint, never an answer: it bounds where the search looks, and every
    # row returned still comes from the data.
    #
    # #344: geocode()'s general ranking orders same-named divisions by raw
    # population, which is right for a bare geocode() call but wrong here —
    # "New York" the state (20.2M) outranks New York City (8.5M) there, and
    # taking hits[0] anchored "Times Square New York" 200km away, on the
    # state's geographic centroid, with the real Times Square outside the
    # city-hint radius from it. A `city` hint's entire meaning is "a city";
    # _pick_city_hint_row prefers the first locality/localadmin-level hit
    # over a same-named region/country one, falling back to the top hit
    # when nothing at that level matched (the hint may genuinely name a
    # country or region and nothing finer).
    # #457: only pass `country` through when set, so the many callers and
    # test doubles that monkeypatch `geocode` with the pre-#457 signature
    # keep working unchanged; the constraint is opt-in either way.
    country_kw = {"country": country} if country is not None else {}
    if city and near_lat is None and near_lon is None:
        try:
            hits = _pkg.geocode(city, limit=5, **country_kw)
        except (_pkg.overture.UpstreamUnavailable, _pkg.overture.SchemaDegraded):
            hits = []
        if hits:
            pin = _pkg._pick_city_hint_row(hits)
            near_lat, near_lon = pin["lat"], pin["lon"]
            city_bounded = True
        else:
            _pkg.logger.info("resolve_place: city hint %r did not resolve; ignoring it", city)

    if near_lat is not None and near_lon is not None:
        city_bounded = True

    # Search the place half when we stripped a trailing city, so
    # "Colosseo Roma" does not return Rome the city as the pin.
    search_query = place_query if city_bounded and place_query != query else query
    if city_bounded and near_lat is not None and near_lon is not None:
        # #476: the pin bounds geocode's own search, not just its output.
        # Filtering afterwards (below, kept as belt-and-braces) had already
        # let the unconstrained pass match "Marina Bay Sands" to five
        # "Marina Bay" neighbourhoods in the US by spelling, anchor its
        # places fallback on Marina, California, and schedule that box's
        # tiles for background download — for a query the caller had
        # pinned to Singapore. With the constraint, divisions outside the
        # city-hint radius are never candidates, and geocode anchors its
        # places search on the pin instead of guessing from the words.
        geocode_hits = _pkg.geocode(
            search_query,
            limit=_pkg._RESOLVE_OVERFETCH,
            lang=lang,
            near=(near_lat, near_lon, _pkg._CITY_HINT_RADIUS_M),
            **country_kw,
        )
        geocode_hits = [
            r
            for r in geocode_hits
            if geo.haversine_m(near_lat, near_lon, r["lat"], r["lon"]) <= _pkg._CITY_HINT_RADIUS_M
        ]
    else:
        geocode_hits = _pkg.geocode(
            search_query, limit=_pkg._RESOLVE_OVERFETCH, lang=lang, **country_kw
        )  # noqa: E501
    division_hits = [r for r in geocode_hits if r["type"] != "place"]

    # #464: None unless the reference below is a split-derived guess; then
    # the query words a place candidate must cover to count (see there).
    split_cover_tokens: list[str] | None = None
    if near_lat is not None and near_lon is not None:
        reference = (near_lat, near_lon)
    elif division_hits:
        reference = (division_hits[0]["lat"], division_hits[0]["lon"])
    else:
        # No division matched the whole query, but the anchor machinery can
        # usually still say where the query means — "plaza mayor madrid"
        # matches no division as a string, yet its anchor is Madrid's centre.
        # Without this the merged ranking has no distance term for exactly
        # the POI-shaped queries that need one, and answered that query with
        # a Plaza Mayor 25 km out of town (#272). One local-index lookup.
        local_table = _pkg._local_divisions_table()
        options = _pkg._fallback_anchor_details(
            query,
            [],
            None,
            local_table,
            alt_table=_pkg._local_alt_names_table(local_table),
            region_population=_pkg._region_population_lookup(local_table),
        )
        reference = (options[0]["lat"], options[0]["lon"]) if options else None
        if options and options[0]["split"]:
            # #464: the reference is a guess at which of the caller's own
            # words locate the query, and _place_match_label's shared-word
            # gate was written for a reference the caller *stated* (a near
            # hint, a city, a division that matched the whole query). Near a
            # guessed one, "shares a word" is exactly the coincidence that
            # produced the anchor in the first place: "Grand Central
            # Terminal" anchored on a Chelyabinsk district called Central
            # and resolved to "Grand Royal, банкет-холл и ресторан" — a
            # restaurant that shares the word "Grand" with the query and
            # nothing else. So a place found near a split anchor must
            # account for every significant word of the query — in its own
            # name, or (when the anchor is strong: the caller typed a
            # city-level division's name) by sitting in the place the
            # anchor's words named. Same containment-or-typo-close test as
            # #374's cover rule, over the whole query rather than a
            # residual.
            exempt = (
                set(_pkg.overture._fold_poi_name(options[0]["candidate"]).split())
                if (options[0]["strong"])
                else set()
            )
            split_cover_full = _pkg._significant_tokens(query)
            split_cover_tokens = [
                t for t in split_cover_full if _pkg.overture._fold_poi_name(t) not in exempt
            ]

    place_rows: list[dict] = []
    gate_tokens: list[str] = []  # #374: set alongside the token loop below
    query_alternates = _pkg._alias_names_for(query) + (
        [search_query] if search_query != query else []
    )
    # #469: the city words split off the query — location context for the
    # gate to set aside, not a word a candidate can be related through.
    context_words = frozenset(t.lower() for t in _pkg._significant_tokens(query)) - frozenset(
        t.lower() for t in _pkg._significant_tokens(search_query)
    )
    # #469: what each of geocode()'s place-kind rows earns from the gate,
    # decided once here — both for the coverage count just below (a row the
    # gate will drop as unrelated is not coverage: before this, ten
    # "...Station" businesses counted as having answered "Shibuya Station"
    # and stood the one scan down that could have) and for the merge
    # further on.
    geocode_place_labels: dict[str, str | None] = {
        r["id"]: _pkg._best_place_label(r["name"], query, query_alternates, context_words)
        for r in geocode_hits
        if r["type"] == "place" and r["id"] and r["name"]
    }
    # geocode()'s own anchored fallback often already searched the places
    # theme near this same reference with the whole query and its tokens —
    # when it came back with enough place-kind rows, re-scanning per token
    # here buys near-duplicates for the price of two more bounded scans
    # ("notre dame paris" measured 11.9s with them, 5s without).
    #
    # "Near this same reference" is load-bearing, not decorative: geocode may
    # have anchored somewhere else entirely — "hoover dam" anchors on Hoover,
    # Alabama, whose %dam% scan returns Adam Cox and Damascus Baptist Church
    # as place hits. Counting those as coverage skipped the one search that
    # would have found the actual dam 200m from the caller's reference.
    geocode_places = sum(
        1
        for r in geocode_hits
        if r["type"] == "place"
        and reference is not None
        and geocode_place_labels.get(r["id"]) is not None
        and geo.haversine_m(reference[0], reference[1], r["lat"], r["lon"])
        <= _pkg._RESOLVE_PLACE_RADIUS_M
    )
    if reference is not None:
        ref_lat, ref_lon = reference
        seen_place_ids: set[str] = set()
        # Not every word deserves its own scan (#272). A feature noun
        # ("square") matches half the businesses in any downtown, and the
        # city word the reference was derived FROM ("cambridge") matches
        # everything named after the city — both are pure noise that then
        # outranked real answers, and each costs a bounded scan. Searching
        # "harvard square cambridge" token-by-token means searching
        # "harvard": the one word that distinguishes the place.
        tokens = [
            t
            for t in _pkg._significant_tokens(search_query)
            if t.lower().strip(".,") not in _pkg._GENERIC_PLACE_WORDS
        ]
        # Phrase first: "harvard square" as one name, not just "harvard"
        # (square is a feature noun and would otherwise be dropped, and
        # the one-word scan then answers Harvard FCU).
        if search_query and search_query.lower() not in {t.lower() for t in tokens}:
            tokens.insert(0, search_query)
        alias_tokens: set[str] = set()
        for alias_name in _pkg._alias_names_for(search_query):
            for tok in alias_name.split():
                if tok not in tokens and tok.lower().strip(".,") not in _pkg._GENERIC_PLACE_WORDS:
                    tokens.append(tok)
                    alias_tokens.add(tok)
        if len(tokens) > 1:
            folded_city = {t.lower() for t in tokens}
            for div in division_hits[:1]:
                # #410: prune against the *primary* name — under a lang
                # override div["name"] may be the localized variant
                # ("Munich"), and the caller's own city word ("München")
                # must still be recognized and pruned.
                div_name = div.get("name_primary") or div.get("name") or ""
                folded_city &= {w.lower() for w in div_name.split()}
            tokens = [t for t in tokens if t.lower() not in folded_city] or tokens
        # #374: the query words a fallback-matched row must account for —
        # the single distinctive tokens that survived the generic/city
        # pruning above, minus alias-derived ones (an alias is an
        # *alternative* spelling of the query, not extra context the name
        # has to contain too). See _fuzzy_place_covers_query.
        gate_tokens = [t for t in tokens if " " not in t and t not in alias_tokens]
        # #469: the query's kind, searched by category on its distinctive
        # word. Runs regardless of coverage — the name scans cannot reach a
        # row filed under the category rather than the English word,
        # however many rows they return. Skipped when no distinctive word
        # survived the pruning ("Station" alone, or "Tokyo Station" once
        # the city word is set aside): a category scan with nothing to
        # match names against is every station in the metro.
        type_slugs = _pkg._type_word_slugs(search_query)
        type_token = gate_tokens[0] if type_slugs and gate_tokens else None
        # perf: these scans used to run one after another, each queued on
        # the global connection lock and each paying tier 1 + alt-name +
        # fuzzy on a miss — the cold half of the c15 corpus leg. Now a
        # first round runs the two scans most likely to settle the query
        # side by side: the whole phrase (all its tiers, as before) and the
        # #469 type scan. When either produced a row the grading below will
        # call _CONFIDENT_PLACE_LABEL, the per-word scans are skipped: a row
        # found by one word alone is at best "prefix", so none of them could
        # have outranked it. Otherwise the second round runs every remaining
        # word side by side with the fuzzy tier off (one word's close
        # spelling is not the answer to a longer query). Results are merged
        # in the serial loop's order — phrase, words, type scan — so the
        # candidate set and ranking are what they were whenever the first
        # round is not confident. Not under an alias pin (#481: the pin, not
        # the label, picks first place) or a guessed anchor (#464: the cover
        # rule may drop the confident row), where every scan runs as before.
        name_tokens = list(tokens) if geocode_places < limit else []
        phrase = name_tokens[0] if name_tokens and name_tokens[0] == search_query else None

        def _name_scan(token: str, fuzzy: bool) -> Callable[[], list[dict]]:
            return lambda: _pkg.overture.find_places(
                ref_lat,
                ref_lon,
                radius_m=_pkg._RESOLVE_PLACE_RADIUS_M,
                name=token,
                limit=_pkg._RESOLVE_OVERFETCH,
                **({} if fuzzy else _pkg._find_places_kwargs(fuzzy_fallback=False)),
            )

        first_round: list[Callable[[], list[dict]]] = []
        if phrase is not None:
            first_round.append(_name_scan(phrase, fuzzy=True))
        if type_token is not None:
            first_round.append(
                lambda: _pkg._type_scan_rows(ref_lat, ref_lon, type_slugs, type_token)
            )  # noqa: E501
        first_rows = _pkg._run_place_scans(first_round)
        phrase_rows = first_rows.pop(0) if phrase is not None else []
        type_rows = first_rows.pop(0) if type_token is not None else []
        confident = (
            alias_pin is None
            and split_cover_tokens is None
            and (
                any(
                    r["name"]
                    and not r.get("matched_by")
                    and _pkg._best_place_label(r["name"], query, query_alternates, context_words)
                    == _pkg._CONFIDENT_PLACE_LABEL
                    for r in phrase_rows
                )
                or any(
                    r["name"] and _pkg._fuzzy_place_covers_query(r["name"], gate_tokens)
                    for r in type_rows
                )
            )
        )
        word_tokens = [] if confident else [t for t in name_tokens if t != phrase]
        word_rows = _pkg._run_place_scans([_name_scan(t, fuzzy=False) for t in word_tokens])
        for token, rows in [(phrase, phrase_rows), *zip(word_tokens, word_rows)]:
            for row in rows:
                if row["id"] and row["id"] not in seen_place_ids:
                    seen_place_ids.add(row["id"])
                    if row.get("matched_by"):
                        # Which token the #373 fallback actually matched
                        # this row against — the #374 gate below trusts
                        # the fuzzy label only when that was the whole
                        # query.
                        row["_matched_token"] = token
                    place_rows.append(row)
        for row in type_rows:
            if row["id"] and row["id"] not in seen_place_ids:
                seen_place_ids.add(row["id"])
                place_rows.append(row)
                continue
            # Already in hand from a name scan — carry the tag over so
            # the grading below sees the row for the kind it is.
            for held in place_rows:
                if held["id"] == row["id"]:
                    held["_type_scan"] = True
                    break

    # #481: the alias pin is a curated coordinate for a specific landmark,
    # so look at what actually stands there. The name-filtered scans above
    # can miss it entirely — "Cathédrale Notre-Dame de Paris" is not a LIKE
    # match for "notre dame" or "notre dame cathedral", and when geocode()
    # already came back with enough same-named shops those scans are
    # skipped anyway — leaving nothing inside the pin radius for the
    # pinned-first sort to promote. One nearest-first scan bounded to the
    # pin radius (warm tiles, no name filter); every row still has to earn
    # a label against the query or an alias spelling below, so the shops
    # and bus stops sharing the square are dropped as unrelated, exactly as
    # a per-token hit would be.
    if alias_pin is not None:
        pinned_ids = {r["id"] for r in place_rows}
        for row in _pkg.overture.find_places(
            alias_pin[0],
            alias_pin[1],
            radius_m=_pkg._ALIAS_PIN_RADIUS_M,
            limit=_pkg._ALIAS_PIN_SCAN_LIMIT,
        ):
            if row["id"] and row["id"] not in pinned_ids:
                pinned_ids.add(row["id"])
                place_rows.append(row)

    candidates = []
    seen_ids: set[str] = set()
    for r in division_hits:
        if not r["id"] or r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])
        candidate = {
            "id": r["id"],
            "kind": "division",
            "name": r["name"],
            "lat": r["lat"],
            "lon": r["lon"],
            "type": r.get("type"),
            "admin_context": r["admin_context"],
            # #410: label (and therefore rank) off the primary name — the
            # caller's query was written against it, and grading "München"
            # against a lang-swapped "Munich" would demote the correct
            # division to a substring match below coincidentally-named
            # places.
            "match": _pkg._division_match_label(
                {**r, "name": r.get("name_primary") or r["name"]}, query, search_query
            ),
            "_prominence": r["rank_score"],
        }
        if r.get("name_primary"):
            # #410: geocode()'s own lang enrichment already applied — just
            # carry it through the merge rather than re-deriving it.
            candidate["name_primary"] = r["name_primary"]
        candidates.append(candidate)
    for r in place_rows:
        if not r["id"] or r["id"] in seen_ids or not r["name"]:
            continue
        # #373: a row overture.find_places tagged as an alt-name/fuzzy
        # fallback hit doesn't contain the typo the caller typed and would
        # be dropped as unrelated by _place_match_label's containment
        # check. But the tag only certifies similarity to the STRING IT WAS
        # SEARCHED WITH (#374): trusted outright only when that was the
        # whole query; a row matched against one token of a multi-token
        # query must additionally cover the rest of the query's distinctive
        # words, or a one-token fuzzy coincidence (a bar named "King")
        # would resolve the whole of "Kings Barbershop Chicago".
        if r.get("matched_by"):
            matched_token = (r.get("_matched_token") or "").lower()
            if matched_token in {query.lower(), search_query.lower()}:
                label = "fuzzy"
            elif _pkg._fuzzy_place_covers_query(r["name"], gate_tokens):
                label = "fuzzy"
            else:
                continue
        else:
            label = _pkg._best_place_label(r["name"], query, query_alternates, context_words)
        # #469: a row of the query's own kind whose name covers every
        # distinctive word has matched the whole query — the category
        # stands in for the type word ("Gare de Shibuya" + train_station is
        # "Shibuya Station"). Graded exact so it outranks every name-only
        # reading, however that reading was labelled.
        if r.get("_type_scan") and _pkg._fuzzy_place_covers_query(r["name"], gate_tokens):
            label = "exact"
        if label is None:
            continue
        if split_cover_tokens is not None and not _pkg._fuzzy_place_covers_query(
            r["name"], split_cover_tokens
        ):
            continue  # #464: shares a word with the query, near a guessed anchor
        seen_ids.add(r["id"])
        candidate = {
            "id": r["id"],
            "kind": "place",
            "name": r["name"],
            "lat": r["lat"],
            "lon": r["lon"],
            "category": r["category"],
            "match": label,
            "_prominence": r.get("confidence") or 0.0,
            "_type_scan": bool(r.get("_type_scan")),
        }
        if r.get("matched_by"):
            candidate["matched_by"] = r["matched_by"]
        candidates.append(candidate)
    # geocode() can resolve a place on its own — its anchored fallback
    # handles "Shibuya Crossing Tokyo" by splitting the trailing city off
    # and searching places around it. Discarding those hits (the previous
    # behavior kept only division hits) meant resolve_place returned
    # nothing for a query geocode could answer: a place resolver that
    # loses to the plain geocoder on place queries. Merge them in with the
    # same relatedness gate the near-reference path uses.
    for r in geocode_hits:
        if r["type"] != "place" or not r["id"] or r["id"] in seen_ids or not r["name"]:
            continue
        label = geocode_place_labels.get(r["id"])
        if label is None:
            continue
        if split_cover_tokens is not None:
            # #464: geocode()'s fallback guessed an anchor the same way this
            # function did ("Mall of America" -> a Virginia division named
            # "...of America", whose %Mall% scan returned "Mercy Mall of
            # VA"); its rows are held to the same whole-query rule. The
            # anchor's own words are only accounted for by *location* for a
            # row that is actually near this function's anchor — geocode()
            # may have picked a different namesake (traced: a Bolivian
            # hamlet named America here, Virginia there), and a row 6,000 km
            # from the anchor whose word it is excused from containing is
            # excused from nothing.
            near_anchor = reference is not None and (
                geo.haversine_m(reference[0], reference[1], r["lat"], r["lon"])
                <= _pkg._PLACES_FALLBACK_RADIUS_M
            )
            required = split_cover_tokens if near_anchor else split_cover_full
            if not _pkg._fuzzy_place_covers_query(r["name"], required):
                continue
        seen_ids.add(r["id"])
        candidates.append(
            {
                "id": r["id"],
                "kind": "place",
                "name": r["name"],
                "lat": r["lat"],
                "lon": r["lon"],
                "category": r.get("category"),
                "match": label,
                "_prominence": r.get("rank_score") or 0.0,
            }
        )

    if split_cover_tokens is not None and not candidates:
        # #464: the guessed anchor explained nothing — the query is a bare
        # name, and the right thing to do with a bare name is what the
        # #83 docstring already promises one: the unanchored, LIMIT-capped
        # scan of the places theme for the WHOLE query. Only exact/prefix
        # rows count here: the scan is a substring ILIKE, and the one
        # thing this path must never do is hand back "Mercy Mall of VA"
        # for "Mall of America" because both contain "Mall" — no result
        # (and the server's need: location) beats a fast wrong one. Gated
        # exactly as geocode()'s own unanchored scan is (#105): against a
        # remote dataset the scan is a full read of the largest theme, so
        # it does not run and the caller is asked for a location instead.
        reference = None
        if not _pkg._skip_unanchored_places_scan():
            for r in _pkg._query_places_fallback(query):
                if not r["id"] or r["id"] in seen_ids or not r["name"]:
                    continue
                tier = _pkg._match_tier(r["name"], query)
                if tier < _pkg._STRONG_TIER:
                    continue
                seen_ids.add(r["id"])
                candidates.append(
                    {
                        "id": r["id"],
                        "kind": "place",
                        "name": r["name"],
                        "lat": r["lat"],
                        "lon": r["lon"],
                        "category": r.get("_category"),
                        "match": _pkg._MATCH_TIER_LABELS[tier],
                        "_prominence": r.get("_confidence") or 0.0,
                    }
                )

    # Distance to the reference before prominence (#272): the reference is
    # the caller's own statement of where they mean (their near-hint, city
    # hint, or the resolved anchor), and names repeat — "plaza mayor madrid"
    # anchored dead-centre on Madrid and still answered with a Plaza Mayor
    # 25 km out, because that row carried more confidence and this sort had
    # no distance term. Same judgment _rank_place applies in geocode's own
    # fallback, applied to the merged list. Km-rounded so GPS-grade jitter
    # never reorders genuinely co-located candidates.
    #
    # #469: among the rows the type scan returned, agreement before distance.
    # The reference is the *city's* pin, and at metro scale the row nearest
    # it is whichever duplicate happens to be mis-pinned toward downtown; a
    # row with other rows of its kind within _TYPE_SCAN_SUPPORT_RADIUS_M is
    # the real complex. Name-scan rows carry no support and are unaffected
    # relative to one another.
    typed = [c for c in candidates if c.get("_type_scan")]
    for c in candidates:
        c["_support"] = (
            sum(
                1
                for other in typed
                if other is not c
                and geo.haversine_m(c["lat"], c["lon"], other["lat"], other["lon"])
                <= _pkg._TYPE_SCAN_SUPPORT_RADIUS_M
            )
            if c.get("_type_scan")
            else 0
        )

    # #481: when the pin came from the bundled alias table, that pin is a
    # curated landmark coordinate — the strongest evidence the server has
    # about where the query means — and a string-tier heuristic must not
    # outvote it. "notre dame paris" pinned 30 m from the cathedral and still
    # answered Notre Dame de Clignancourt 4.4 km north, because the parish
    # church's name *starts with* the query (prefix) while the cathedral's
    # only contains it (substring), and tier sorts before distance. Every
    # candidate within _ALIAS_PIN_RADIUS_M of the pin is tagged and sorts
    # ahead of all others; tier → distance → prominence still decide among
    # the pinned rows (the cathedral over the museum named after it). When
    # nothing sits inside the radius — the alias points at something the
    # scans missed — no row is tagged and the ordering is unchanged.
    if alias_pin is not None:
        for c in candidates:
            if (
                geo.haversine_m(alias_pin[0], alias_pin[1], c["lat"], c["lon"])
                <= _pkg._ALIAS_PIN_RADIUS_M
            ):
                c["_alias_pinned"] = True

    def _rank_candidate(c):
        near_km = 0.0
        if reference is not None:
            near_km = round(
                geo.haversine_m(reference[0], reference[1], c["lat"], c["lon"]) / 1000.0
            )
        return (
            0 if c.get("_alias_pinned") else 1,
            -_pkg._MATCH_LABEL_RANK[c["match"]],
            -c["_support"],
            near_km,
            -c["_prominence"],
            c["id"],
        )

    candidates.sort(key=_rank_candidate)
    for c in candidates:
        del c["_prominence"]
        del c["_support"]
        c.pop("_type_scan", None)
        c.pop("_alias_pinned", None)
    out = candidates[:limit]
    # The whole ranked list, not `out`: the key carries no limit, and a
    # later call with a larger limit slices the cached list on read.
    _pkg._resolve_cache_put(query, cache_city, cache_lat, cache_lon, candidates, lang, country)
    if out:
        _pkg._remember_last_city(city, out[0])
        _pkg._kick_autowarm(out[0])
    return out
