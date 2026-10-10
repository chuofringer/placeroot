"""Overlap of resolve_place's S3 scans, measured with 0.2 s recording fakes.

Same approach as tests/test_resolve_place_latency.py: every scan-shaped call
is a fake that sleeps 0.2 s and records (name, args, start, end), so the
critical path is the timeline, not machine speed. "Rounds" are maximal runs
of calls whose intervals overlap, i.e. the sequential steps from call entry
to return. Measured on main (#511) with these fakes:

    resolve_place("Shibuya Station Tokyo")  (one resolve_place call)
      BEFORE  5 rounds, 2 of them places-theme: divisions pin ("Tokyo"),
              divisions literal, divisions folded (#221, only when the literal
              is weak), _query_places_fallback (geocode()'s anchored scan) —
              then round 1, find_places phrase || type scan side by side.
              Round 2 (single-word scans) is skipped: round 1 is confident.
              Wall ~1.0 s.
      AFTER   unchanged. Round 1's phrase scan only runs when the fallback's
              rows did not already cover the limit, and the type scan's token
              is pruned against geocode()'s own top division. Both depend on
              the fallback's return value, so they cannot start earlier
              without adding or dropping calls. The divisions chain is
              data-dependent too: the pin feeds the near search, and the
              folded pass depends on the literal's result.

    resolve_named_place("Shibuya Station Tokyo")  (what route/from_to call per name)
      BEFORE  10 rounds, 3 of them places-theme. Leg 1 is geocode(full query):
              4 division rounds and the anchored fallback. Leg 2 is
              _resolve_place_leg -> resolve_place: the pin, literal, folded,
              fallback, and round 1 — 5 more rounds. Leg 2 runs only when leg 1
              found no division and the query carries extra place context, and
              resolve_place writes the resolve LRU and the last-good city
              memory, so running it speculatively could leave session state
              behind. Wall ~2.0 s. Not overlapped.

    _resolve_route_ends (route/from_to, the server pair path)
      Two plain names: BEFORE already overlaps — _resolve_pair runs both named
              resolves on their own threads and cursors, and the timeline
              interleaves them. Wall ~2.0 s (one named resolve), not ~4.0 s.
      A name and a GERS id: BEFORE sequential — the name's ~2.0 s resolve,
              then the id lookup (0.2 s): 11 rounds, wall ~2.2 s.
      AFTER   both ends side by side (_resolve_ref_pair): 10 rounds, wall
              ~2.0 s. The origin's error still wins.
"""

import contextlib
import threading
import time

import pytest

from placeroot import db, geocode, gers, overture, server

from .test_resolve_place_latency import GARE_DE_SHIBUYA, SHIBUYA_SHOP, TOKYO, YOYOGI, _place_match

SLEEP_S = 0.2
_JITTER_S = 0.05
GERS_ID = "0123456789abcdef0123456789abcdef"


class Timeline:
    """Recorded (name, args, start, end) for every fake call."""

    def __init__(self):
        self.events: list[tuple] = []
        self._lock = threading.Lock()

    def run(self, name, args, value):
        start = time.monotonic()
        time.sleep(SLEEP_S)
        end = time.monotonic()
        with self._lock:
            self.events.append((name, args, start, end))
        return value

    def call_set(self):
        return sorted((name, args) for name, args, _, _ in self.events)

    def rounds(self, names=None):
        """Sequential rounds: maximal runs of calls that run side by side.

        A call starting less than _JITTER_S before the round's last end
        still belongs to the round it is sequential with, not to the one
        before it — thread wake-up jitter is milliseconds, the fakes 200 ms.
        """
        spans = sorted(
            (s, e) for name, _, s, e in self.events if names is None or name in names
        )
        groups, cur_end = 0, None
        for s, e in spans:
            if cur_end is None or s >= cur_end - _JITTER_S:
                groups += 1
                cur_end = e
            else:
                cur_end = max(cur_end, e)
        return groups

    def wall(self):
        return max(e for *_, e in self.events) - min(s for _, _, s, _ in self.events)

    def overlapping(self, a_name, b_name):
        a = [(s, e) for n, _, s, e in self.events if n == a_name]
        b = [(s, e) for n, _, s, e in self.events if n == b_name]
        return any(s1 < e2 and s2 < e1 for s1, e1 in a for s2, e2 in b)


