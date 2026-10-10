"""Bare trailing-word qualifiers, the resolve cache's limit, tier folding,
explicit country= on the degrade paths, word-boundary prefixes, and the
per-table memos — all exercised offline against the pure helpers (and, where
a whole resolve is needed, with the query functions monkeypatched the way
test_resolve_place.py does)."""

import threading

import duckdb
import pytest

from placeroot import db, geo, geocode, overture

# ---------------------------------------------------------------------------
# bare trailing words are not qualifiers of a bare modifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["West Virginia", "Hotel California", "New Jersey", "New Mexico", "Northern Ireland"],
)
def test_a_division_name_with_a_modifier_head_is_not_split(query):
    assert geocode._parse_region_suffix(query, None) == (query, None, None)
    assert geocode._parse_country_suffix(query, None) == (query, None, None)


def test_a_comma_qualifier_still_splits():
    assert geocode._parse_region_suffix("Paris, Texas", None) == ("Paris", "US-TX", "Texas")


def test_a_bare_region_after_a_real_name_still_splits():
    assert geocode._parse_region_suffix("Portland Oregon", None) == (
        "Portland", "US-OR", "Oregon",
    )
    assert geocode._parse_country_suffix("Portland Jersey", None) == ("Portland", "JE", "Jersey")


def test_a_query_that_names_a_division_exactly_is_not_split(monkeypatch):
    """Gate 2: a plausible head is still no reason to split a string that
    is itself a division's full name — the probe is the local index."""
    probed = []

    def fake_probe(name, local_table):
        probed.append((name, local_table))
        return name == "Lake Charles Louisiana"

    monkeypatch.setattr(geocode, "_division_named_exactly", fake_probe)
    assert geocode._parse_region_suffix("Lake Charles Louisiana", "t.parquet") == (
        "Lake Charles Louisiana", None, None,
    )
    assert geocode._parse_region_suffix("Baton Rouge Louisiana", "t.parquet") == (
        "Baton Rouge", "US-LA", "Louisiana",
    )
    # Only reached for the bare reading, and only once the suffix resolved
    # and the head passed gate 1 — never for a comma, never for "West".
    geocode._parse_region_suffix("Paris, Texas", "t.parquet")
    geocode._parse_region_suffix("West Virginia", "t.parquet")
    assert [p[0] for p in probed] == ["Lake Charles Louisiana", "Baton Rouge Louisiana"]


def test_the_exact_name_probe_is_cached_but_not_its_failures(monkeypatch):
    geocode._division_named_exactly_cached.cache_clear()
    calls = []
    outcomes = iter([duckdb.Error("transient"), ("x",), None])

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append(params["exact"])
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome

            class R:
                def fetchone(self_inner):
                    return outcome

            return R()

    monkeypatch.setattr(overture, "conn", lambda: FakeConn())
    assert geocode._division_named_exactly("Oregon", "t.parquet") is False  # failed probe
    assert geocode._division_named_exactly("Oregon", "t.parquet") is True   # re-probed
    assert geocode._division_named_exactly("oregon", "t.parquet") is True   # cached
    assert geocode._division_named_exactly("Nowhere", "t.parquet") is False
    assert calls == ["oregon", "oregon", "nowhere"]
    assert geocode._division_named_exactly("Oregon", None) is False


# ---------------------------------------------------------------------------
# resolve cache: the key carries no limit, so the value must not either
# ---------------------------------------------------------------------------


def _three_places(lat, lon, radius_m=1000, category=None, name=None, limit=10):
    return [
        {
            "id": f"place-{i}", "name": f"Example {i}", "category": "cafe",
            "basic_category": "cafe", "operating_status": "open",
            "confidence": 0.5, "lat": 1.0, "lon": 2.0, "distance_m": 10 * i,
        }
        for i in range(3)
    ]


def test_a_small_limit_does_not_truncate_what_the_cache_serves_later(monkeypatch):
    monkeypatch.setattr(geocode, "geocode", lambda *a, **k: [])
    monkeypatch.setattr(overture, "find_places", _three_places)
    geocode.clear_resolve_session()

    first = geocode.resolve_place("Example", near_lat=1.0, near_lon=2.0, limit=1)
    assert len(first) == 1
    # Cut the data source off: anything that comes back now came from the cache.
    monkeypatch.setattr(overture, "find_places", lambda *a, **k: [])
    again = geocode.resolve_place("Example", near_lat=1.0, near_lon=2.0, limit=3)
    assert [r["id"] for r in again] == ["place-0", "place-1", "place-2"]
    assert geocode.resolve_place("Example", near_lat=1.0, near_lon=2.0, limit=2) == again[:2]


# ---------------------------------------------------------------------------
# _match_tier folds case, diacritics and punctuation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, query",
    [
        ("Notre-Dame", "notre dame"),
        ("Straße", "STRASSE"),
        ("São Paulo", "sao paulo"),
        ("Saint-Étienne", "saint etienne"),
    ],
)
def test_match_tier_is_exact_across_case_diacritics_and_punctuation(name, query):
    assert geocode._match_tier(name, query) == 3


