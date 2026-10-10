"""Named-place scan helpers used by resolve_place: match labels, type scans, fuzzy place match."""

import contextlib
import contextvars
import inspect
import os
import re
import sys as _sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import duckdb

from placeroot import categories, db

_pkg = _sys.modules["placeroot.geocode"]


def _unbounded_name_search_enabled() -> bool:
    """True iff the operator opted back into the unbounded places-name scan."""
    value = os.environ.get(_pkg._UNBOUNDED_NAME_SEARCH_ENV, "").strip().lower()
    return value not in ("", "0", "false", "off")



def _is_remote(glob: str) -> bool:
    """Whether reading `glob` means going over the network."""
    return glob.lower().startswith(_pkg._REMOTE_GLOB_SCHEMES)



def _skip_unanchored_places_scan() -> bool:
    """Whether to skip the anchorless places-name scan for this dataset.

    Only skipped when the scan would be a remote read of the whole places
    theme (#105) and the operator hasn't opted back in.
    """
    if _unbounded_name_search_enabled():
        return False
    return _pkg._is_remote(_pkg.overture.upstream_glob(theme="places", type_="place"))


# Bbox radius (#22) for the name-filtered find_places call when no
# near_lat/near_lon hint is given but a division match is in hand — "same
# metro area" as the top division match, not a general-purpose area search.
_RESOLVE_PLACE_RADIUS_M = 20_000


# #481: how far from a bundled-alias pin a candidate still counts as *the*
# landmark the alias names. The alias table is a curated coordinate — the
# strongest evidence this module ever holds about where a query means — and
# a candidate sitting on it must outrank one that merely earned a better
# string tier 4 km away ("notre dame paris" answered Notre Dame de
# Clignancourt, a parish church in the 18th, on a prefix match while the
# cathedral 30 m from the pin only *contained* the words). 400 m is a
# landmark's footprint plus the shops named after it, not a neighbourhood.
_ALIAS_PIN_RADIUS_M = 400

# Rows fetched by the one unfiltered nearest-first scan at an alias pin —
# the landmark itself sits metres from its pin, so the nearest couple of
# dozen rows always include it even on a square packed with cafés and
# souvenir shops. find_places clamps to overture.MAX_ROWS (25) regardless.
_ALIAS_PIN_SCAN_LIMIT = 25


_MATCH_TIER_LABELS = {3: "exact", 2: "prefix", 1: "substring"}


# resolve_place's `match` labels, best first. "contains" (#475) is a place
# label only — the candidate's whole name contains the whole query, or the
# reverse — and sits above "substring", which for places also covers a
# single shared significant word (see _place_match_label). "fuzzy" (#215)
# is not a tier the literal search can produce — it means the name doesn't
# contain the query at all and was reached by edit distance instead — so
# it ranks below every literal label, matching how _rank_key already
# orders the rows themselves.
_MATCH_LABEL_RANK = {"exact": 4, "prefix": 3, "contains": 2, "substring": 1, "fuzzy": 0}


# Small enough to filter, generic enough that requiring them in a name
# match would be actively wrong ("the Whole Foods on Lamar" — "the"/"on"
# aren't part of any real place name). Dropped before a query is split into
# per-token find_places searches and before word-overlap scoring.
_STOPWORDS = {"the", "a", "an", "on", "in", "at", "near", "of", "and", "by"}


