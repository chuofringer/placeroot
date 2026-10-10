"""resolve_area(), resolve_named_place(), the typo tier and comma-qualified names."""

import contextlib
import contextvars
import os
import sys as _sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait

import duckdb

from placeroot import db, geo
from placeroot.errors import AmbiguousArea, AmbiguousPlace, AnchoredNotFound

_pkg = _sys.modules["placeroot.geocode"]


# --- #123: free-text area name -> a division to constrain a search to -------

# Two candidates count as "equally ranked" when their rank_scores differ by
# less than this. rank_score is a computed float, so exact equality is the
# wrong test — but the tolerance stays tiny on purpose: geocode already
# breaks same-name ties by population and match tier (#47/#53), so a
# genuinely more prominent namesake outranks the rest by a wide margin and
# resolves cleanly. What survives at this tolerance is the real ambiguity
# the issue is about: same-tier, same-name divisions the dataset gives us
# no signal to choose between (e.g. two population-less "Springfield"s).
_AREA_RANK_EPSILON = 1e-6


# Cap on candidates reported back for an ambiguous area — enough to choose
# from, not a data dump.
_AREA_MAX_CANDIDATES = 5


def resolve_area(area: str) -> dict | None:
    """Free-text area name -> the single division to constrain a search to.

    Thin resolution layer over geocode()'s division ranking — deliberately
    NOT a second ranking implementation. geocode() already handles the
    "City, ST" suffix, diacritic/abbreviation variants, and the
    population-weighted tie-breaks; this just takes its division results
    (type != "place"; a business named "Palo Alto Cafe" is not an area) and
    decides whether the top one is a safe pick.

    Returns {"division_id", "name", "admin_context"} for a confident match,
    or None if nothing matched at all. Raises AmbiguousArea when several
    equally-ranked divisions share the top name, so the caller can surface
    the candidates instead of silently searching one arbitrary "Springfield"
    and reporting its places as though the question had one answer.

    Raises overture.UpstreamUnavailable if the underlying scan fails.
    """
    area = area.strip()
    if not area:
        return None

    # Rows without an id can't be handed to the polygon search at all, so
    # they're dropped here rather than surfacing as a confusing downstream
    # error (id is only ever absent from a degraded dataset).
    divisions = [
        r
        for r in _pkg.geocode(area, limit=_pkg._RESOLVE_OVERFETCH)
        if r["type"] != "place" and r["id"]
    ]
    if not divisions:
        return None

    top = divisions[0]
    # Ambiguity is specifically "same name, no way to rank them" — a
    # differently-named division that merely scored close (a neighborhood
    # inside the city you asked for) is not ambiguity, so compare names too.
    top_name = _pkg._normalize_for_match(top["name"])
    tied = [
        d
        for d in divisions
        if _pkg._normalize_for_match(d["name"]) == top_name
        and abs(d["rank_score"] - top["rank_score"]) < _AREA_RANK_EPSILON
    ]
    if len(tied) > 1:
        raise AmbiguousArea(area, [_area_candidate(d) for d in tied[:_AREA_MAX_CANDIDATES]])

    _pkg._kick_autowarm(top)
    return _area_candidate(top)


def _area_candidate(row: dict) -> dict:
    """A division row, projected to just what an area choice needs."""
    return {
        "division_id": row["id"],
        "name": row["name"],
        "admin_context": row["admin_context"],
    }


