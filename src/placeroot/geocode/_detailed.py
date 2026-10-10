"""geocode(), geocode_batch() and geocode_detailed(): the division search pipeline."""

import sys as _sys

from placeroot import geo, home_region
from placeroot.geocode._divisions import NearConstraint
from placeroot.geocode._ranking import DEFAULT_LIMIT

_pkg = _sys.modules["placeroot.geocode"]


def geocode(
    query: str, limit: int = DEFAULT_LIMIT, lang: str | None = None, country: str | None = None,
    near: NearConstraint | None = None,
) -> list[dict]:
    """Free-text place name -> ranked candidates. See geocode_detailed.

    country (#457) and near (#476): see geocode_detailed. May raise ValueError.
    """
    return _pkg.geocode_detailed(query, limit, lang=lang, country=country, near=near)["results"]



def geocode_batch(
    queries: list[str], limit_per_query: int = 3, country: str | None = None,
) -> list[dict]:
    """Geocode many names against ONE opened local divisions table (#329).

    Opens the name table (and its alt-name sibling) once, then looks every
    query up against that same path. A two-name walk must not pay N cold
    S3 scans or N table materializations. Each row is the top candidate
    for that query, or the standard error envelope {"query", "error":
    "not_found", "detail"} (roadmap §4, next tier: unified per-row batch
    error shape — a bare "no match" string used to stand in its place);
    input order is preserved. The caller (server.py) applies the 20-query
    cap.

    country (#457) applies the same ISO 3166-1 constraint to every query in
    the batch. May raise ValueError (validated once, up front, rather than
    per query).
    """
    if country is not None:
        country = _pkg.normalize_country(country)
    local_table = _pkg._local_divisions_table()
    alt_table = _pkg._local_alt_names_table(local_table)
    rows = []
    for query in queries:
        hits = _pkg.geocode_detailed(
            query, limit_per_query, local_table=local_table, alt_table=alt_table,
            country=country,
        )["results"]
        if not hits:
            rows.append({
                "query": query,
                "error": "not_found",
                "detail": f"no match for {query!r}",
            })
            continue
        top = hits[0]
        rows.append({
            "query": query,
            "name": top["name"],
            "type": top["type"],
            "lat": top["lat"],
            "lon": top["lon"],
            "id": top["id"],
            "rank_score": top["rank_score"],
        })
    return rows