# #469: the generic type word a place query ends in -> the Overture category
# slugs a row of that kind carries. "Shibuya Station" is a *kind* of thing
# plus a name, and Overture files the thing under a category, not under the
# English word: the station is "Gare de Shibuya" / "京王井の頭線 渋谷駅",
# category train_station, and no row anywhere is named "Shibuya Station".
# A name-only search can never reach it; a category-filtered search on the
# distinctive word alone ("Shibuya", within train_station) finds it in one
# bounded scan. See _type_word_slugs and the #469 block in resolve_place.
#
# Curated from what categories.search_categories returns for each word
# rather than looked up live: that search is lexical, and for "station" it
# ranks gas_station and radio_station level with metro_station. Every slug
# here is a row in data/overture_categories.csv (tested), and rows come back
# through find_places' substring category filter, so a scan for "park" also
# returns "parking" — _type_scan_rows drops those with an exact/hierarchy
# check against the same CSV. Words whose category is too loose to be
# useful ("market", "square", "center", "hall", "terminal") are deliberately
# absent: they still count as generic for the gate, they just get no scan.
_TYPE_WORD_CATEGORIES: dict[str, tuple[str, ...]] = {
    "station": ("train_station", "metro_station", "light_rail_and_subway_stations"),
    "park": ("park",),
    "airport": ("airport",),
    "museum": ("museum",),
    "bridge": ("bridge",),
    "tower": ("tower",),
    "temple": ("temple",),
    "shrine": ("shinto_shrines",),
    "church": ("church_cathedral",),
    "cathedral": ("church_cathedral",),
    "stadium": ("stadium_arena",),
    "arena": ("stadium_arena",),
    "university": ("college_university",),
    "college": ("college_university",),
    "school": ("school",),
    "hospital": ("hospital",),
    "zoo": ("zoo",),
    "beach": ("beach",),
    "plaza": ("plaza",),
    "hotel": ("hotel",),
    "library": ("library",),
    "theater": ("theaters_and_performance_venues",),
    "theatre": ("theaters_and_performance_venues",),
    "palace": ("palace",),
    "castle": ("castle",),
    "mall": ("shopping_center",),
}


# #469: two rows of the same category this close together are the same
# feature — a station's exits, lines and operators are each their own
# places row, a park's lawn and its athletic track likewise. A row that
# stands alone is the one mis-pinned across town (a "Shibuya Station Tokyo.
# Japan" 7 km east of every other Shibuya station row, at confidence 0.71).
_TYPE_SCAN_SUPPORT_RADIUS_M = 1_000



def _match_label(row: dict, query: str) -> str:
    """A geocode() result row -> how it matched `query`.

    A #215 fuzzy row is labeled from its own provenance, not re-derived
    from its name: `_match_tier` reports "substring" for any name it is
    handed, including one that doesn't contain the query at all, so
    deriving a label from "Berkeley" against "Berekley" would claim a
    containment that isn't there — the one thing a caller reads this field
    to rule out.
    """
    if row.get("matched_by") == "fuzzy":
        return "fuzzy"
    return _MATCH_TIER_LABELS[_pkg._match_tier(row["name"], query)]



def _division_match_label(row: dict, query: str, search_query: str) -> str:
    """A division candidate's match label in resolve_place (#344): the
    better of its label against the full `query` and against the
    city-stripped `search_query`.

    Place candidates already retry against `search_query` when a match
    against the full query is weak (see the `_place_match_label` call
    sites below) — a query like "Times Square New York" strips its trailing
    city hint before searching, and the landmark a caller means is judged
    against what is actually left of their words, not against text a city
    hint added back on. Divisions never got that retry: graded only against
    the untouched original query, the *correct* "Times Square" division
    read as a bare substring ("Times Square" is not a prefix of "Times
    Square New York"), while an unrelated, coincidentally-named place
    elsewhere could read as a stronger "prefix" match purely because places'
    tiering already checks both directions. Kind-agnostic ranking (the
    point of this merge) needs kind-consistent tiering, not just a
    kind-agnostic sort key sitting on top of two different ones.
    """
    label = _match_label(row, query)
    if search_query != query:
        alt_label = _match_label(row, search_query)
        if _pkg._MATCH_LABEL_RANK[alt_label] > _pkg._MATCH_LABEL_RANK[label]:
            return alt_label
    return label



# resolve_place runs one find_places per significant token, each taking the
# shared DuckDB connection lock — so an unbounded token count from a huge
# query string is a lock-contention DoS against the whole server, not just a
# slow response. A real place reference ("the Whole Foods on South Lamar,
# Austin") is a handful of words; cap the fan-out well above that.
_MAX_RESOLVE_TOKENS = 12



