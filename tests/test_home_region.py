"""#406: PLACEROOT_HOME resolution bias — explicit config only, a bias never
a filter, always disclosed when it changes an answer.

Uses the same Springfield IL/MA pair test_geocode_ranking.py's corpus pins
(#47): fixture-only, real populations, MA (155929) outranks IL (114230) by
default. Setting PLACEROOT_HOME to Springfield, IL's own area is the
fixture-friendly Springfield-class ambiguity this feature exists for.
"""

from placeroot import autowarm, geocode, home_region, server


def test_no_home_configured_is_byte_identical(monkeypatch):
    """No PLACEROOT_HOME -> geocode's ranking and rank_score are untouched."""
    baseline = geocode.geocode_detailed("Springfield")
    assert home_region.get_home_region() is None
    result = geocode.geocode_detailed("Springfield")
    assert result == baseline
    assert result["results"][0]["admin_context"] == ["United States", "Massachusetts"]
    assert "note" not in result


def test_home_bias_breaks_springfield_tie(monkeypatch):
    monkeypatch.setenv("PLACEROOT_HOME", "Springfield, IL")
    result = geocode.geocode_detailed("Springfield")
    top = result["results"][0]
    assert top["admin_context"] == ["United States", "Illinois"]
    assert "note" in result
    assert result["note"].startswith(home_region.DISCLOSURE_PREFIX)
    assert "Springfield" in result["note"]
    assert "near hint" in result["note"] or "city/near" in result["note"]


def test_home_bias_never_filters_out_distant_results(monkeypatch):
    """The distant match is still in the list, just not first."""
    monkeypatch.setenv("PLACEROOT_HOME", "Springfield, IL")
    result = geocode.geocode_detailed("Springfield", limit=5)
    admin_contexts = [r["admin_context"] for r in result["results"]]
    assert ["United States", "Massachusetts"] in admin_contexts
    assert ["United States", "Illinois"] in admin_contexts


def test_home_bias_does_not_override_an_explicit_region_suffix(monkeypatch):
    """An explicit, unambiguous query still resolves to its real answer —
    the bias only breaks ties, it never relabels a differently-scoped
    query's own answer."""
    monkeypatch.setenv("PLACEROOT_HOME", "Springfield, IL")
    result = geocode.geocode_detailed("Springfield, MA")
    assert result["results"][0]["admin_context"] == ["United States", "Massachusetts"]


def test_unresolvable_home_disables_bias_without_error(monkeypatch, caplog):
    monkeypatch.setenv("PLACEROOT_HOME", "Zzzznotarealplacexyz123")
    assert home_region.get_home_region() is None
    result = geocode.geocode_detailed("Springfield")
    assert result["results"][0]["admin_context"] == ["United States", "Massachusetts"]
    assert "note" not in result


def test_empty_home_env_is_a_noop(monkeypatch):
    monkeypatch.setenv("PLACEROOT_HOME", "   ")
    assert home_region.get_home_region() is None


def test_home_resolution_is_cached(monkeypatch):
    monkeypatch.setenv("PLACEROOT_HOME", "Springfield, IL")
    first = home_region.get_home_region()
    assert first is not None
    monkeypatch.setenv("PLACEROOT_HOME", "Springfield, MA")
    # Still cached — changing the env var mid-process doesn't re-resolve.
    second = home_region.get_home_region()
    assert second == first


def test_roots_path_is_a_documented_stub():
    assert home_region.resolve_home_from_roots() is None


def test_env_beats_roots(monkeypatch):
    monkeypatch.setattr(home_region, "resolve_home_from_roots", lambda: "Springfield, MA")
    monkeypatch.setenv("PLACEROOT_HOME", "Springfield, IL")
    home = home_region.get_home_region()
    assert home is not None
    assert home["name"] == "Springfield"
    assert home["lon"] < -85  # Illinois, not Massachusetts


def test_in_home_region_false_with_no_home(monkeypatch):
    assert home_region.in_home_region(39.78, -89.65) is False


def test_resolve_place_inherits_the_bias(monkeypatch):
    """resolve_place merges geocode()'s (now home-biased) division matches,
    so it shares the ranking layer rather than needing its own bias logic."""
    monkeypatch.setenv("PLACEROOT_HOME", "Springfield, IL")
    rows = geocode.resolve_place("Springfield", limit=5)
    division_rows = [r for r in rows if r["kind"] == "division" and r["name"] == "Springfield"]
    assert division_rows
    assert division_rows[0]["admin_context"] == ["United States", "Illinois"]


def test_kick_home_autowarm_schedules_when_home_resolves(monkeypatch):
    seen = []
    monkeypatch.setattr(autowarm, "schedule_autowarm", lambda lat, lon: seen.append((lat, lon)))
    monkeypatch.setenv("PLACEROOT_HOME", "Springfield, IL")
    home_region.kick_home_autowarm()
    home = home_region.get_home_region()
    # Springfield, IL is itself a city-scale hit, so resolving the home text
    # (via geocode()) also fires the *existing* per-resolve autowarm kick
    # (autowarm.maybe_autowarm_hit) — kick_home_autowarm's own explicit
    # schedule is a second, harmless call (deduped for real in production
    # by autowarm's in-flight/marker checks, bypassed here by the
    # monkeypatch). Every call must name the same home point.
    assert seen
    assert all(call == (home["lat"], home["lon"]) for call in seen)


def test_kick_home_autowarm_is_a_noop_without_home(monkeypatch):
    seen = []
    monkeypatch.setattr(autowarm, "schedule_autowarm", lambda lat, lon: seen.append((lat, lon)))
    home_region.kick_home_autowarm()
    assert seen == []


def test_warm_home_async_does_not_block(monkeypatch):
    """server._warm_home_async starts a daemon thread and returns immediately."""
    import time

    entered = []

    def slow():
        entered.append(1)
        time.sleep(0.2)

    monkeypatch.setattr(home_region, "kick_home_autowarm", slow)
    t0 = time.perf_counter()
    server._warm_home_async()
    assert time.perf_counter() - t0 < 0.1