def geocode_detailed(
    query: str, limit: int = DEFAULT_LIMIT, include_country: bool = False,
    *,
    local_table: str | None = None,
    alt_table: str | None = None,
    lang: str | None = None,
    country: str | None = None,
    near: NearConstraint | None = None,
) -> dict:
    """Free-text place name -> ranked candidates, from Overture divisions (and places fallback).

    Returns {"results": [...]} and, when the places-name half of the search
    is skipped as not worth its cost — no derivable location context to
    bound it by (#105), or nothing but stopwords left to search names for
    (#216) — a "note" saying so and how to make the query answerable.

    A "note" also comes back *with* results when nothing matched literally
    and the answer came from the #215 fuzzy tier instead: it names the
    spelling the results were corrected to ("Berekley" -> "Berkeley"), so
    a caller can tell a correction from a match. Those rows also carry
    "matched_by": "fuzzy" individually, which is what resolve_place reads
    to label them (it returns no note of its own).

    A query that is entirely a postcode ("94110", "1011AB", "SW1A 1AA") is
    answered from the addresses theme instead of by name (#223): one row per
    country carrying that code, `type` "postcode", `id` None, plus `country`
    and `address_count`, with a "note" on both the granularity of a postcode
    centroid and the theme's coverage. See the module docstring's #223
    section.

    Never more than `limit` results. Each result: {name, type, lat, lon, id
    (GERS), admin_context, rank_score, plus "matched_by" on a #215 fuzzy
    row, plus (#214) "matched_name" on a row found through one of
    Overture's alternate names rather than the canonical one — "Munich"
    answers München, with matched_name "Munich"}. Raises
    overture.UpstreamUnavailable if the remote scan fails after retries;
    the caller (server.py) turns that into a structured error like the
    other tools.

    Handles "City, ST" / "City, Region" suffixes (#46) and ranks same-tier
    ties by population/prominence (#47) — see the module docstring.

    include_country adds each row's ISO country code (None for a places-
    fallback row, which has no admin chain to read one off) to the result
    dicts. Off by default, and deliberately not part of the MCP tool's
    payload — it exists for geocode_address, which has to reject a
    runner-up anchor sitting in a different country than the top candidate
    ("London" -> London, Ontario for a UK query) and cannot do that from
    admin_context alone once a row's chain is empty.

    lang (#410, a 2-3 letter code) requests Overture's language-tagged
    names.common variant for each division-kind row, when one exists for
    that id and language: `name` becomes the variant and the primary is
    added back as `name_primary` only when it differs — piggybacked as one
    extra lookup keyed by the batch of result ids, not a scan per row (see
    _lang_variants_for). Places-fallback rows (a row's `_category` is set)
    are unaffected — same scope line find_places itself draws this round.
    No lang given, or no variant found for a row, leaves it byte-identical
    to the no-lang answer.

    country (#457, ISO 3166-1 alpha-2/alpha-3, case-insensitive; aliases
    like "UK"/"USA" also accepted) is the explicit form of the "City,
    Country" suffix parsing below — constrains divisions candidates to
    that country's own `country` column. Raises ValueError if it isn't a
    recognized country code, or if it disagrees with a country/region
    suffix parsed off `query` itself (server.py turns either into a
    structured bad_request naming both).

    near (#476, a (lat, lon, radius_m) triple) is a caller that already
    knows where the query means — resolve_place with a city pin — telling
    this search so. It bounds every division pass (literal, alternate,
    variant, fuzzy) to that box, and it *is* the places-fallback anchor:
    _fallback_anchor_candidates' speculative "which trailing or leading
    word is the city" splits do not run at all, because the caller has
    already answered that question. Measured before: resolve_place("Marina
    Bay Sands Singapore") ran geocode("Marina Bay Sands") unconstrained,
    which anchored on Marina, California (the only division "Marina"
    matches outright), scanned places there, retried five more cities on
    three continents, and scheduled California's places and base-theme
    tiles for background materialization — for a query pinned to
    Singapore. With near, none of that has a reason to happen. No near
    given leaves this function byte-identical to before.
    """
    query = query.strip()
    limit = max(1, min(limit, _pkg.MAX_LIMIT))
    if not query:
        return {"results": []}

    normalized_country = _pkg.normalize_country(country) if country is not None else None

    note = None
    if local_table is None:
        local_table = _pkg._local_divisions_table()

    # #223: a query that is *entirely* a postcode is not a name lookup, and
    # searching division names for "94110" finds nothing by construction. One
    # upstream aggregate over the addresses theme answers it instead, per
    # country carrying the code. A postcode-shaped query that matches nothing
    # still falls through to the name search below -- the shape detector is
    # conservative but not infallible, and a real name that happens to be
    # shaped like a postcode must still be findable.
    #
    # A postcode that *does* match returns here and the name search never
    # runs, which is intended and is the one case where the detector costs
    # something: a division literally named "2100" is unreachable through
    # geocode in a dataset where 2100 is also a live Danish postcode. The
    # alternative -- merging the two halves -- would have to rank a point
    # count against a population on one scale, and would put a namesake
    # village in the middle of an answer about a postal code. Pinned by
    # test_a_postcode_hit_short_circuits_the_name_search.
    #
    # Its note is kept aside
    # (postcode_note) rather than assigned to `note`: when both halves come
    # back empty, "this is what an empty postcode answer means" is the more
    # useful of the two explanations, so it wins at the return below.
    postcode_note = None
    variants = _pkg._postcode_variants(query)
    if variants:
        display = _pkg._postcode_display(query)
        postcode_rows = _pkg._query_postcode_countries(variants)
        if postcode_rows:
            return {
                "results": _pkg._postcode_results(display, postcode_rows, local_table, limit),
                "note": _pkg._postcode_note(display),
            }
        postcode_note = _pkg._postcode_empty_note(display)

    # #214: None whenever there is no alternate-name table to search — cache
    # off, a cache directory predating the feature, a dataset without
    # names.common — in which case every _query_divisions call below is
    # exactly the primary-name-only search it was before.
    if alt_table is None:
        alt_table = _pkg._local_alt_names_table(local_table)
    base_query, region_code, _region_name = _pkg._parse_region_suffix(query, local_table)
    country_code = None
    _country_name = None
    qualifier_note = None
    if region_code is None:
        # #457: only tried once the region parse has failed — "Springfield,
        # IL" and "London, Ontario" never reach this at all, so the
        # region/country parses can't fight over the same suffix.
        base_query, country_code, _country_name = _pkg._parse_country_suffix(
            query, local_table, alt_table
        )
        if country_code is None and normalized_country is None:
            # #457: neither a region nor a country suffix resolved. "Never
            # search the joined string" — a comma-separated qualifier
            # (only a comma counts; see _unrecognized_comma_qualifier) that
            # names nothing this module recognizes still gets set aside,
            # with a note, rather than searching a literal string no
            # division name will ever equal.
            unrecognized = _pkg._unrecognized_comma_qualifier(query)
            if unrecognized:
                base_query, suffix_text = unrecognized
                qualifier_note = _pkg._unrecognized_qualifier_note(suffix_text, base_query)
            else:
                base_query = query

    # Whether country_code (if any) came from parsing `query` itself, as
    # opposed to the explicit `country=` param merged in just below — only
    # a *parsed* country gets the #46-style "degrade to unconstrained on
    # zero candidates" treatment. An explicit country= is a deliberate
    # filter the caller stated on purpose; zero matches inside it is a real
    # answer, not a misparse worth second-guessing.
    # (If both were given they already agree — the conflict check below
    # raises otherwise — and the explicit one wins the "no degrade" rule.)
    country_code_from_suffix = country_code is not None and normalized_country is None

    if normalized_country is not None:
        if country_code is not None and country_code != normalized_country:
            raise ValueError(
                f"country={country!r} conflicts with the parsed qualifier "
                f"{_country_name!r} ({country_code}) in {query!r}"
            )
        if region_code is not None:
            implied = region_code.split("-", 1)[0]
            if implied and implied != normalized_country:
                raise ValueError(
                    f"country={country!r} conflicts with the parsed region "
                    f"qualifier {_region_name!r} ({implied}) in {query!r}"
                )
        country_code = normalized_country

    search_query = base_query if (region_code or country_code or qualifier_note) else query
    # `region_code`/`country_code` are cleared below if a constrained
    # search comes up empty; the #215 fuzzy pass still wants the code the
    # query itself carried, so keep it.
    suffix_region_code = region_code
    suffix_country_code = country_code

    # #476: `near_kw` rather than a bare keyword so the no-constraint call
    # stays exactly the call it was (the pre-#476 signature is what every
    # test double of _query_divisions in the suite answers to).
    near_kw: dict = {"near": near} if near is not None else {}
    # The explicit country= filter, kept on every degrade/retry below: only
    # a qualifier *parsed* off the query is a guess worth withdrawing (the
    # contract stated at country_code_from_suffix). Passed as a kwarg only
    # when set, for the same reason as near_kw.
    explicit_country_kw: dict = (
        {"country_code": normalized_country} if normalized_country is not None else {}
    )
    divisions = _pkg._query_divisions(
        search_query, region_code, local_table, alt_table=alt_table,
        country_code=country_code, **near_kw,
    )
    if region_code and not divisions:
        # #46: recognized a region suffix, but nothing in this dataset
        # matches inside it — degrade to an unconstrained search of the
        # original query rather than returning empty for a query that
        # would otherwise have matched something.
        region_code = None
        country_code = normalized_country
        search_query = query
        divisions = _pkg._query_divisions(
            search_query, None, local_table, alt_table=alt_table,
            **explicit_country_kw, **near_kw,
        )
    elif country_code and not divisions and country_code_from_suffix:
        # #457: same idea, but degrading to the BASE name (not the whole
        # comma-joined string) — Overture names are bare, so re-searching
        # "Springfield, GB" verbatim could never match anything anyway,
        # unlike the region path above which still has today's plain
        # substring behavior to fall back on for a name+suffix combination
        # its own docstring already documents as unconstrained-original.
        #
        # Only for a country parsed off the query itself, not an explicit
        # country= (see country_code_from_suffix above) — a caller-stated
        # filter coming up empty is a real, precise answer.
        #
        # A bare trailing word (no comma — "Portland Jersey") degrades to
        # the whole query instead, like the region path: the base word
        # alone is a far broader search than the caller typed.
        country_code = None
        search_query = base_query if "," in query else query
        divisions = _pkg._query_divisions(
            search_query, None, local_table, alt_table=alt_table, **near_kw
        )
        qualifier_note = _pkg._country_degrade_note(search_query, suffix_country_code)

    # #53: literal query didn't reach an exact-or-prefix division match with
    # some real prominence behind it — retry with normalized variants
    # (abbreviation swaps + diacritic folding) and merge in whatever they
    # find. Rows sourced this way are tagged `_variant`, which _rank_key
    # only ever uses as the last tiebreak (after the #47 population/proxy
    # chain) — see _rank_key's docstring for why: a literal exact match
    # against Overture's population-less tiny-village namesakes must not
    # get to shadow a genuinely prominent place found only through a
    # spelling variant ("St. Louis" also literally names several small,
    # unpopulated villages worldwide; the famous one is "Saint Louis" in
    # Overture's own naming and needs the variant retry to enter the
    # candidate pool at all — verified live, see the #53 module docstring).
    #
    # "Good enough" literal match: at least one exact/prefix candidate that
    # actually carries a population figure — Overture populates that field
    # for most well-known places (#47), so its presence is itself a signal
    # the literal search already found something real, not just a
    # same-spelling coincidence.
    #
    # Read through _effective_tier, not _match_tier, so a #214 alternate-name
    # hit counts for what it achieved: "Munich" is an exact match for
    # München's alternate, and re-deriving a tier from the canonical
    # "München" here would grade it 1 and send a query that has already found
    # its answer through the variant retries for nothing. Before #214 no row
    # in this pool carried a `_tier`, so this reads identically.
    best_literal_tier = max((_pkg._effective_tier(c, search_query) for c in divisions), default=0)
    literal_match_has_prominence = any(
        _pkg._effective_tier(c, search_query) == best_literal_tier
        and c.get("population") is not None
        for c in divisions
    )
    literal_answer_is_good_enough = (
        best_literal_tier >= _pkg._STRONG_TIER and literal_match_has_prominence
    )
    seen_ids = {c["id"] for c in divisions}
    variant_rows: list[dict] = []
    if not literal_answer_is_good_enough:
        for variant_query in _pkg._abbreviation_variant_queries(search_query):
            for row in _pkg._query_divisions(
                variant_query, region_code, local_table, country_code=country_code, **near_kw
            ):
                if row["id"] not in seen_ids:
                    row["_variant"] = True
                    # Tier against the variant text it actually matched
                    # (#53) — see _effective_tier's docstring for why this
                    # can't be recomputed against the original query later.
                    row["_tier"] = _pkg._match_tier(row["name"], variant_query)
                    variant_rows.append(row)
                    seen_ids.add(row["id"])

    # #221: with a local table (#43) the diacritic-folded pass runs
    # unconditionally, outside the "literal match lacks prominence" gate
    # above. Under the gate it never ran for "Zurich": the literal ILIKE
    # finds a Dutch village spelled exactly that, and it carries a
    # population (190), so the gate read the literal search as having
    # already found something real — while Zürich, 443k, was not merely
    # ranked below but absent from the candidate pool entirely, since ILIKE
    # '%Zurich%' does not match "Zürich". A prominence gate cannot work
    # here: whether the folded spelling is worth looking for has nothing to
    # do with how prominent the *unfolded* one turned out to be. So the pass
    # runs and _rank_key decides, which is what it is for. Cheap enough to
    # do every time — one more predicate over the same local divisions table
    # the literal pass just read (0.2s measured, #214).
    #
    # Without a local table it stays gated, exactly as before #221. That
    # 0.2s is a local-parquet number; with no local table _query_divisions
    # falls through to _query_divisions_from_upstream, and an unanchored
    # ILIKE over the divisions theme has nothing to prune by, so running it
    # unconditionally would roughly double the upstream work for every query
    # that used to stop at a good literal answer. That is the cost class
    # #105/#216 exist to avoid and that _query_divisions_fuzzy declines to
    # pay upstream for the same reason. The consequence is deliberate and
    # narrow: cache-off callers keep the pre-#221 "Zurich" answer (the fold
    # still runs for them whenever the literal search came back weak, which
    # is what "Sao Paulo" -> "São Paulo" needs), and warming the cache is
    # what buys the fix.
    #
    # The abbreviation retries above stay gated on both paths: they fire one
    # extra query per expandable token ("St." -> "Saint", "N." -> "North"),
    # and unlike folding they rewrite the query into a genuinely different
    # string, so running them against an already-good literal answer buys
    # noise rather than reach.
    #
    # perf: under a caller's near box (#476 — resolve_place's city pin) a
    # literal answer that is already confident (_CONFIDENT_TIER or better,
    # with a population behind it) stands the folded pass down as well.
    # #221's reason for always running it — a populated namesake village
    # somewhere on Earth shadowing the folded spelling of a famous city — is
    # a worldwide problem; inside the one city's box the caller pinned, an
    # exact-or-prefix populated division IS the answer, and the folded pass
    # could only pad it. Unpinned callers keep #221's always-run rule.
    confident_literal = (
        near is not None
        and best_literal_tier >= _pkg._CONFIDENT_TIER
        and literal_match_has_prominence
    )
    if not confident_literal and (local_table is not None or not literal_answer_is_good_enough):
        stripped_query = _pkg._strip_diacritics(search_query)
        # Not when stripping left nothing: a query of only combining marks
        # folds to "", and searching for it is an ILIKE '%%' that matches
        # every division in the dataset — a nonsense query answered with
        # whichever places are most populous. The literal pass, matching raw
        # names, correctly returns nothing for those.
        rows = (
            _pkg._query_divisions(
                stripped_query, region_code, local_table,
                fold_diacritics=True, country_code=country_code, **near_kw,
            )
            if stripped_query
            else []
        )
        for row in rows:
            if row["id"] not in seen_ids:
                row["_variant"] = True
                row["_tier"] = _pkg._match_tier(row["name"], stripped_query)
                variant_rows.append(row)
                seen_ids.add(row["id"])
    divisions = divisions + variant_rows

    # #215: the literal search (and its #53 variant retries) found nothing
    # at all — the shape a typo makes. Retry by edit distance against the
    # local table only; see the module docstring for why emptiness is the
    # whole trigger and why this never runs upstream.
    #
    # Matched against `base_query`, not `search_query`: when a region
    # suffix was recognized and its constrained search came up empty,
    # search_query has been reset to the *whole* original string, suffix
    # included, and no division name is within edit distance of
    # "Berekley, CA" — the correction would be lost on the single most
    # common way a caller writes a place. base_query is the name half
    # either way (_parse_region_suffix returns the query unchanged when it
    # recognizes no suffix), and the region it was parsed off of is passed
    # as a filter, dropped on a miss the same way the literal search drops
    # it.
    fuzzy_rows: list[dict] = []
    fuzzy_query = base_query
    if not divisions and local_table is not None and not _pkg._has_like_metacharacter(fuzzy_query):
        if _pkg._is_bundled_table(local_table):
            # A >=0.92 near-miss against only the top-150k names is how
            # "Berkeley Springs" becomes Berkeley: on the stage-0 index the
            # fuzzy tier trades a recall gap for confidently wrong answers.
            # Skip it; the long-tail fallback below (and the full table,
            # once its background build lands) handle the real name.
            fuzzy_rows = []
        else:
            fuzzy_rows = _pkg._query_divisions_fuzzy(
                local_table, fuzzy_query, suffix_region_code, suffix_country_code, **near_kw
            )
        if not fuzzy_rows and (
            suffix_region_code or (suffix_country_code and normalized_country is None)
        ):
            # Retry without the *parsed* qualifier only; an explicit
            # country= is the caller's filter and stays on.
            if normalized_country is not None:
                fuzzy_rows = _pkg._query_divisions_fuzzy(
                    local_table, fuzzy_query, None, normalized_country, **near_kw
                )
            else:
                fuzzy_rows = _pkg._query_divisions_fuzzy(local_table, fuzzy_query, **near_kw)
        divisions = fuzzy_rows

    _bundled_recall_pending = not divisions and _pkg._is_bundled_table(local_table)
    if (
        _bundled_recall_pending
        # #268: no division is named "Eiffel Tower". This scan exists to
        # find long-tail *populated places* the bundled index omits, and a
        # query naming a feature is not one — it was 12.8s of a 13.1s call,
        # spent proving a negative the query's own shape already implies.
        and not _pkg._names_a_feature(search_query)
        # #476: a pinned query has its anchor already; the places search
        # gets its chance first, same as when a split derives one.
        and near is None
        and _pkg._fallback_anchor(
            search_query, [], region_code, local_table,
            alt_table=_pkg._local_alt_names_table(local_table),
            region_population=_pkg._region_population_lookup(local_table),
        ) is None
    ):
        # The stage-0 bundled index carries only populous divisions; a
        # long-tail name (an unpopulated hamlet) must not read as "no such
        # place" while the full table builds in the background. With no
        # anchor derivable, the anchored-places path can't answer either,
        # so run the upstream divisions scan (the pre-#43 recall path)
        # now. When an anchor IS derivable the places search gets its
        # chance first — "Shibuya Crossing Tokyo" measured 55-70s when a
        # zero-row divisions scan ran ahead of it — and the same upstream
        # scan runs after it instead, only if nothing at all was found
        # (see the empty-candidates recall retry below).
        divisions = _pkg._query_divisions(search_query, region_code, None, country_code=country_code)  # noqa: E501
        _bundled_recall_pending = False

    region_population = _pkg._region_population_lookup(local_table)
    _pkg._flag_namesake_localities(divisions, search_query)
    divisions.sort(key=lambda r: _pkg._rank_key(r, search_query, region_population))

    # #406: cheap on an already-sorted list — the disclosure note only ever
    # needs to know whether the home bias picked a *different* top division
    # than an unbiased sort would have, not the full unbiased ordering.
    # `_home_biased_winner_id` is the division id the disclosure names if it
    # does turn out to be the final answer's top result (checked once, right
    # before `out` is built below — a places-fallback row, or a later recall
    # pass, can still overtake it).
    _home_biased_winner_id = None
    if divisions and home_region.home_bias_active():
        unbiased_top = min(
            divisions,
            key=lambda r: _pkg._rank_key(r, search_query, region_population, home_bias=False),
        )
        if divisions[0]["id"] != unbiased_top["id"]:
            _home_biased_winner_id = divisions[0]["id"]

    # #329: a famous landmark's exact-matching namesake (Colosseo the
    # Frosinone microhood, Colosseum in Queensland) must not stand the
    # places fallback down or win the batch. If we have an alias pin,
    # keep only rows in that city and still search places there.
    alias_hit = _pkg._alias_anchor(search_query)
    if alias_hit is not None:
        alat, alon, _aname = alias_hit
        def _near_alias(d):
            return geo.haversine_m(alat, alon, d["lat"], d["lon"]) <= _pkg._CITY_HINT_RADIUS_M
        divisions = [d for d in divisions if _near_alias(d)]
        # #215 stands the places fallback down once *any* fuzzy row exists.
        # A far namesake that only survived as a typo correction (Colosseo
        # the Frosinone microhood for "Colosseo Roma") must not.
        fuzzy_rows = [d for d in fuzzy_rows if _near_alias(d)]

    # Skip the places fallback once an exact-name division match is already
    # in hand: places is Overture's largest, least-indexed theme, and an
    # unbounded ILIKE scan over it live can cost tens of seconds (measured)
    # — worth paying to fill out a weak result set (a prefix/substring-only
    # match, or none at all), not worth paying just to pad an already-exact
    # answer out to `limit`.
    has_exact_division = any(_pkg._effective_tier(c, search_query) == 3 for c in divisions)
    candidates = divisions
    # #215: a fuzzy hit means the query is a misspelling we have a
    # correction for, which also settles what the places half would be
    # searching for — the typo, as a substring, against Overture's largest
    # theme. That is the scan that answered "Sna Francisco" with "Snags N
    # Burgs Cafe"; with San Francisco already in hand it can only add noise,
    # so a fuzzy answer stands the fallback down the same way an exact
    # division match does.
    if len(candidates) < limit and not has_exact_division and not fuzzy_rows:
        # #83: bound the places scan to an anchor's vicinity whenever one
        # can be derived (a division match already in hand, or a trailing
        # location word in the query) instead of an unconstrained
        # worldwide scan — see _fallback_anchor/_query_places_fallback.
        #
        # #476: under a caller's pin the pin is the anchor and the whole
        # search_query is the name to look for — the caller already split
        # the city off. The speculative splits are what aimed a Singapore
        # query at Marina, California; they have nothing to add here.
        if near is not None:
            anchor_options = [(near[0], near[1], search_query)]
        else:
            anchor_options = _pkg._fallback_anchor_candidates(
                search_query, divisions, region_code, local_table, alt_table=alt_table,
                region_population=region_population,
            )
        # #329: a famous landmark with no derivable city still has an
        # alias pin on the bundled index — use it as the places-fallback
        # anchor rather than skipping the search (or aiming at a random
        # "Tower"/"Center" division).
        if alias_hit is not None:
            pin = (round(alias_hit[0], 3), round(alias_hit[1], 3))
            rest = [
                a for a in anchor_options
                if (round(a[0], 3), round(a[1], 3)) != pin
            ]
            anchor_options = [alias_hit] + rest
        anchor_hit = anchor_options[0] if anchor_options else None
        anchor, name_query = (
            ((anchor_hit[0], anchor_hit[1]), anchor_hit[2]) if anchor_hit else (None, search_query)
        )
        if name_query is None:
            # #216: an anchor was derivable, but the query has nothing left
            # in it that a place could be named — see _fallback_anchor.
            # Searching places for a bag of stopwords costs a full-theme
            # substring scan (50.2s measured on "the Met") and returns
            # whatever unrelated name happens to contain "the". Say what
            # went wrong instead.
            note = _STOPWORD_RESIDUAL_NOTE
        elif anchor is None and _pkg._skip_unanchored_places_scan():
            # #105: with no anchor there is no bbox to prune by, so this is
            # a substring scan of every place on Earth. Measured live
            # against the 2026-07-22.0 release: 216s end-to-end for a query
            # that matched nothing, and dropping the ORDER BY doesn't help
            # (219s) -- with few or no matches the LIMIT can never
            # short-circuit, so the whole theme is read either way. That is
            # far past any MCP client's timeout: not a slow answer, a hang.
            #
            # Return what divisions found (often nothing) immediately and
            # tell the caller how to make the query answerable, instead.
            # Local datasets keep #83's behavior -- see
            # _skip_unanchored_places_scan.
            note = _UNANCHORED_NAME_SEARCH_NOTE
        else:
            seen_names = {(c["name"].lower()) for c in candidates}
            # #476: an anchor derived by splitting the query is a guess
            # about where the query means, and the first scan is the test
            # of that guess. Its box's tiles are not scheduled for
            # background materialization until the guess has paid off —
            # measured, the losing guesses left 807 MB of Florida and
            # California places/base tiles behind two European landmark
            # resolves. A pin, an alias, or a division match already in
            # hand is not a guess and schedules as before.
            speculative = divisions == [] and alias_hit is None and near is None
            places = _pkg._query_places_fallback(
                name_query, anchor=anchor, also=search_query,
                schedule_tiles=not speculative,
            )
            if not places and len(anchor_options) > 1:
                # The best reading of the query found nothing where it
                # pointed. Ambiguous city names make that reading a coin
                # flip — "harvard square cambridge" anchored on Cambridge,
                # UK over Ontario's and Massachusetts's, and the answer sat
                # in the third city of that name. Sequential retries lose
                # that race one box at a time, so the second pass scans
                # every candidate city's box at once: one bounded query,
                # file-pruned per box, against answering nothing.
                alternates = [(a[0], a[1]) for a in anchor_options[1:] if (a[0], a[1]) != anchor]
                if alternates:
                    places, winner = _pkg._query_places_multi_anchor(
                        name_query, alternates, also=search_query,
                    )
                    if places and winner is not None:
                        anchor = winner
            if speculative and places and anchor is not None:
                # The guess held: this is the anchor that answered, so its
                # tiles are worth having for the repeat query (#476).
                _pkg._schedule_places_tiles_near(anchor)
            places = [p for p in places if p["name"].lower() not in seen_names]
            # Rank by the better of the two readings of the query: a row
            # named for the whole query beats one that merely contains the
            # residual the anchor split left (#268).
            #
            # Distance to the anchor breaks ties before confidence does. Names
            # repeat inside one metro area — "Millennium Park Chicago" anchored
            # correctly on the Loop and still answered with a Millennium Park
            # 31 km away in the suburbs, because that row happened to carry
            # more confidence. The anchor is the caller's own statement about
            # where they mean; among equally-good name matches, closer to it is
            # closer to what they asked for.
            #
            # #429: the bundled alias spellings of a landmark are readings of
            # the query too, and grading against them is what stops a
            # world-famous name from losing on a technicality. "Musée du
            # Louvre" merely *contains* the word "Louvre", so literal grading
            # made it a substring match and sorted it below every hotel whose
            # name happens to start with the word; against the alias spelling
            # "musee du louvre" it is the exact match it actually is.
            alias_readings = _pkg._alias_names_for(search_query)

            def _rank_place(r):
                near = 0.0
                if anchor is not None:
                    near = geo.haversine_m(anchor[0], anchor[1], r["lat"], r["lon"])
                return (
                    -max(
                        _pkg._match_tier(r["name"], reading)
                        for reading in (name_query, search_query, *alias_readings)
                    ),
                    round(near / 1000.0),
                    -r["_confidence"],
                    r["id"],
                )

            places.sort(key=_rank_place)
            candidates = candidates + places

    if not candidates and _bundled_recall_pending and not _pkg._names_a_feature(search_query):
        # Anchored-places had its chance and found nothing either; the
        # query may be a real division below the bundled index's
        # population cutoff. One upstream divisions scan preserves the
        # recall the full local table used to guarantee — paid only when
        # every faster path came up empty, and only until the background
        # full build lands.
        #
        # Not for a query naming a feature, though (#268): no division is
        # called "Eiffel Tower" or "Grand Central Terminal", so the scan can
        # only come back empty, and it was the entire cost of doing so —
        # 11.0s of a fresh install's first landmark query, spent proving a
        # negative that the query's own shape already implies.
        recalled = _pkg._query_divisions(
            search_query, region_code, None, country_code=country_code, **near_kw
        )
        _pkg._flag_namesake_localities(recalled, search_query)
        recalled.sort(key=lambda r: _pkg._rank_key(r, search_query, region_population))
        candidates = recalled
        # #406: the recall pass replaces `candidates` wholesale, so redo the
        # bias-changed-winner check against it rather than trusting the
        # (now superseded) one computed above.
        _home_biased_winner_id = None
        if recalled and home_region.home_bias_active():
            unbiased_top = min(
                recalled,
                key=lambda r: _pkg._rank_key(r, search_query, region_population, home_bias=False),
            )
            if recalled[0]["id"] != unbiased_top["id"]:
                _home_biased_winner_id = recalled[0]["id"]

    # #410: one lookup for the whole page of division-kind rows about to be
    # returned, keyed by the ids actually surviving [:limit] — not a scan per
    # row, and not run at all unless a lang was actually requested. Places-
    # fallback rows (row["_category"] is set) are excluded: they come from a
    # different theme/id-space than the divisions lang table indexes, same
    # scope line find_places draws this round (see the docstring above).
    lang_variants: dict[str, str] = {}
    if lang:
        page = candidates[:limit]
        division_ids = [r["id"] for r in page if r.get("id") and r.get("_category") is None]
        lang_variants = _pkg._lang_variants_for(_pkg._local_lang_names_table(local_table), division_ids, lang)  # noqa: E501

    out = []
    for row in candidates[:limit]:
        entry = {
            "name": row["name"],
            "type": row["subtype"],
            "lat": row["lat"],
            "lon": row["lon"],
            "id": row["id"],
            "admin_context": row["admin_context"],
            "rank_score": _pkg._rank_score(row, search_query) if "_confidence" not in row else round(  # noqa: E501
                0.4 + row["_confidence"] * 0.3, 3
            ),
        }
        if include_country:
            entry["country"] = row.get("country")
        if row.get("_category") is not None:
            # Places-fallback rows only: the place's Overture category, so
            # resolve_place (and any caller) doesn't lose it in the merge.
            entry["category"] = row["_category"]
        if row.get("_matched_name"):
            # #214: only present when the row was found through one of
            # Overture's alternate (names.common) spellings rather than its
            # canonical one. `name` stays canonical either way, so this is
            # what tells a caller "Munich" found "München" on purpose.
            entry["matched_name"] = row["_matched_name"]
        if row.get("_fuzzy"):
            # #215: the note says "these answer a corrected spelling" in
            # prose, which is what a human reader needs; this says it per
            # row, in a field, which is what code needs. resolve_place
            # returns no note at all and has to label each candidate's
            # match on its own — without this it can only re-derive a tier
            # from a name that doesn't contain the query, and call a
            # correction a "substring" match.
            entry["matched_by"] = "fuzzy"
        variant = lang_variants.get(row["id"]) if row.get("id") else None
        if variant and variant != entry["name"]:
            # #410: never invent or transliterate — variant only ever comes
            # from Overture's own names.common map, looked up above.
            # name_primary appears only when it actually differs, so a
            # no-variant answer stays byte-identical to the no-lang one.
            entry["name_primary"] = entry["name"]
            entry["name"] = variant
        out.append(entry)
    if out:
        _pkg._kick_autowarm(out[0])
    result = {"results": out}
    fuzzy_out = [row for row in candidates[:limit] if row.get("_fuzzy")]
    if fuzzy_out:
        # #215: unlike the skip notes below, this one accompanies real
        # results — it says which spelling they actually answer. Named
        # against the string actually fuzzed, which is the query minus any
        # region suffix.
        result["note"] = _pkg._fuzzy_correction_note(fuzzy_out, fuzzy_query)
    elif qualifier_note:
        # #457: same idiom — rides along with real results (an
        # unrecognized qualifier or a country degrade still searches the
        # base name and usually finds something), not instead of them.
        result["note"] = qualifier_note
    if postcode_note and not out:
        # #223: the query was postcode-shaped, the postcode aggregate found
        # nothing, and neither did the name search. What that emptiness means
        # is a coverage fact, not a "the places half was skipped" fact.
        result["note"] = postcode_note
    elif note and not out:
        # Only worth saying when the answer is empty: if divisions already
        # produced candidates, the skipped places half isn't what the caller
        # is missing.
        result["note"] = note
    if (
        _home_biased_winner_id is not None
        and out
        and out[0]["id"] == _home_biased_winner_id
    ):
        # #406: only fires when the home-biased division is still the
        # actual top result after everything else (places fallback, limit
        # trim) has had its say — never when the bias was overridden by a
        # stronger later signal. Never a filter: the distant candidates it
        # displaced are still in `out`, just not first.
        home = home_region.get_home_region()
        disclosure = home_region.disclosure_note(home)
        result["note"] = f"{result['note']} {disclosure}" if result.get("note") else disclosure
    return result



