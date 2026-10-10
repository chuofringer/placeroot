"""Scan counts behind resolve_place's cold cost (perf/resolve-place-round-trips).

The weekday question gate's slowest leg is c15, "How far is it to walk from
Shibuya Station to Yoyogi Park?", whose cold half is two resolve_place
calls: 10.8s on a good S3 day, 16.5s on a slow one, over the 15s budget.
These tests pin what that half *does* rather than how long it takes: every
scan-shaped call is a recording fake (the shape tests/test_resolve_place.py
uses), and the number of calls is the assertion — offline, in milliseconds.

Measured on main (#507) through the same fakes, per query, for both
"Shibuya Station Tokyo" and "Yoyogi Park Tokyo":

    BEFORE  _query_divisions 3 (city pin, pinned literal, pinned folded —
            all local-index reads), _query_places_fallback 1 (geocode()'s
            own anchored places scan), find_places 3 (whole phrase, the
            word "Shibuya"/"Yoyogi", the #469 type scan) — 4 places-theme
            scans, run one after another under overture._conn_lock, and
            each find_places miss paying its alt-name and fuzzy tiers too
            (10 places-theme reads when nothing matches by name; see
            test_fuzzy_tier_is_skipped_for_single_word_scans).
    AFTER   _query_divisions 3, _query_places_fallback 1, find_places 2 —
            the phrase and type scans run side by side, the type scan's
            exact hit settles the query, and the single-word scan never
            runs (3 places-theme scans; 7 reads when nothing matches by
            name, with no fuzzy tier on any single-word scan).

The top result is unchanged: a single-word scan's row is at best a
"prefix" match, and the row that stood it down is an "exact" one.
"""

import contextlib
import threading
import time

import pytest

from placeroot import geocode, overture, trace

from .conftest import FIXTURE_PATH


def _needs_readable_places_fixture():
    """The real find_places needs DuckDB's httpfs even for a local parquet
    file; where it cannot be loaded (an offline sandbox) every fixture-
    backed test in the suite fails with UpstreamUnavailable, and these two
    would only add to that set."""
    if overture.probe_schema(str(FIXTURE_PATH)) is None:
        pytest.skip("places fixture unreadable here (httpfs unavailable)")


TOKYO = {
    "id": "div-tokyo",
    "name": "Tokyo",
    "subtype": "locality",
    "country": "JP",
    "region": "JP-13",
    "lat": 35.6895,
    "lon": 139.6917,
    "admin_context": ["Japan"],
    "population": 13_960_000,
}
# What Overture actually holds (see geocode._TYPE_WORD_CATEGORIES): the
# station is filed under its category with a non-English primary name, and
# no row anywhere is named "Shibuya Station".
GARE_DE_SHIBUYA = {
    "id": "pl-gare-shibuya",
    "name": "Gare de Shibuya",
    "category": "train_station",
    "basic_category": "train_station",
    "operating_status": "open",
    "confidence": 0.9,
    "brand": None,
    "has_website": None,
    "has_phone": None,
    "lat": 35.658,
    "lon": 139.7016,
    "distance_m": 100,
}
SHIBUYA_SHOP = {
    "id": "pl-shibuya-shop",
    "name": "Shibuya Mark City",
    "category": "shopping_center",
    "basic_category": "shopping_center",
    "operating_status": "open",
    "confidence": 0.95,
    "brand": None,
    "has_website": None,
    "has_phone": None,
    "lat": 35.6585,
    "lon": 139.7005,
    "distance_m": 120,
}
YOYOGI = {
    "id": "pl-yoyogi",
    "name": "Yoyogi Park",
    "category": "park",
    "basic_category": "park",
    "operating_status": "open",
    "confidence": 0.9,
    "brand": None,
    "has_website": None,
    "has_phone": None,
    "lat": 35.6717,
    "lon": 139.6949,
    "distance_m": 100,
}


def _place_match(name, rows):
    n = (name or "").lower()
    return [dict(r) for r in rows if n and n in r["name"].lower()]