def resolve_named_place(query: str) -> dict | None:
    """Free-text place name -> one place or division, or AmbiguousPlace.

    Same ranking as geocode() / resolve_area(): a prominence winner is a
    safe pick. Several same-name, same-score hits raise AmbiguousPlace
    so a compose tool cannot silently route the wrong city. Includes
    places as well as divisions — "Ferry Building" is not an area.

    When geocode matches no division at all, the places half of the answer
    comes from resolve_place rather than from geocode's own supplementary
    places scan — see the #429 block below for why that leg exists and why
    it is scoped to the no-division case.

    A comma-qualified name ("Le Marais, Paris") is read as name-plus-
    qualifier rather than as one opaque string — see the #427 block below
    for the split, the anchor bound, and how the tiers interact.

    Returns {name, lat, lon, id, type, admin_context} or None if nothing
    matched, plus a non-fatal "note" when a qualifier was present but did
    not resolve. Raises AmbiguousPlace, AnchoredNotFound (nothing inside a
    qualifier that did resolve), or overture.UpstreamUnavailable.
    """
    query = query.strip()
    if not query:
        return None

    head, qualifier = _split_qualifier(query)
    if head is not None:
        anchor = _pkg._resolve_qualifier_anchor(qualifier)
        if anchor is not None:
            return _resolve_inside_anchor(query, head, anchor)

    # perf: leg 2 is started now, alongside leg 1, rather than after it. It is
    # only kept when leg 1 finds no division (see _SpeculativeLeg).
    spec = _speculate_place_leg(query)
    try:
        rows = [
            r
            for r in _pkg.geocode(query, limit=_pkg._RESOLVE_OVERFETCH)
            if r.get("lat") is not None
            and r.get("lon") is not None
            # #431: a fuzzy row that only ever proved itself against part of
            # what the caller typed is not an answer. See the block below.
            and not _pkg._fuzzy_row_is_too_weak(query, r)
        ]
    except BaseException:
        if spec is not None:
            spec.discard()  # leg 1 failed: the serial path would never have run leg 2
        raise
    hit = None
    # A speculative leg exists only when the extra-context gate already passed.
    if not any(r["type"] != "place" for r in rows) and (
        spec is not None or _pkg._has_extra_place_context(query)
    ):
        # #429: no division matched, so this is a places question — and the
        # places resolver is resolve_place, not geocode. See the block below.
        hit = _resolve_place_leg(query, spec)
    elif spec is not None:
        spec.discard()
    if hit is None:
        if not rows:
            return None
        hit = _pick_named_place(query, rows)
    if head is not None:
        # The qualifier named nothing this release knows, so the whole
        # string was searched — today's behavior, and still the best
        # available answer. But the caller stated a location that was not
        # honored, and has to hear that rather than be handed a homonym as
        # if it had been.
        hit["note"] = f"{qualifier!r} did not resolve as a place or region; searched the full text"
    return hit