# --- #22: GERS id resolution -----------------------------------------------

# resolve_place overfetches both sources before merging/ranking/trimming to
# `limit`, the same reasoning as DIVISION_OVERFETCH: a shallow per-source
# limit can drop the right candidate before the merged ranking ever sees it.
_RESOLVE_OVERFETCH = 10


# #105: a places-name search with no anchor has no bbox to prune by, making
# it a substring scan of Overture's largest theme. Against the live remote
# dataset that measured 216s end-to-end (and 219s with the ORDER BY removed
# -- a LIMIT can't short-circuit a scan matching few or no rows), which is
# past any MCP client's timeout.
#
# The cost is the *remote* full-theme read, not the query shape: against a
# local dataset (a test fixture, or a mirror via PLACEROOT_DATA_PATH /
# PLACEROOT_UPSTREAM_BASE) the same scan is cheap and genuinely useful, so
# #83's name-only search is kept exactly as-is there. Set this env var to
# force the unbounded scan even against a remote dataset.
_UNBOUNDED_NAME_SEARCH_ENV = "PLACEROOT_UNBOUNDED_NAME_SEARCH"


# Schemes DuckDB reads over the network; anything else is a local path.
_REMOTE_GLOB_SCHEMES = ("s3://", "http://", "https://", "gcs://", "gs://", "az://", "azure://")


_STOPWORD_RESIDUAL_NOTE = (
    "no division matched this query as a whole, and once its trailing location "
    "word is set aside as an anchor nothing distinctive is left to search place "
    "names for (only common words like \"the\" or \"of\", or generic type words like "
    "\"Station\" or \"Park\"), so the places half of "
    "the search was skipped -- matching those against every place name is minutes "
    "of scanning for results that would be unrelated anyway. Spell the name out "
    "(\"the Metropolitan Museum of Art\" rather than \"the Met\"), or use "
    "find_places with lat/lon to search a known area."
)


_UNANCHORED_NAME_SEARCH_NOTE = (
    "no division matched, and this query carries no location context to bound a "
    "place-name search by, so the places half of the search was skipped (it would "
    "scan the entire global places dataset -- minutes, not seconds). Add a location "
    "to the query (\"Blue Bottle Roastery, Oakland\"), or use find_places with "
    "lat/lon (or resolve_place with near_lat/near_lon) to search a known area."
)