@pytest.fixture
def recorder(monkeypatch):
    """Every scan-shaped call resolve_place can make, recorded by name.

    find_places answers like the Tokyo data does: the station is reachable
    only through the category scan, "Yoyogi Park" literally by name.
    """
    calls: list[tuple] = []

    def fake_find_places(
        lat, lon, radius_m=1000, category=None, name=None, limit=10, categories=None, **kw
    ):
        calls.append(
            (
                "find_places",
                name,
                tuple(categories or ()),
                kw.get("fuzzy_fallback", True),
                threading.get_ident(),
            )
        )
        if categories:
            return _place_match(name, [GARE_DE_SHIBUYA, YOYOGI])
        return _place_match(name, [SHIBUYA_SHOP, YOYOGI])

    def fake_divisions(query, region_code, local_table, **kw):
        calls.append(("_query_divisions", query, kw.get("fold_diacritics", False)))
        return [dict(TOKYO)] if query.strip().lower() == "tokyo" else []

    def fake_fuzzy(*a, **k):
        calls.append(("_query_divisions_fuzzy", a[1] if len(a) > 1 else None))
        return []

    def fake_places_fallback(query, anchor=None, also=None, schedule_tiles=True):
        calls.append(("_query_places_fallback", query))
        return []

    def fake_multi_anchor(*a, **k):
        calls.append(("_query_places_multi_anchor", a[0] if a else None))
        return [], None

    monkeypatch.setattr(overture, "find_places", fake_find_places)
    monkeypatch.setattr(geocode, "_query_divisions", fake_divisions)
    monkeypatch.setattr(geocode, "_query_divisions_fuzzy", fake_fuzzy)
    monkeypatch.setattr(geocode, "_query_places_fallback", fake_places_fallback)
    monkeypatch.setattr(geocode, "_query_places_multi_anchor", fake_multi_anchor)
    geocode.clear_resolve_session()
    yield calls
    geocode.clear_resolve_session()


def _counts(calls):
    out: dict[str, int] = {}
    for c in calls:
        out[c[0]] = out.get(c[0], 0) + 1
    return out


@pytest.mark.parametrize(
    "query, top",
    [("Shibuya Station Tokyo", "pl-gare-shibuya"), ("Yoyogi Park Tokyo", "pl-yoyogi")],
)
def test_c15_cold_half_scan_counts(recorder, query, top):
    """The two c15 resolves: BEFORE find_places 3 / AFTER 2 per query (the
    module docstring has the full before/after table)."""
    results = geocode.resolve_place(query)
    assert results and results[0]["id"] == top and results[0]["match"] == "exact"
    assert _counts(recorder) == {
        "_query_divisions": 3,
        "_query_places_fallback": 1,
        "find_places": 2,
    }
    scans = [c for c in recorder if c[0] == "find_places"]
    # First round only: the whole phrase (all tiers) and the type scan
    # (fuzzy tier off) — no single-word scan after a confident hit.
    # Round 1 runs these two in parallel, so the recorder sees them in
    # completion order: compare as a set, not a sequence.
    assert sorted((c[1], bool(c[2]), c[3]) for c in scans) == sorted(
        [
            (query.rsplit(" ", 1)[0], False, True),
            (query.split(" ", 1)[0], True, False),
        ]
    )


def test_word_scans_still_run_when_the_first_round_is_not_confident(recorder, monkeypatch):
    """A phrase that only finds substring rows and a type scan that finds
    nothing: every single-word scan runs, with the fuzzy tier off, and the
    rows merge in the serial loop's order."""

    def fake_find_places(
        lat, lon, radius_m=1000, category=None, name=None, limit=10, categories=None, **kw
    ):
        recorder.append(
            ("find_places", name, tuple(categories or ()), kw.get("fuzzy_fallback", True))
        )
        if categories:
            return []
        return _place_match(name, [SHIBUYA_SHOP])

    monkeypatch.setattr(overture, "find_places", fake_find_places)
    results = geocode.resolve_place("Shibuya Hikarie Tokyo")
    scans = [(c[1], c[3]) for c in recorder if c[0] == "find_places"]
    assert scans == [("Shibuya Hikarie", True), ("Shibuya", False), ("Hikarie", False)]
    assert [r["id"] for r in results] == ["pl-shibuya-shop"]