def _nothing_but_stopwords(text: str) -> bool:
    """Whether `text` holds no word a place could be named after — every word
    in it is a _STOPWORD, or there are no words at all.

    _fallback_anchor's residual gate (#216). Deliberately *not*
    _significant_tokens' rule: that one also drops words under 3 characters,
    which is safe there only because it layers a never-search-nothing
    fallback on top. Here the answer is load-bearing — "reject" means
    returning no results at all — and plenty of real names are nothing but
    short words ("H&M", and most two-character Chinese/Japanese/Korean place
    names). Those are distinctive enough to search on; "the" is not.
    """
    return not any(w.lower() not in _STOPWORDS for w in re.findall(r"[\w'-]+", text))



def _nothing_but_generic(text: str) -> bool:
    """Whether `text` holds no word a place could be *named* — every word in
    it is a _STOPWORD or a _GENERIC_PLACE_WORDS member, or there are no words
    at all.

    #472: _nothing_but_stopwords, one level up. "Shibuya Station" anchors on
    the Shibuya division and leaves "Station" as the residual, and ILIKE
    '%Station%' over the anchor's box is the same scan #216 refused for
    '%the%' with a smaller haystack: 5.5s of a 5.7s geocode() call, measured
    live, answering with "Nakameguro Station" and "Tokyo Station Beer
    Stand". A type word says what the place is, never which one — every
    station inside the box matches equally, so the results are junk by
    construction and the time is spent proving it. Same for "Park",
    "Museum", "Airport": any anchor split whose residual is only a feature
    noun.

    This is the residual gate _fallback_anchor_candidates and _alias_anchor
    actually apply. _nothing_but_stopwords is kept as it was so its meaning
    stays exact ("H&M" and a two-character CJK name still pass, and so does
    "Station" — a type word is not a stopword); this widens the *question*
    rather than the stopword set. One word being generic is not enough:
    "Blue Bottle Station" and "Westfield Valley Fair" carry a distinctive
    word alongside the type word and must reach the anchored scan.
    """
    return not any(
        w.lower() not in _STOPWORDS and w.lower() not in _pkg._GENERIC_PLACE_WORDS
        for w in re.findall(r"[\w'-]+", text)
    )



def _significant_tokens(query: str) -> list[str]:
    """query -> its meaningful words: >=3 chars, not a stopword, in order of
    appearance, capped at _MAX_RESOLVE_TOKENS. Falls back to the whole query
    if nothing survives (e.g. a query that's all short/stopword tokens)
    rather than searching nothing.
    """
    tokens = [t for t in re.findall(r"[\w'-]+", query) if len(t) >= 3]
    significant = [t for t in tokens if t.lower() not in _STOPWORDS]
    return (significant or tokens or [query])[:_pkg._MAX_RESOLVE_TOKENS]



def _is_word_prefix(prefix: str, text: str) -> bool:
    """`text` starts with `prefix` *at a word boundary*: "Mall" is a prefix
    of "Mall of America", "Ma" is not — the character after the prefix must
    end a word (or the string), or the label is only "contains"."""
    if not prefix or not text.startswith(prefix):
        return False
    return len(text) == len(prefix) or not text[len(prefix)].isalnum()