# --- #431: the typo tier refuses a match it only half earned ----------------
#
# The defect: resolve_named_place("Gare du Nord") answered "Garen Du", a
# hamlet in Côtes-d'Armor, and from_to("Louvre Museum" -> "Gare du Nord")
# then routed 430 km to it and said too_far. Not a threshold that was set
# too low — the row scored 0.975, comfortably over _FUZZY_SIMILARITY_
# THRESHOLD. It scored that against "Gare du". _parse_region_suffix reads
# the trailing "Nord" as Cameroon's CM-NO, the region-filtered fuzzy pass
# finds nothing there, and the retry that drops the filter (see the #215
# block in geocode()) then scores the shortened base_query against every
# name in the table. The suffix was misread, so a third of the query was
# never matched by anything, and the caller is handed the result as a fact.
#
# So the test is not "how close is this row to the string the pass happened
# to score" but "how close is it to what the caller actually typed". Two
# measures, both needed — neither separates the sets alone:
#
#   coverage: is every query token in the row's name, or typo-close to one
#             of its words (the #374 measure, same 0.92 bar)? A recognized
#             region suffix is excluded, but only when the row genuinely
#             lies in that region ("Berekley, CA" -> Berkeley, whose
#             admin_context names California; "Gare du Nord" -> Garen Du,
#             whose context names Bretagne, does not get the exemption).
#   whole:    jaro-winkler of the row's folded name against the folded
#             query, region suffix dropped only under that same exemption.
#
# Measured on release 2026-08-19.0 (fuzzy top-1 per query, "whole" column):
#
#   KEEP  Sao Paluo -> São Paulo          0.978  covered
#         New Yrok -> New York            0.975  covered
#         Rio de Janiero -> Rio de Janeiro 0.986 covered
#         Berekley, CA -> Berkeley        0.971  covered (CA exempt)
#         Cinncinati, OH -> Cincinnati    0.965  covered (OH exempt)
#         Sna Francisco -> San Francisco  0.977  UNCOVERED ("Sna"/"san" is
#                                                0.556 as a lone token)
#   REFUSE Gare du Nord -> Garen Du       0.883  UNCOVERED ("Nord")
#         Le Marais, Paris -> Le Mauvais Pas 0.921 UNCOVERED
#         Union Station Denver -> Union Station 0.930 UNCOVERED
#         Marina Bay Sands -> Marina Bay Estates 0.931 UNCOVERED
#         Copacabana Beach -> Copacabana Bajo 0.936 UNCOVERED
#         Brandenburg Gate -> Brandenburg  0.938 UNCOVERED
#         Central Park West -> Central Park Estates 0.948 UNCOVERED
#
# Coverage alone would refuse "Sna Francisco", a correction the #215 tests
# pin. Whole-string similarity alone cannot be set anywhere: the corrections
# run down to 0.965 and the homonyms up to 0.948, but "Kuala Lampur" ->
# Kuala Lumpur sits at 0.939, below two of the refusals. Requiring BOTH to
# fail separates every measured pair: refuse only an uncovered row whose
# whole-query similarity is under 0.96. The bar sits 0.012 above the
# strongest measured homonym and 0.017 below the weakest correction that
# needs the escape hatch. The cost is a real correction whose typo is in a
# token the name does not otherwise account for and that scores under 0.96
# overall ("Kuala Lampur"); geocode() still returns and labels that one, and
# resolve_named_place's caller gets not_found with its try hint pointing at
# resolve_place with city/near context. A wrong station is worse.
#
# Single-token queries are untouched: "Berekley" has nothing to be partly
# matched against, and the whole failure mode is a query whose remainder
# went unaccounted for.
#
# The refusal runs before the #429 places leg rather than after the winner
# is picked, because that leg is gated on geocode having matched no
# division — and the hamlet is a division. Dropping it does not only stop a
# wrong answer, it uncovers the search that can give a right one: with a
# Paris session in hand, "Gare du Nord" now resolves to the station itself
# (measured 216 m from the platforms), where before the hamlet stood in
# front of it. Live-pinned in test_live.py.
_FUZZY_WHOLE_QUERY_FLOOR = 0.96


def _fuzzy_row_is_too_weak(query: str, row: dict) -> bool:
    """Whether `row` is a fuzzy match that never accounted for all of `query`."""
    if row.get("matched_by") != "fuzzy":
        return False
    tokens = [t for t in query.replace(",", " ").split() if t]
    if len(tokens) < 2:
        return False
    scored_query = query
    base, _code, region_name = _pkg._parse_region_suffix(query, _pkg._local_divisions_table())
    if region_name and base != query and _row_lies_in_region(row, region_name):
        scored_query = base
        tokens = [t for t in base.replace(",", " ").split() if t]
    if _pkg._fuzzy_name_covers_tokens(row.get("name") or "", tokens):
        return False
    whole = _pkg._jaro_winkler(
        _pkg._normalize_for_match(row.get("name") or ""), _pkg._normalize_for_match(scored_query)
    )
    return whole < _pkg._FUZZY_WHOLE_QUERY_FLOOR


def _row_lies_in_region(row: dict, region_name: str) -> bool:
    """Whether `row`'s admin chain names the region a suffix was parsed as."""
    folded = _pkg._normalize_for_match(region_name)
    return any(_pkg._normalize_for_match(ctx) == folded for ctx in (row.get("admin_context") or []))


def _fuzzy_name_covers_tokens(name: str, tokens: list[str]) -> bool:
    """Whether every token of the query is accounted for by `name`.

    The #374 measure (_fuzzy_place_covers_query) applied to division names:
    a token counts as covered when the folded name contains it outright or
    one of the name's words is within _FUZZY_SIMILARITY_THRESHOLD of it.
    Separate from that function because it folds with _normalize_for_match,
    what the division tiers and _query_divisions_fuzzy use, rather than
    overture._fold_poi_name.
    """
    folded_name = _pkg._normalize_for_match(name)
    words = folded_name.split()
    for tok in tokens:
        folded_tok = _pkg._normalize_for_match(tok)
        if not folded_tok or folded_tok in folded_name:
            continue
        if any(
            _pkg._jaro_winkler(word, folded_tok) >= _pkg._FUZZY_SIMILARITY_THRESHOLD
            for word in words
        ):  # noqa: E501
            continue
        return False
    return True