def test_word_scans_run_in_parallel_on_their_own_cursors(recorder, monkeypatch):
    """Three word scans, each holding for 50ms, overlap in time on more
    than one thread, each inside db.isolated_reads — and their rows still
    come back in token order.

    db.isolated_reads is replaced by a recording no-op: opening a real
    private cursor needs the shared DuckDB instance, which offline cannot
    load httpfs — that fallback (scan anyway, unisolated) is what
    _run_place_scans does then, and not what this test is about."""
    from placeroot import db

    entered: list[int] = []
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()

    @contextlib.contextmanager
    def fake_isolated_reads():
        entered.append(threading.get_ident())
        yield

    def slow_find_places(
        lat, lon, radius_m=1000, category=None, name=None, limit=10, categories=None, **kw
    ):
        recorder.append(
            (
                "find_places",
                name,
                tuple(categories or ()),
                kw.get("fuzzy_fallback", True),
                threading.get_ident(),
            )
        )
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        if categories or " " in name:
            return []  # the phrase and type scans miss; only the words answer
        return [dict(SHIBUYA_SHOP, id=f"pl-{name.lower()}", name=f"{name} Place")]

    monkeypatch.setattr(db, "isolated_reads", fake_isolated_reads)
    monkeypatch.setattr(overture, "find_places", slow_find_places)
    results = geocode.resolve_place("Alpha Bravo Charlie Tokyo", limit=5)
    scans = [c for c in recorder if c[0] == "find_places"]
    assert sorted(c[1] for c in scans) == sorted(
        ["Alpha Bravo Charlie", "Alpha", "Bravo", "Charlie"]
    )
    assert active["peak"] > 1, "word scans serialized"
    assert len({c[4] for c in scans[1:]}) > 1
    assert len(entered) == 3 and len(set(entered)) > 1
    assert [r["id"] for r in results] == ["pl-alpha", "pl-bravo", "pl-charlie"]


def test_run_place_scans_keeps_order_and_reraises_the_first_failure():
    def ok(v):
        return lambda: [v]

    def boom():
        raise overture.UpstreamUnavailable("s3 down")

    assert geocode._run_place_scans([]) == []
    assert geocode._run_place_scans([ok(1)]) == [[1]]
    assert geocode._run_place_scans([ok(1), ok(2), ok(3), ok(4), ok(5)]) == [
        [1],
        [2],
        [3],
        [4],
        [5],
    ]
    with pytest.raises(overture.UpstreamUnavailable):
        geocode._run_place_scans([ok(1), boom, ok(3)])


def test_find_places_kwargs_matches_the_installed_double(monkeypatch):
    """The six-parameter doubles the suite uses must keep working: a newer
    keyword is passed only when the callee can take it."""
    monkeypatch.setattr(
        overture,
        "find_places",
        lambda lat, lon, radius_m=1000, category=None, name=None, limit=10: [],
    )
    assert geocode._find_places_kwargs(fuzzy_fallback=False) == {}
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [])
    assert geocode._find_places_kwargs(fuzzy_fallback=False) == {"fuzzy_fallback": False}
    monkeypatch.undo()
    assert geocode._find_places_kwargs(fuzzy_fallback=False) == {"fuzzy_fallback": False}