def _place_match_label(
    name: str, query: str, context_words: frozenset[str] = frozenset()
) -> str | None:
    """Match label for a place candidate found via per-token search (#22),
    or None if it isn't actually related to `query` — the per-token
    find_places calls below are deliberately loose (OR of significant
    words) for recall, so this is what keeps an unrelated nearby place from
    polluting results just because it happens to share one common word.

    Unlike _match_tier (divisions, where the query and the canonical name
    are usually close to the same shape), a free-text place query commonly
    carries extra context the place's own name doesn't ("Mañana coffee
    Austin" vs. a place literally named "Mañana Coffee") — so containment
    is checked in both directions, and failing that, a shared significant
    word still counts as a (weaker) match.

    Whole-name containment and a shared word are not the same strength of
    claim, and labeling them both "substring" (#475) let the tie-breaks
    decide between them: "Marina Bay Sands Singapore" answered "Freia
    Aesthetics | Marina Square" — an aesthetics clinic that shares the one
    word "marina" — over "Skypark#Marina Bay Sands Hotel,Singapore.", whose
    name contains the caller's entire query. Both read as "substring"; the
    city pin (Singapore's centroid, 1 km from the clinic and ~1.1 km from
    the hotel, the same km once rounded) settled nothing; prominence
    picked the clinic. So containment in either direction is its own label,
    "contains", ranked between "prefix" and "substring": a name that holds
    every word the caller typed, or a query that holds the candidate's
    whole name ("the Blue Bottle Roastery Austin" vs. "Blue Bottle
    Roastery" — the #22 shape, with the name mid-query rather than leading
    it), beats any one-word coincidence, and the distance/prominence
    tie-breaks then only ever choose among names that actually contain
    each other. A shared significant word stays "substring": still related
    enough to keep, still the weakest literal claim.

    #469: the shared word has to be a *distinctive* one. "Shibuya Station"
    and "Snow Peak Land Station" share "station"; "Yoyogi Park" and "Park
    Hotel Tokyo" share "park". Those words say what kind of thing the
    caller means, and every downtown has hundreds of names carrying them —
    letting one through as relatedness is what resolved the station to a
    sporting-goods store 7 km away and routed a 1.5 km walk as 8.6 km.
    So the generic feature words (_GENERIC_PLACE_WORDS) are set aside from
    the query's side of the overlap first, and the candidate must share
    one of the words that remain. Only when the query is *nothing but*
    generic words ("Station") is there no distinctive word to insist on,
    and the plain overlap rule stands. The containment rules above are
    untouched: a name that contains the whole query, type word and all,
    is still a match.

    context_words are the query's location words — the trailing city
    resolve_place split off to anchor the search ("tokyo" in "Shibuya
    Station Tokyo"). They say where, not which, and are set aside from the
    overlap the same way: "Tokyo Station Beer Stand" shares "tokyo" with
    that query and is no more the answer for it than Snow Peak was.
    """
    n, q = _pkg._normalize_for_match(name), _pkg._normalize_for_match(query)
    if n == q:
        return "exact"
    if _pkg._is_word_prefix(q, n) or _pkg._is_word_prefix(n, q):
        return "prefix"
    if n in q or q in n:
        return "contains"
    n_tokens = set(_pkg._significant_tokens(n))
    q_tokens = set(_pkg._significant_tokens(q))
    q_distinctive = {
        t for t in q_tokens
        if t.strip(".,") not in _pkg._GENERIC_PLACE_WORDS and t not in context_words
    }
    if n_tokens & (q_distinctive or q_tokens):
        return "substring"
    return None



def _type_word_slugs(search_query: str) -> tuple[str, ...]:
    """#469: the category slugs for the generic type word `search_query`
    ends in, or () when it ends in something else (or in a type word with
    no usable category — see _TYPE_WORD_CATEGORIES)."""
    tokens = _pkg._significant_tokens(search_query)
    if not tokens:
        return ()
    return _pkg._TYPE_WORD_CATEGORIES.get(tokens[-1].lower().strip(".,"), ())



# perf: how many of resolve_place's bounded places scans run side by side.
# Each runs on its own cursor (db.isolated_reads) rather than under the
# global connection lock; small because every scan still competes for the
# same S3 bandwidth and DuckDB thread pool, and a query has at most a
# handful of distinctive words.
_PLACE_SCAN_WORKERS = 4


# perf: the label a first-round row must earn for resolve_place to skip its
# per-word scans. "exact" is the one label a single-word scan can never
# beat — a row found by one word alone is at best "prefix" (its name is a
# word-prefix of the query; see _place_match_label), so skipping those
# scans cannot change the top result.
_CONFIDENT_PLACE_LABEL = "exact"



def _find_places_kwargs(**extra) -> dict:
    """The keywords in `extra` that the installed overture.find_places
    accepts. The suite's test doubles answer to the six-parameter pre-#373
    signature (lat, lon, radius_m, category, name, limit); a newer, rarely
    non-default keyword is passed only when the callee can take it — the
    same opt-in idiom as #457's `country_kw`."""
    try:
        params = inspect.signature(_pkg.overture.find_places).parameters
    except (TypeError, ValueError):  # pragma: no cover - a C callable
        return dict(extra)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(extra)
    return {k: v for k, v in extra.items() if k in params}



