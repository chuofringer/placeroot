"""Speculative leg 2 in resolve_named_place: overlap, discard, errors, switches.

Built on tests/test_resolve_overlap.py's Timeline and recording fakes (0.2 s
per scan), so the critical path is the timeline, not machine speed. Wall
assertions are relative to the serial sum, never absolute seconds.
"""

import copy

import pytest

from placeroot import geocode
from placeroot.errors import UpstreamUnavailable
from placeroot.geocode import _named_places, _resolve

from .test_resolve_overlap import SHIBUYA_NAMED_CALLS, SLEEP_S, TOKYO, timeline  # noqa: F401

SERIAL_ROUNDS = 10  # resolve_named_place("Shibuya Station Tokyo") with the switch off
POI = "Shibuya Station Tokyo"


@pytest.fixture
def tl(timeline):  # noqa: F811 - the overlap suite's fixture, drained after each test
    yield timeline
    # Discarded legs keep running after their call returns. Wait for them while
    # the monkeypatches are still in place, so nothing leaks into the next test.
    _named_places.wait_for_speculative_legs(timeout=10)


def _divisions_matching_tokyo(timeline_):
    """Division fake that answers any query naming Tokyo (leg 1 then hits)."""

    def fake(query, region_code, local_table, **kw):
        args = (query, kw.get("fold_diacritics", False))
        value = [dict(TOKYO)] if "tokyo" in query.lower() else []
        return timeline_.run("_query_divisions", args, value)

    return fake


def _state():
    """The shared state a discarded leg must not touch."""
    return (
        copy.deepcopy(list(geocode._resolve_lru.items())),
        list(geocode._last_good_by_session.items()),
    )


# --- (a) POI-shaped query: the legs overlap -----------------------------------


def test_poi_query_rounds_drop_and_answer_is_unchanged(tl, monkeypatch):
    monkeypatch.setenv("PLACEROOT_SPECULATE_RESOLVE", "0")
    serial = geocode.resolve_named_place(POI)
    serial_calls = tl.call_set()
    assert tl.rounds() == SERIAL_ROUNDS

    tl.events.clear()
    geocode.clear_resolve_session()
    monkeypatch.delenv("PLACEROOT_SPECULATE_RESOLVE")
    spec = geocode.resolve_named_place(POI)

    assert spec["id"] == serial["id"] == "pl-gare-shibuya"
    assert spec == serial
    assert tl.call_set() == serial_calls == SHIBUYA_NAMED_CALLS
    assert tl.rounds() <= 6
    assert tl.wall() >= 5 * SLEEP_S * 0.9  # lower bound only: five sequential rounds
    assert tl.wall() < SERIAL_ROUNDS * SLEEP_S * 0.75  # well under the serial sum


def test_joined_leg_commits_its_writes(tl):
    """Leg 1 misses, so leg 2's answer is used and its writes are applied."""
    assert not geocode._resolve_lru
    hit = geocode.resolve_named_place(POI)
    assert hit["id"] == "pl-gare-shibuya"
    assert geocode._resolve_lru, "committed resolve LRU entry expected"
    assert geocode._last_good()[0] == "Tokyo"


# --- (b) division-shaped query: the discarded leg leaves no state -------------


def test_discarded_leg_leaves_lru_and_last_city_untouched(tl, monkeypatch):
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_matching_tokyo(tl))
    # Seed real state first, so "unchanged" means "unchanged from something".
    geocode.resolve_place("Yoyogi Park Tokyo")
    assert geocode._resolve_lru
    assert geocode._last_good_by_session

    before = _state()
    tl.events.clear()
    hit = geocode.resolve_named_place(POI)
    _named_places.wait_for_speculative_legs(timeout=10)

    assert hit["type"] != "place", "leg 1 must find a division for this test"
    # The speculative leg really ran (its own name-only call is in the timeline)
    # and was discarded, not skipped.
    assert ("_query_divisions", ("Shibuya Station", False)) in tl.call_set()
    assert _state() == before


# --- (c) switches off: exactly the serial rounds -----------------------------


def test_env_switch_off_restores_the_serial_ten_rounds(tl, monkeypatch):
    monkeypatch.setenv("PLACEROOT_SPECULATE_RESOLVE", "0")
    hit = geocode.resolve_named_place(POI)
    assert hit["id"] == "pl-gare-shibuya"
    assert tl.rounds() == SERIAL_ROUNDS
    assert tl.wall() >= SERIAL_ROUNDS * SLEEP_S * 0.9
    assert tl.call_set() == SHIBUYA_NAMED_CALLS


def test_module_constant_off_restores_the_serial_ten_rounds(tl, monkeypatch):
    monkeypatch.setattr(_named_places, "SPECULATE_NAMED_RESOLVE", False)
    monkeypatch.delenv("PLACEROOT_SPECULATE_RESOLVE", raising=False)
    hit = geocode.resolve_named_place(POI)
    assert hit["id"] == "pl-gare-shibuya"
    assert tl.rounds() == SERIAL_ROUNDS


# --- (d) errors ---------------------------------------------------------------


def test_speculative_error_is_swallowed_when_leg_one_finds_a_division(tl, monkeypatch):
    monkeypatch.setattr(geocode, "_query_divisions", _divisions_matching_tokyo(tl))
    calls = []

    def failing_impl(*args, **kwargs):
        calls.append(kwargs.get("defer"))
        raise UpstreamUnavailable("speculative scan down")

    monkeypatch.setattr(_resolve, "_resolve_place_impl", failing_impl)
    hit = geocode.resolve_named_place(POI)  # must not raise
    _named_places.wait_for_speculative_legs(timeout=10)

    assert hit["type"] != "place"
    assert calls == [True], "the speculative leg ran, and its error was not surfaced"


def test_speculative_error_surfaces_when_leg_one_misses(tl, monkeypatch):
    def failing_impl(*args, **kwargs):
        raise UpstreamUnavailable("speculative scan down")

    monkeypatch.setattr(_resolve, "_resolve_place_impl", failing_impl)
    with pytest.raises(UpstreamUnavailable, match="speculative scan down"):
        geocode.resolve_named_place(POI)


def test_leg_one_error_propagates_and_discards_leg_two(tl, monkeypatch):
    def failing_geocode(*args, **kwargs):
        raise UpstreamUnavailable("division scan down")

    monkeypatch.setattr(geocode, "geocode", failing_geocode)
    with pytest.raises(UpstreamUnavailable, match="division scan down"):
        geocode.resolve_named_place(POI)
    _named_places.wait_for_speculative_legs(timeout=10)
    assert not geocode._resolve_lru
    assert not geocode._last_good_by_session