def test_match_tier_prefix_and_substring_floor():
    assert geocode._match_tier("Notre-Dame de Paris", "notre dame") == 2
    assert geocode._match_tier("Dame", "notre dame") == 1
    assert geocode._match_tier("New York", "york") == 1
    # Never below 1: the caller already filtered non-matches out.
    assert geocode._match_tier("Anything", "???") == 1


# ---------------------------------------------------------------------------
# explicit country= survives the degrade paths
# ---------------------------------------------------------------------------


def _record_division_queries(monkeypatch, calls):
    def fake(query, region_code, local_table, **kw):
        calls.append((query, region_code, kw.get("country_code")))
        return []

    monkeypatch.setattr(geocode, "_query_divisions", fake)
    monkeypatch.setattr(geocode, "_query_places_fallback", lambda *a, **k: [])
    monkeypatch.setattr(geocode, "_local_alt_names_table", lambda t: None)
    monkeypatch.setattr(geocode, "_region_population_lookup", lambda t: {})


def test_region_degrade_keeps_the_explicit_country(monkeypatch):
    calls = []
    _record_division_queries(monkeypatch, calls)
    monkeypatch.setattr(geocode, "_local_divisions_table", lambda: None)

    geocode.geocode_detailed("Springfield, IL", limit=5, country="US")

    assert calls[0] == ("Springfield", "US-IL", "US")
    # The #46 degrade drops the parsed region, not the caller's filter.
    assert calls[1] == ("Springfield, IL", None, "US")
    # ...and so do the variant/recall searches after it. (The anchor
    # derivation's lookup of the bare word "IL" is not a search.)
    searches = [c for c in calls if c[0].startswith("Springfield")]
    assert len(searches) >= 2 and all(c[2] == "US" for c in searches)


def test_fuzzy_retry_keeps_the_explicit_country(monkeypatch, tmp_path):
    calls = []
    _record_division_queries(monkeypatch, calls)
    table = str(tmp_path / "divisions.parquet")
    monkeypatch.setattr(geocode, "_local_divisions_table", lambda: table)
    fuzzy = []

    def fake_fuzzy(table_path, query, region_code=None, country_code=None, **kw):
        fuzzy.append((query, region_code, country_code))
        return []

    monkeypatch.setattr(geocode, "_query_divisions_fuzzy", fake_fuzzy)

    geocode.geocode_detailed("Springfield, IL", limit=5, country="US")

    assert fuzzy == [("Springfield", "US-IL", "US"), ("Springfield", None, "US")]


def test_fuzzy_retry_is_skipped_when_only_the_explicit_country_could_be_dropped(
    monkeypatch, tmp_path,
):
    calls = []
    _record_division_queries(monkeypatch, calls)
    table = str(tmp_path / "divisions.parquet")
    monkeypatch.setattr(geocode, "_local_divisions_table", lambda: table)
    fuzzy = []
    monkeypatch.setattr(
        geocode, "_query_divisions_fuzzy",
        lambda t, q, r=None, c=None, **kw: fuzzy.append((q, r, c)) or [],
    )

    geocode.geocode_detailed("Springfield", limit=5, country="US")

    assert fuzzy == [("Springfield", None, "US")]
    assert all(c[2] == "US" for c in calls if c[0].startswith("Springfield"))


def test_a_country_suffix_that_agrees_with_country_param_does_not_degrade(monkeypatch):
    calls = []
    _record_division_queries(monkeypatch, calls)
    monkeypatch.setattr(geocode, "_local_divisions_table", lambda: None)

    result = geocode.geocode_detailed("Springfield, GB", limit=5, country="GB")

    assert result["results"] == []
    assert all(c[2] == "GB" for c in calls if c[0].startswith("Springfield"))
    assert "unconstrained" not in (result.get("note") or "")


# ---------------------------------------------------------------------------
# prefix labels need a word boundary
# ---------------------------------------------------------------------------


def test_prefix_label_requires_a_word_boundary():
    assert geocode._place_match_label("Mall of America", "Ma", set()) == "contains"
    assert geocode._place_match_label("Mall of America", "Mall", set()) == "prefix"
    assert geocode._place_match_label("Mall of America", "mall of", set()) == "prefix"
    assert geocode._place_match_label("Mall", "Mall of America", set()) == "prefix"
    assert geocode._place_match_label("Mall of America", "Mall of America", set()) == "exact"


def test_is_word_prefix():
    assert geocode._is_word_prefix("mall", "mall of america")
    assert geocode._is_word_prefix("mall", "mall")
    assert geocode._is_word_prefix("mall", "mall-mart")
    assert not geocode._is_word_prefix("ma", "mall of america")
    assert not geocode._is_word_prefix("", "mall")


# ---------------------------------------------------------------------------
# per-table memos
# ---------------------------------------------------------------------------