def _run_place_scans(jobs: list[Callable[[], list[dict]]]) -> list[list[dict]]:
    """Run independent bounded places scans side by side; results in job
    order, so the caller merges them exactly as a serial loop would have.

    Each worker reads on a private cursor (db.isolated_reads) so the scans
    overlap instead of queueing on overture._conn_lock, and runs under a
    copy of the calling context so trace/progress records land in the
    caller's request. The first job's exception (in job order) is
    re-raised after every job has finished — UpstreamUnavailable from a
    scan propagates exactly as it did from the serial loop.
    """
    if len(jobs) <= 1:
        return [job() for job in jobs]

    def _isolated(job):
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(db.isolated_reads())
            except duckdb.Error:
                # No private cursor to be had (the shared instance could
                # not be opened — offline, httpfs missing): the scan still
                # runs, just serialized on the global lock as before, and
                # whether it can answer is its own business to report.
                _pkg.logger.debug("isolated cursor unavailable; scanning unisolated", exc_info=True)
            return job()

    with ThreadPoolExecutor(max_workers=min(_PLACE_SCAN_WORKERS, len(jobs))) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _isolated, job) for job in jobs
        ]
        return [f.result() for f in futures]



def _type_scan_rows(
    lat: float, lon: float, slugs: tuple[str, ...], token: str
) -> list[dict]:
    """#469: one bounded find_places scan for rows OF the query's kind whose
    name carries its distinctive word — "Shibuya" within train_station.

    Same radius and limit as the per-token scans. find_places' category
    filter is a substring match (so "park" returns parking lots too); rows
    are kept only when their own category is one of `slugs` or sits under
    one in the taxonomy, the exact/hierarchy reading the compose tools use.
    A row with no category at all is kept — a degraded taxonomy column is
    not evidence against it. Each kept row is tagged "_type_scan" for the
    grading in resolve_place.
    """
    # perf: one word of the query within one category — a close-spelling
    # match of it is not the answer, so the fuzzy tier is off (see
    # find_places' fuzzy_fallback); the alt-name tier still runs.
    rows = _pkg.overture.find_places(
        lat, lon, radius_m=_pkg._RESOLVE_PLACE_RADIUS_M,
        categories=list(slugs), name=token, limit=_pkg._RESOLVE_OVERFETCH,
        **_pkg._find_places_kwargs(fuzzy_fallback=False),
    )
    kept = []
    for row in rows:
        cat = row.get("category")
        if cat:
            chain = categories.hierarchy_for(cat) or [cat]
            if not any(slug in chain for slug in slugs):
                continue
        row["_type_scan"] = True
        kept.append(row)
    return kept



def _best_place_label(
    name: str, query: str, alternates: list[str],
    context_words: frozenset[str] = frozenset(),
) -> str | None:
    """The strongest label `name` earns against `query` or any of `alternates`.
    context_words pass through to _place_match_label (#469).

    #429: the alternates are other spellings of the same thing the caller
    asked for — the bundled landmark aliases, and the city-stripped
    search_query — so the label they earn is a label for the caller's own
    question, not a consolation prize. Taking the *first* non-None one
    (the previous rule) meant a literal label, however weak, blocked the
    alternates from ever being tried: "Louvre" reads as a bare substring of
    "Musée du Louvre", so the museum was graded a substring match and lost
    the tier sort to "Louvre Luxury Apartment & SPA", whose name merely
    starts with the word. Graded against the alias spelling "musee du
    louvre" it is an exact match, which is what it actually is.
    """
    best = _pkg._place_match_label(name, query, context_words)
    for alt in alternates:
        label = _pkg._place_match_label(name, alt, context_words)
        if label is not None and (
            best is None or _pkg._MATCH_LABEL_RANK[label] > _pkg._MATCH_LABEL_RANK[best]
        ):
            best = label
    return best