# --- #429: the places leg of an unqualified name ----------------------------
#
# The defect: resolve_named_place had no places search of its own. It read
# geocode(), whose places half is a *supplement* to the divisions search and
# is gated accordingly — stood down once an exact or fuzzy division match is
# in hand, and skipped outright against a remote dataset when no anchor can
# be derived from the query, because unanchored it is a substring scan of
# every place on Earth (#105, ~216s measured). "Louvre Museum" derives no
# anchor: "Museum" is a feature noun, and "Louvre" only prefix-matches the
# commune of Louvres, which the leading-token rule requires to be exact. So
# geocode returned nothing and every compose built on this resolver —
# from_to's ends, meeting_point's origins, optimize_route's stops,
# find_near's near — answered "no place matched" for a name the places
# theme carries six times over.
#
# resolve_place is the resolver that *does* have a places leg: its own
# reference derivation, the bundled landmark pins, the #329 last-good city,
# and a per-token bounded find_places sweep. Everything geocode's places
# half can reach, resolve_place's merge already includes (it folds
# geocode()'s own place rows in), so handing the places question to it is
# strictly more recall, never less.
#
# Scoped to "geocode found no division at all": when a division did match,
# the ranking that chose it stays untouched and the answer is byte-identical
# to before. This is a fallback, not a second opinion — a query geocode
# already answers is never re-litigated.
#
# Scoped again by _has_extra_place_context, and that one is a cost rule
# rather than a correctness rule. resolve_place re-runs geocode and then
# fans out one bounded find_places per significant token. When it would
# derive the very anchor geocode just used, that fan-out is the same search
# again at several times the price: "Nowhere At All Xyzzy" (a miss, cold
# cache) measured 12s before this leg and had not returned after five
# minutes with it. So the leg runs when resolve_place can bound the search
# somewhere geocode could not — a bundled landmark pin, or the session's
# last good city — which is the whole class this issue is about.


def _has_extra_place_context(query: str) -> bool:
    """Whether resolve_place knows a location for `query` that geocode did not.

    #469: a trailing well-known city counts. "Shibuya Station Tokyo" gives
    geocode one opaque string, and its places half returned a row named
    "Shibuya Station Tokyo. Japan" pinned 7 km east of the station — the
    top hit, so from_to routed 8.6 km for a 1.5 km walk. resolve_place
    splits the city off, anchors on its pin, searches the residual by
    category as well as by name, and grades against the residual. That is
    a bounded search (one local city lookup plus a few 20 km scans, ~2 s
    measured), never the unbounded miss the cost rule above guards
    against — that case has no recognizable city to split off.
    """
    _place_query, city, coords = _pkg._extract_city_hint(query)
    if coords is not None or city:
        return True
    last_city, _last_coords = _pkg._last_good()
    return bool(last_city) and _pkg._query_is_poi_shaped(query)


# --- perf: leg 2 speculated alongside leg 1 ----------------------------------
#
# resolve_named_place's two legs used to run in series: geocode(query) first,
# then resolve_place(query) only when geocode found no division. For a
# POI-shaped name leg 1 always misses, so both were paid in full: ~10 round
# trips, ~2.0 s with 0.2 s scans, the cold half of the c15 walk. Leg 2 now
# starts on a worker while leg 1 runs. Its writes (resolve LRU, last-city
# memory, autowarm) are deferred into a commit() that runs only if leg 2's
# answer is used, so a speculative run that leg 1 makes unnecessary leaves
# no state behind: rounds become max(leg 1, leg 2) instead of their sum.
#
# Rules: a division from leg 1 discards leg 2 without commit (its exception,
# if any, is logged at debug, not raised). A leg-1 exception discards leg 2
# and propagates, as the serial path would have. Otherwise leg 2's result is
# joined and committed, and any exception it raised surfaces exactly as the
# serial call would have raised it.
#
# The pool is bounded: a speculative leg holds one read cursor for its whole
# run, and the read pool has a fixed cursor cap (db.DEFAULT_READ_CURSORS), so
# at most _SPECULATE_WORKERS of them are ever held here. A discarded leg is
# cancelled if it has not started; a running one is not waited for.