def test_region_population_lookup_scans_once_per_table(monkeypatch):
    geocode._region_population_lookup_cached.cache_clear()
    executed = []
    outcomes = iter([duckdb.Error("transient"), [("US-CA", 39_000_000)]])

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append(sql)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome

            class R:
                def fetchall(self_inner):
                    return outcome

            return R()

    monkeypatch.setattr(overture, "conn", lambda: FakeConn())
    assert geocode._region_population_lookup("t.parquet") == {}  # failure: not cached
    assert geocode._region_population_lookup("t.parquet") == {"US-CA": 39_000_000}
    assert geocode._region_population_lookup("t.parquet") == {"US-CA": 39_000_000}
    assert len(executed) == 2
    assert geocode._region_population_lookup(None) == {}
    geocode.clear_resolve_session()
    assert geocode._region_population_lookup_cached.cache_info().currsize == 0


def test_fallback_anchor_details_is_memoized_per_inputs(monkeypatch):
    brooklyn = {
        "id": "div-brooklyn", "name": "Brooklyn", "subtype": "locality", "country": "US",
        "region": "US-NY", "lat": 40.65, "lon": -73.95, "admin_context": ["United States"],
        "population": 2_600_000,
    }
    lookups = []

    def fake_query(candidate, region_code, local_table, **kw):
        lookups.append(candidate)
        return [dict(brooklyn)] if candidate.lower() == "brooklyn" else []

    monkeypatch.setattr(geocode, "_query_divisions", fake_query)
    geocode.clear_resolve_session()

    first = geocode._fallback_anchor_details("Landmark Brooklyn", [], None, None)
    assert first and first[0]["lat"] == 40.65
    n = len(lookups)
    assert n > 0
    second = geocode._fallback_anchor_details("Landmark Brooklyn", [], None, None)
    assert second == first
    assert len(lookups) == n, "the second identical call must not re-run the division scans"
    # A caller's own edits never reach the memo.
    second[0]["lat"] = 0.0
    assert geocode._fallback_anchor_details("Landmark Brooklyn", [], None, None)[0]["lat"] == 40.65
    # Different inputs are a different question.
    geocode._fallback_anchor_details("Landmark Brooklyn", [], "US-NY", None)
    assert len(lookups) > n


# ---------------------------------------------------------------------------
# build races
# ---------------------------------------------------------------------------


def test_unique_tmp_paths_never_collide(tmp_path):
    target = tmp_path / "sub" / "table.parquet"
    paths = {geocode._unique_tmp_path(target) for _ in range(5)}
    assert len(paths) == 5
    assert all(p.parent == target.parent and p.name.startswith("table.parquet.") for p in paths)
    assert all(p.suffix == ".tmp" for p in paths)


def test_alt_table_build_is_attempted_once_across_threads(monkeypatch, tmp_path):
    builds = []
    started = threading.Barrier(6)

    def fake_build(alt_path, glob):
        builds.append(alt_path)

    monkeypatch.setattr(geocode, "_ALT_BUILD_ATTEMPTED", set())
    monkeypatch.setattr(geocode, "_try_materialize_alt_names_table", fake_build)
    monkeypatch.setattr(overture, "upstream_glob", lambda **kw: "divisions-glob")
    table = str(tmp_path / "divisions.parquet")

    def worker():
        started.wait()
        geocode._local_alt_names_table(table)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(builds) == 1
    assert geocode._local_alt_names_table(table) is None  # one attempt per process


def test_lang_table_build_is_attempted_once(monkeypatch, tmp_path):
    builds = []
    monkeypatch.setattr(geocode, "_LANG_BUILD_ATTEMPTED", set())
    monkeypatch.setattr(
        geocode, "_try_materialize_lang_names_table", lambda p, g: builds.append(p)
    )
    monkeypatch.setattr(overture, "upstream_glob", lambda **kw: "divisions-glob")
    table = str(tmp_path / "divisions.parquet")
    assert geocode._local_lang_names_table(table) is None
    assert geocode._local_lang_names_table(table) is None
    assert len(builds) == 1


# ---------------------------------------------------------------------------
# geo.geom_expr: a failed probe is not cached
# ---------------------------------------------------------------------------


def test_geom_expr_reprobes_after_a_failed_probe(monkeypatch):
    geo.clear_geom_expr_cache()
    outcomes = iter([duckdb.Error("httpfs not loaded"), ("geometry", "GEOMETRY")])
    probes = []

    class FakeConn:
        def execute(self, sql):
            probes.append(sql)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome

            class R:
                def fetchone(self_inner):
                    return outcome

            return R()

    monkeypatch.setattr(db, "shared_conn", lambda: FakeConn())
    glob = "s3://bucket/theme=places/*.parquet"
    assert geo.geom_expr(glob) == "ST_GeomFromWKB(geometry)"   # degraded, not cached
    assert geo.geom_expr(glob) == "geometry"                   # re-probed
    assert geo.geom_expr(glob, as_wkt=True) == "ST_AsText(geometry)"  # cached
    assert len(probes) == 2
    geo.clear_geom_expr_cache()