def test_fuzzy_tier_is_skipped_for_single_word_scans(monkeypatch):
    """Against the real find_places (the committed NYC fixture, where no
    Tokyo-box name matches), each scan's tiers are visible as trace
    records. BEFORE: 3 scans x (literal + alt-name + fuzzy) = 9 places
    reads. AFTER: the phrase keeps all three tiers (a typo in the whole
    query is what #373's fuzzy tier is for); the word and type scans stop
    after the alt-name tier: 7 reads, no fuzzy tier on either."""
    _needs_readable_places_fixture()
    monkeypatch.setattr(
        geocode,
        "_query_divisions",
        lambda query, region_code, local_table, **kw: (
            [dict(TOKYO)] if query.strip().lower() == "tokyo" else []
        ),
    )
    monkeypatch.setattr(geocode, "_query_divisions_fuzzy", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [])
    # Not confident (nothing matches), so every scan runs: the phrase with
    # all three tiers, the word and the type scan with two each.
    monkeypatch.setattr(geocode, "_CONFIDENT_PLACE_LABEL", "never")
    geocode.clear_resolve_session()
    token = trace.start()
    try:
        assert geocode.resolve_place("Shibuya Station Tokyo") == []
        scans = [r.name for r in trace.records() if r.kind == "scan"]
    finally:
        trace.reset(token)
        geocode.clear_resolve_session()
    places = [s for s in scans if s.startswith("places ")]
    assert sorted(places) == sorted(
        ["places radius scan", "places alt-name scan", "places fuzzy scan"]  # phrase
        + ["places radius scan", "places alt-name scan"]  # "Shibuya"
        + ["places radius scan", "places alt-name scan"]  # type scan
    ), places
    assert places.count("places fuzzy scan") == 1


def test_fuzzy_fallback_keyword_defaults_to_every_tier():
    """find_places itself: the default runs all three tiers on a miss,
    fuzzy_fallback=False stops after the alt-name tier."""
    _needs_readable_places_fixture()
    token = trace.start()
    try:
        overture.find_places(35.66, 139.70, radius_m=500, name="Nosuchplace", limit=3)
        default = [r.name for r in trace.records() if r.kind == "scan"]
    finally:
        trace.reset(token)
    token = trace.start()
    try:
        overture.find_places(
            35.66,
            139.70,
            radius_m=500,
            name="Nosuchplace",
            limit=3,
            fuzzy_fallback=False,
        )
        gated = [r.name for r in trace.records() if r.kind == "scan"]
    finally:
        trace.reset(token)
    assert default[-3:] == ["places radius scan", "places alt-name scan", "places fuzzy scan"]
    assert gated[-2:] == ["places radius scan", "places alt-name scan"]
    assert "places fuzzy scan" not in gated


def test_pinned_confident_literal_skips_the_folded_pass(monkeypatch):
    """geocode() under a near box: an exact, populated literal division
    stands the diacritic-folded pass down (perf, _CONFIDENT_TIER); with a
    weak literal answer the folded pass runs exactly as before (#221)."""
    calls: list[tuple] = []
    rows = {"strong": [dict(TOKYO)], "weak": [dict(TOKYO, population=None)]}
    state = {"mode": "strong"}

    def fake_divisions(query, region_code, local_table, **kw):
        calls.append((query, kw.get("fold_diacritics", False)))
        return [dict(r) for r in rows[state["mode"]]] if not kw.get("fold_diacritics") else []

    monkeypatch.setattr(geocode, "_query_divisions", fake_divisions)
    monkeypatch.setattr(geocode, "_query_divisions_fuzzy", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_region_population_lookup", lambda t: {})
    monkeypatch.setattr(geocode, "_is_bundled_table", lambda t: False)
    monkeypatch.setattr(geocode, "_local_alt_names_table", lambda t: None)
    near = (35.6895, 139.6917, 50_000)

    out = geocode.geocode_detailed("Tokyo", near=near, local_table="local-divisions")
    assert out["results"][0]["id"] == "div-tokyo"
    assert calls == [("Tokyo", False)]

    calls.clear()
    state["mode"] = "weak"
    geocode.geocode_detailed("Tokyo", near=near, local_table="local-divisions")
    assert calls == [("Tokyo", False), ("Tokyo", True)]

    # Unpinned, a local table still runs the folded pass unconditionally (#221).
    calls.clear()
    state["mode"] = "strong"
    geocode.geocode_detailed("Tokyo", local_table="local-divisions")
    assert calls == [("Tokyo", False), ("Tokyo", True)]