def _jaro_winkler(a: str, b: str) -> float:
    """Plain-Python Jaro-Winkler (0..1), matching DuckDB's
    jaro_winkler_similarity closely enough to share
    _FUZZY_SIMILARITY_THRESHOLD: standard Jaro, then the Winkler common-
    prefix boost (scaling 0.1, prefix capped at 4, applied above 0.7 —
    the same shape as the rapidfuzz implementation DuckDB vendors).

    Exists for the #374 re-score below, where a per-row SQL round-trip per
    candidate would be all overhead: the SQL tiers keep using DuckDB's own
    function.
    """
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(max(la, lb) // 2 - 1, 0)
    a_matched = [False] * la
    b_matched = [False] * lb
    matches = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - window), min(lb, i + window + 1)):
            if not b_matched[j] and b[j] == ca:
                a_matched[i] = b_matched[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = 0
    j = 0
    for i in range(la):
        if a_matched[i]:
            while not b_matched[j]:
                j += 1
            if a[i] != b[j]:
                transpositions += 1
            j += 1
    jaro = (
        matches / la + matches / lb + (matches - transpositions / 2) / matches
    ) / 3
    if jaro <= 0.7:
        return jaro
    prefix = 0
    for ca, cb in zip(a, b):
        if ca != cb or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)



def _fuzzy_place_covers_query(name: str, tokens: list[str]) -> bool:
    """#374: whole-query relatedness re-score for a #373 fallback row that
    was matched against a single TOKEN of a multi-token query.

    The per-token find_places loop in resolve_place means a fuzzy/alt-name
    hit only proved similarity to ONE word — trusting its matched_by label
    outright let a bar named "King" (fuzzy-close to the token "kings")
    resolve the whole query "Kings Barbershop Chicago". Such a row is only
    kept when every distinctive query token is covered by the row's actual
    name: contained in it, or typo-close (same 0.92 threshold as the SQL
    tier) to one of its words. Rows matched against the whole query never
    come through here — their similarity was already scored against
    everything the caller typed.
    """
    folded_name = _pkg.overture._fold_poi_name(name)
    name_words = folded_name.split()
    for tok in tokens:
        folded_tok = _pkg.overture._fold_poi_name(tok)
        if not folded_tok or folded_tok in folded_name:
            continue
        if any(
            _pkg._jaro_winkler(word, folded_tok) >= _pkg._FUZZY_SIMILARITY_THRESHOLD
            for word in name_words
        ):
            continue
        return False
    return True



# #344: subtypes a `city` hint is allowed to resolve to without falling
# back to the top (population-ranked) hit. Deliberately narrower than
# _SUBTYPE_WEIGHT's full ladder — a hint named "city" should not silently
# resolve to a neighborhood, which is finer-grained than any caller means
# by "the city".
_CITY_HINT_SUBTYPES = frozenset({"locality", "localadmin"})


# #427: how deep to look for the division a comma qualifier names. Same
# depth resolve_place's own `city` hint uses — enough for _pick_city_hint_row
# to find a city-level hit under a same-named region, not so deep that a
# qualifier turns into a survey.
_ANCHOR_LOOKUP_LIMIT = 10


# #427: how many candidates the anchored division pass asks geocode() for.
# Deliberately far above _RESOLVE_OVERFETCH: geocode ranks by prominence,
# with no idea an anchor is in play, and a name like "Hilltop" or "Le
# Marais" has dozens of homonyms worldwide — the one inside the anchor is
# routinely past the first ten. The rows are already ranked and in memory
# by then, so a deeper page costs a slice, not a scan.
_ANCHORED_OVERFETCH = 50



def _pick_city_hint_row(hits: list[dict]) -> dict:
    """Pick the row a `city` hint means, from geocode()'s ranked hits.

    geocode()'s own ranking (_rank_key) orders same-named divisions by raw
    population, which is the right call for a bare geocode() query — but a
    `city` hint's whole meaning is "a city", and a state/region can share a
    populous namesake with the city it was named after ("New York" the
    state outranks New York City there). Preferring the first city-level
    hit over a broader admin unit resolves that without touching geocode()'s
    general ranking, which other callers (and the ranking corpus) depend on
    exactly as it is. Falls back to the top hit when nothing at the city
    level matched — the hint may genuinely name a country or region.
    """
    for hit in hits:
        if hit.get("type") in _CITY_HINT_SUBTYPES:
            return hit
    return hits[0]