SPECULATE_NAMED_RESOLVE = True

SPECULATE_ENV = "PLACEROOT_SPECULATE_RESOLVE"

_SPECULATE_WORKERS = 4

_SPECULATE_POOL = ThreadPoolExecutor(
    max_workers=_SPECULATE_WORKERS,
    thread_name_prefix="resolve-speculative",
)

# Legs submitted and not yet finished. Tests drain it so a discarded leg
# cannot outlive the monkeypatches of the test that started it.
_IN_FLIGHT: set[Future] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def wait_for_speculative_legs(timeout: float | None = None) -> None:
    """Block until every speculative leg started so far has finished."""
    with _IN_FLIGHT_LOCK:
        pending = list(_IN_FLIGHT)
    wait(pending, timeout=timeout)


def _speculation_enabled() -> bool:
    if not SPECULATE_NAMED_RESOLVE:
        return False
    raw = os.environ.get(SPECULATE_ENV, "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


class _SpeculativeLeg:
    """A leg-2 run in flight: join() commits and returns, discard() abandons."""

    def __init__(self, future: "Future[tuple[list[dict], object]]"):
        self._future = future

    def join(self) -> list[dict]:
        rows, commit = self._future.result()  # re-raises the leg's own exception
        commit()
        return rows

    def discard(self) -> None:
        self._future.cancel()  # no-op once running; its result is never committed
        self._future.add_done_callback(_log_discarded_leg)


def _log_discarded_leg(future: "Future") -> None:
    if not future.cancelled() and future.exception() is not None:
        _pkg.logger.debug(
            "speculative resolve_place leg failed (discarded)",
            exc_info=future.exception(),
        )


def _speculative_leg_worker(query: str):
    """Leg 2 on a worker thread: reads only; returns (rows, commit)."""
    from placeroot.geocode import _resolve  # call time: the module is loaded by now

    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(db.isolated_reads())
        except duckdb.Error:
            _pkg.logger.debug(
                "isolated cursor unavailable; speculative leg unisolated",
                exc_info=True,
            )
        return _resolve._resolve_place_impl(
            query,
            None,
            None,
            _pkg._RESOLVE_OVERFETCH,
            None,
            None,
            None,
            defer=True,
        )


def _speculate_place_leg(query: str) -> _SpeculativeLeg | None:
    """Start leg 2 for `query` now, or None when it must stay serial.

    Serial when speculation is switched off, when the extra-context gate
    fails (leg 2 would never run), or when resolve_place has been replaced
    (a test double or caller's own resolver must still be the one called).
    """
    if not _speculation_enabled():
        return None
    from placeroot.geocode import _resolve

    if _pkg.resolve_place is not _resolve.resolve_place:
        return None
    if not _pkg._has_extra_place_context(query):
        return None
    future = _SPECULATE_POOL.submit(contextvars.copy_context().run, _speculative_leg_worker, query)
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.add(future)
    future.add_done_callback(_forget_leg)
    return _SpeculativeLeg(future)


def _forget_leg(future: Future) -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.discard(future)


def _resolve_place_leg(query: str, spec: _SpeculativeLeg | None = None) -> dict | None:
    """resolve_place's top place candidate for `query`, or None.

    No ambiguity check: same reading as the anchored path in
    _resolve_inside_anchor. Several businesses sharing a name in one metro
    is the ordinary shape of the places theme rather than the "which city
    did you mean" ambiguity AmbiguousPlace exists to surface, and
    resolve_place has already ranked them by tier, distance and confidence.

    A degraded places dataset degrades this leg rather than the whole
    resolve: resolve_place raises SchemaDegraded where geocode does not, and
    a caller who used to get geocode's own answer (or an honest None) must
    not start getting an exception because a *supplementary* search failed.

    `spec` is the speculative run started by resolve_named_place; when given,
    its answer is joined and committed instead of running leg 2 again.
    """
    try:
        if spec is None:
            hits = _pkg.resolve_place(query, limit=_pkg._RESOLVE_OVERFETCH)
        else:
            hits = spec.join()
    except _pkg.overture.SchemaDegraded as e:
        _pkg.logger.info(
            "resolve_named_place: places leg unavailable (%s); using geocode's rows", e
        )  # noqa: E501
        return None
    for hit in hits:
        if hit.get("kind") == "place":
            return {
                "name": hit["name"],
                "lat": hit["lat"],
                "lon": hit["lon"],
                "id": hit["id"],
                "type": "place",
            }
    return None


# --- #427: comma-qualified names ("Le Marais, Paris") -----------------------
#
# The defect: the whole string went to geocode() as one opaque name, no
# literal tier matched it, and the #215 typo tier then fuzzed across the
# comma — "Le Marais, Paris" came back as three villages named "Le Mauvais
# Pas", the qualifier the caller supplied thrown away entirely.
#
# Which comma: the FIRST one, with everything after it kept together as the
# qualifier. The issue proposed the last comma; the first is what
# geocode_address already does (its city anchor is the whole tail), and it
# reads "Grand Army Plaza, Brooklyn, NY" the way a caller means it — head
# "Grand Army Plaza", qualifier "Brooklyn, NY", which the anchor lookup
# then resolves as a name plus a region suffix on its own. A last-comma
# split would instead go looking for something named "Grand Army Plaza,
# Brooklyn".
#
# Which commas are ours: only the ones geocode() does not already
# understand. "Austin, TX" / "London, Ontario" are a name plus a region
# suffix, which _parse_region_suffix has recognized since #46 — it searches
# the name half alone, constrained to that region, so the comma is never
# crossed and there is nothing here to improve. _split_qualifier hands those
# back as unqualified, which is what keeps that whole family (and every
# typo correction inside it, "Berekley, CA") byte-identical.
#
# Tier interaction: for the qualifiers that are ours, the full ladder
# (exact -> alt -> typo) runs on the HEAD alone, inside the anchor bound —
# so no candidate can be reached by fuzzing a string that spans the comma.
# That is the "Le Mauvais Pas" class, impossible by construction rather
# than filtered out afterwards. The one path that still searches the whole
# string is the fallback below, taken only when the qualifier resolves to
# nothing at all, and it says so in a note.
#
# Anchor bound: a candidate is inside the anchor when the anchor's resolved
# name appears in the candidate's admin_context chain, or when it sits
# within _CITY_HINT_RADIUS_M of the anchor's point. Containment alone is
# too narrow (a place row carries no chain); radius alone is wrong for a
# region or country anchor, whose centroid can be hundreds of km from every
# real answer inside it.


def _split_qualifier(query: str) -> tuple[str | None, str | None]:
    """(head, qualifier) for a comma-qualified name, else (None, None).

    (None, None) for a query with no comma, and for one whose tail is a
    region suffix geocode() already resolves against — see the block above.
    """
    head, sep, tail = query.partition(",")
    head, tail = head.strip(), tail.strip()
    if not sep or not head or not tail:
        return None, None
    if _pkg._parse_region_suffix(query, _pkg._local_divisions_table())[1] is not None:
        return None, None
    return head, tail


def _pick_named_place(query: str, rows: list[dict]) -> dict:
    """The winner among ranked candidates, or AmbiguousPlace on a same-name tie."""
    top = rows[0]
    top_name = _pkg._normalize_for_match(top["name"])
    tied = [
        r
        for r in rows
        if _pkg._normalize_for_match(r["name"]) == top_name
        and abs(r.get("rank_score", 0) - top.get("rank_score", 0)) < _AREA_RANK_EPSILON
    ]
    if len(tied) > 1:
        raise AmbiguousPlace(query, [_named_candidate(r) for r in tied[:_AREA_MAX_CANDIDATES]])
    return _named_candidate(top)


def _resolve_qualifier_anchor(text: str) -> dict | None:
    """The city/region a qualifier names, or None if it names nothing.

    The name has to match exactly (folded, or through one of #214's
    alternates): a qualifier is the caller telling us where, so answering
    it with a prefix or fuzzy neighbor is the same class of mistake this
    whole path exists to stop — "Paris, France" anchored on *Franceville*
    before this rule, because geocode ranks a city above a country and
    "France" is a prefix of the one in Gabon.

    Place-kind hits are not anchors either: a qualifier says *where*, and a
    business that happens to share the word would bound the search on a
    storefront. When the qualifier is itself comma-separated and resolves
    as a whole ("Brooklyn, NY"), that is the anchor; only if it does not
    is its last segment tried on its own.
    """
    for candidate in _qualifier_texts(text):
        folded = _pkg._normalize_for_match(candidate)
        hits = [
            h
            for h in _pkg.geocode(candidate, limit=_pkg._ANCHOR_LOOKUP_LIMIT)
            if h.get("type") != "place"
            and h.get("lat") is not None
            and h.get("lon") is not None
            and _names_qualifier(h, folded)
        ]
        if hits:
            pin = _pkg._pick_city_hint_row(hits)
            return {
                "name": pin["name"],
                "lat": pin["lat"],
                "lon": pin["lon"],
                "folded_name": _pkg._normalize_for_match(pin["name"]),
            }
    return None


def _names_qualifier(row: dict, folded: str) -> bool:
    """Whether `row` is named exactly `folded`, canonically or through a #214 alternate."""
    names = {_pkg._normalize_for_match(n) for n in (row.get("name"), row.get("matched_name")) if n}
    return folded in names


def _qualifier_texts(text: str):
    yield text
    last = text.rsplit(",", 1)[-1].strip()
    if last and last != text:
        yield last


def _inside_anchor(row: dict, anchor: dict) -> bool:
    """Whether `row` is inside the qualifier, by admin chain or by distance.

    A row that carries an admin chain is judged on it alone, never on
    distance: "Le Marais, Paris" has a locality named Le Marais 45 km
    outside Paris and administratively in Essonne, well inside any
    city-scale radius, and taking it is the same wrong answer in a nearer
    village. A row with no chain to judge — a places-theme row from
    geocode's own fallback, or a country whose chain is just itself —
    falls back to the radius.
    """
    chain = {_pkg._normalize_for_match(n) for n in (row.get("admin_context") or []) if n}
    if chain:
        return anchor["folded_name"] in chain
    distance_m = geo.haversine_m(anchor["lat"], anchor["lon"], row["lat"], row["lon"])
    return distance_m <= _pkg._CITY_HINT_RADIUS_M


def _resolve_inside_anchor(query: str, head: str, anchor: dict) -> dict:
    """The full tier ladder on `head` alone, bounded by a resolved anchor."""
    rows = [
        r
        for r in _pkg.geocode(head, limit=_pkg._ANCHORED_OVERFETCH)
        if r.get("lat") is not None and r.get("lon") is not None and _inside_anchor(r, anchor)
    ]
    if rows:
        return _pick_named_place(query, rows)

    # No division inside the anchor. The neighborhood spellings this fix
    # exists for ("Le Marais") live in the places theme, not the divisions
    # one, so the anchored places search is the answer rather than a
    # consolation prize — bounded by the same anchor, never worldwide.
    anchored = _pkg.resolve_place(
        head, near_lat=anchor["lat"], near_lon=anchor["lon"], limit=_pkg._RESOLVE_OVERFETCH
    )
    places = [p for p in anchored if p.get("kind") == "place"]
    if not places:
        raise AnchoredNotFound(query, head, anchor["name"])
    top = places[0]
    return {
        "name": top["name"],
        "lat": top["lat"],
        "lon": top["lon"],
        "id": top["id"],
        "type": "place",
    }


def _named_candidate(row: dict) -> dict:
    """A geocode row, projected to what a named-place compose needs."""
    out = {
        "name": row["name"],
        "lat": row["lat"],
        "lon": row["lon"],
        "id": row.get("id"),
        "type": row.get("type"),
    }
    if row.get("admin_context"):
        out["admin_context"] = row["admin_context"]
    return out
