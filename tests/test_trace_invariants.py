"""What a tool call is allowed to scan — asserted offline, in milliseconds.

Every latency incident in this repo has been the same *class* of bug rather
than the same instance: a query ran that could not prune what it read, or
ran at all when it could only ever come back empty. Three of them, all
found by a user hitting them in production:

- "Stanford Shopping Center": anchored on the wrong word and scanned
  Pittsburgh. 61s, no answer.
- "Eiffel Tower": a divisions name scan looking for a division by that
  name. 12.8s of a 13.1s call, and no division is called that.
- "BASIS Silicon Valley Lower School Sunnyvale": the same divisions scan
  again, because "school" was missing from the feature-noun list. 15.2s of
  21s.

Sampling latency catches those one at a time, on a real network, after
someone has already had a bad experience. Their shared property —
"something scanned that shouldn't have" — is checkable here instead:
trace.py records every scan with whether it was bounded, so these tests
assert the *shape* of a call's data access against the fixtures, with no
network, no stopwatch, and no flakiness.

The rule these encode: a scan may be unbounded only where the code
deliberately chose that and gated it. Anything else is the bug.
"""

import pytest

from placeroot import buildings, divisions, geocode, land_use, overture, trace, water

from .conftest import CENTER_LAT, CENTER_LON


@pytest.fixture
def traced():
    """Record scans for one call, then hand them back."""
    token = trace.start()
    try:
        yield trace
    finally:
        trace.reset(token)


def _scans(recorded):
    return [r for r in recorded.records() if r.kind == "scan"]


# --- point and radius tools bound everything they read ----------------------


@pytest.mark.parametrize("call", [
    pytest.param(lambda: overture.find_places(CENTER_LAT, CENTER_LON, radius_m=500),
                 id="find_places"),
    pytest.param(lambda: overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=500),
                 id="summarize_area"),
    pytest.param(lambda: buildings.buildings_at(CENTER_LAT, CENTER_LON), id="buildings_at"),
    pytest.param(lambda: buildings.summarize_buildings(CENTER_LAT, CENTER_LON, radius_m=500),
                 id="summarize_buildings"),
    pytest.param(lambda: divisions.admin_lookup(CENTER_LAT, CENTER_LON), id="admin_lookup"),
    pytest.param(lambda: land_use.land_use_at(CENTER_LAT, CENTER_LON), id="land_use_at"),
    pytest.param(lambda: water.water_near(CENTER_LAT, CENTER_LON, radius_m=500), id="water_near"),
])
def test_a_located_query_never_scans_without_a_bound(traced, call):
    """These tools are all handed a coordinate. There is no reason for any
    read they make to lack a bbox — and an unbounded read against a
    planet-scale theme is the difference between seconds and minutes."""
    call()

    unbounded = traced.unbounded_scans()
    assert not unbounded, (
        "a coordinate was supplied, so every scan should have been bounded; "
        f"unbounded: {[(r.name, r.detail) for r in unbounded]}"
    )


# --- the divisions name scan is gated, not merely slow ----------------------


def _recall_watcher(monkeypatch):
    """Make the bundled-index recall path reachable offline and count it.

    The recall only exists when the divisions table is the wheel's stage-0
    index (a subset), so point the lookup at the real bundled index — an
    in-repo parquet, so this stays offline — and stub the upstream scan the
    recall would issue. Every remaining upstream call is then the recall
    itself, which is what these two tests are actually about.
    """
    from placeroot import release as release_mod

    index = (
        __import__("pathlib").Path("src/placeroot/data/geocode-index")
        / release_mod.PINNED_RELEASE / "table.parquet"
    )
    calls = []
    monkeypatch.setattr(geocode, "_local_divisions_table", lambda: str(index))
    monkeypatch.setattr(
        geocode, "_query_divisions_from_upstream",
        lambda *a, **k: calls.append(a[0]) or [],
    )
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [])
    return calls


def test_a_feature_query_runs_no_divisions_recall_scan(monkeypatch):
    """No division is named "Eiffel Tower" or "BASIS ... Lower School", so
    the recall can only come back empty — and it was 12.8s of a 13.1s call,
    then 15.2s of a 21s one. The gate is _names_a_feature; this asserts it
    holds end to end rather than unit-testing the predicate alone."""
    calls = _recall_watcher(monkeypatch)

    geocode.geocode("Eiffel Tower", limit=5)

    assert calls == [], f"a feature query must not reach upstream divisions: {calls}"


def test_an_ordinary_unknown_name_still_recalls_upstream(monkeypatch):
    """The complement, so the gate above can't quietly become "skip
    everything": a name that could plausibly *be* a small division keeps the
    recall the bundled index's population cutoff exists to backstop."""
    calls = _recall_watcher(monkeypatch)

    geocode.geocode("Zzyzxville", limit=5)

    assert calls, "an ordinary unknown name must still reach the recall scan"


# --- the scan record itself -------------------------------------------------


def test_scans_are_recorded_with_their_source_and_bound(traced):
    overture.find_places(CENTER_LAT, CENTER_LON, radius_m=500)

    scans = _scans(traced)
    assert scans, "find_places must record at least one scan"
    first = scans[0]
    assert first.detail["bounded"] is True
    assert first.seconds >= 0.0
    assert first.detail.get("source"), "a scan should say what it read"


def test_nothing_is_recorded_when_nobody_is_tracing():
    """The contextvar default: a direct library call, or any code path with
    no tracer installed, pays one variable read and records nothing."""
    assert not trace.enabled()
    with trace.phase("free"):
        pass
    overture.find_places(CENTER_LAT, CENTER_LON, radius_m=500)
    assert trace.records() == []


def test_a_phase_is_recorded_even_when_its_body_raises(traced):
    """A phase that blew up after 30s is exactly the one worth seeing."""
    with pytest.raises(ValueError), trace.phase("doomed"):
        raise ValueError("boom")

    assert [r.name for r in traced.records()] == ["doomed"]


def test_summary_is_slowest_first_and_json_safe(traced):
    with trace.phase("quick"):
        pass
    with trace.phase("slower"):
        import time

        time.sleep(0.01)

    rows = traced.summary()
    assert [r["name"] for r in rows] == ["slower", "quick"]
    assert all(isinstance(r["seconds"], float) for r in rows)