@pytest.fixture
def timeline(monkeypatch):
    tl = Timeline()

    def fake_find_places(lat, lon, radius_m=1000, category=None, name=None, limit=10,
                         categories=None, **kw):
        args = (name, tuple(categories or ()), kw.get("fuzzy_fallback", True))
        if categories:
            value = _place_match(name, [GARE_DE_SHIBUYA, YOYOGI])
        else:
            value = _place_match(name, [SHIBUYA_SHOP, YOYOGI])
        return tl.run("find_places", args, value)

    def fake_divisions(query, region_code, local_table, **kw):
        args = (query, kw.get("fold_diacritics", False))
        value = [dict(TOKYO)] if query.strip().lower() == "tokyo" else []
        return tl.run("_query_divisions", args, value)

    def fake_places_fallback(query, anchor=None, also=None, schedule_tiles=True):
        args = (query, anchor is not None and round(anchor[0], 2), also, schedule_tiles)
        return tl.run("_query_places_fallback", args, [])

    def fake_gers(text):
        return tl.run("gers_lookup", (text,), {
            "id": text, "name": "Yoyogi Park", "lat": 35.6717, "lon": 139.6949,
        })

    @contextlib.contextmanager
    def no_private_cursor():
        # Offline a private cursor cannot open httpfs; the scans run unisolated
        # either way, which is all these timing tests need.
        yield

    monkeypatch.setattr(overture, "find_places", fake_find_places)
    monkeypatch.setattr(geocode, "_query_divisions", fake_divisions)
    monkeypatch.setattr(geocode, "_query_divisions_fuzzy", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_query_places_fallback", fake_places_fallback)
    monkeypatch.setattr(geocode, "_query_places_multi_anchor", lambda *a, **k: ([], None))
    monkeypatch.setattr(gers, "gers_lookup", fake_gers)
    monkeypatch.setattr(db, "isolated_reads", no_private_cursor)
    geocode.clear_resolve_session()
    yield tl
    geocode.clear_resolve_session()


# resolve_place("Shibuya Station Tokyo") alone: BEFORE and AFTER (unchanged).
SHIBUYA_PLACE_CALLS = sorted([
    ("_query_divisions", ("Tokyo", False)),
    ("_query_divisions", ("Shibuya Station", False)),
    ("_query_divisions", ("Shibuya Station", True)),
    ("_query_places_fallback", ("Shibuya Station", 35.69, "Shibuya Station", True)),
    ("find_places", ("Shibuya Station", (), True)),
    ("find_places", ("Shibuya", ("train_station", "metro_station",
                                 "light_rail_and_subway_stations"), False)),
])

# resolve_named_place("Shibuya Station Tokyo"): BEFORE and AFTER (unchanged).
SHIBUYA_NAMED_CALLS = sorted([
    ("_query_divisions", ("Shibuya Station Tokyo", False)),
    ("_query_divisions", ("Shibuya Station Tokyo", True)),
    ("_query_divisions", ("Station Tokyo", False)),
    ("_query_divisions", ("Tokyo", False)),
    ("_query_places_fallback", ("Shibuya Station", 35.69, "Shibuya Station Tokyo", False)),
    ("_query_divisions", ("Tokyo", False)),
    ("_query_divisions", ("Shibuya Station", False)),
    ("_query_divisions", ("Shibuya Station", True)),
    ("_query_places_fallback", ("Shibuya Station", 35.69, "Shibuya Station", True)),
    ("find_places", ("Shibuya Station", (), True)),
    ("find_places", ("Shibuya", ("train_station", "metro_station",
                                 "light_rail_and_subway_stations"), False)),
])


def test_single_resolve_place_critical_path_is_unchanged(timeline):
    """BEFORE and AFTER: 5 sequential rounds (2 places-theme) and ~1.0 s."""
    results = geocode.resolve_place("Shibuya Station Tokyo")
    assert results and results[0]["id"] == "pl-gare-shibuya"
    assert results[0]["match"] == "exact"
    assert timeline.rounds() == 5
    assert timeline.rounds({"find_places", "_query_places_fallback"}) == 2
    # Lower bound only: 5 sequential rounds cannot finish faster than 5 sleeps.
    # No upper bound — a shared CI runner adds arbitrary wall time; the round
    # count above is the structural claim.
    assert timeline.wall() >= 5 * SLEEP_S * 0.9
    assert timeline.call_set() == SHIBUYA_PLACE_CALLS


def test_named_place_is_two_serial_legs_and_is_unchanged(timeline):
    """BEFORE and AFTER: resolve_named_place runs geocode(full query) and then,
    when no division matched, resolve_place. 10 rounds, ~2.0 s."""
    hit = geocode.resolve_named_place("Shibuya Station Tokyo")
    assert hit["id"] == "pl-gare-shibuya"
    assert timeline.rounds() == 10
    assert timeline.rounds({"find_places", "_query_places_fallback"}) == 3
    assert timeline.wall() >= 10 * SLEEP_S * 0.9  # lower bound only; a shared runner adds arbitrary wall time
    assert timeline.call_set() == SHIBUYA_NAMED_CALLS


def test_pair_of_plain_names_already_overlaps(timeline):
    """BEFORE and AFTER: the two named resolves interleave (one pair costs
    about one named resolve, ~2.0 s, not two of them, ~4.0 s)."""
    origin, dest, error = server._resolve_route_ends("Shibuya Station Tokyo", "Yoyogi Park Tokyo")
    assert error is None
    assert origin["id"] == "pl-gare-shibuya"
    assert dest["id"] == "pl-yoyogi"
    assert timeline.overlapping("_query_divisions", "_query_divisions")
    # Overlap shows up as the round count of ONE named resolve (10), not two
    # in series (20); wall time on a shared runner is not a reliable witness.
    assert timeline.rounds() == 10


def test_pair_of_name_and_gers_id_overlaps(timeline):
    """BEFORE: the name resolve (~2.0 s) and the GERS lookup run in turn
    (11 rounds, ~2.2 s). AFTER: both ends side by side (10 rounds, ~2.0 s);
    the name's own calls are the same set as a lone resolve_named_place."""
    origin, dest, error = server._resolve_route_ends("Shibuya Station Tokyo", GERS_ID)
    assert error is None
    assert origin["id"] == "pl-gare-shibuya"
    assert dest["matched_by"] == "gers_id"
    assert timeline.overlapping("gers_lookup", "_query_divisions"), "ends ran in turn"
    assert timeline.rounds() == 10
    assert timeline.wall() >= 10 * SLEEP_S * 0.9  # lower bound only; a shared runner adds arbitrary wall time
    name_calls = [c for c in timeline.call_set() if c[0] != "gers_lookup"]
    assert name_calls == SHIBUYA_NAMED_CALLS


def test_failing_origin_still_reports_origin(timeline, monkeypatch):
    """An unresolvable origin is the error returned, whatever the GERS end does."""
    monkeypatch.setattr(geocode, "resolve_named_place", lambda *a, **k: None)
    origin, dest, error = server._resolve_route_ends("Nowhere at all", GERS_ID)
    assert origin is None and dest is None
    assert error["field"] == "from"
