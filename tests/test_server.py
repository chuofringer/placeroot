import os
import time

from placeroot import server
from placeroot.cache import parse_warm_region

from .conftest import CENTER_LAT, CENTER_LON

# --- Issue #163: coordinate hardening ---------------------------------------


def test_invalid_coord_rejects_out_of_range_lat():
    for bad_lat in (91.0, -91.0):
        err = server._invalid_coord(bad_lat, CENTER_LON)
        assert err == {
            "error": "bad_request",
            "detail": (
                f"lat={bad_lat!r} is out of range; lat must be in [-90, 90] and "
                "lon in [-180, 180] (did you swap lat and lon?)"
            ),
        }


def test_invalid_coord_rejects_out_of_range_lon():
    for bad_lon in (181.0, -181.0):
        assert server._invalid_coord(CENTER_LAT, bad_lon)["error"] == "bad_request"


def test_invalid_coord_rejects_swapped_lat_lon():
    # Manila, swapped: lat=120.98 (invalid) instead of lon=120.98.
    err = server._invalid_coord(120.98, 14.60)
    assert err["error"] == "bad_request"
    assert "swap" in err["detail"]


def test_invalid_coord_rejects_non_finite():
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert server._invalid_coord(bad, CENTER_LON)["error"] == "bad_request"
        assert server._invalid_coord(CENTER_LAT, bad)["error"] == "bad_request"


def test_invalid_coord_accepts_boundary_values():
    assert server._invalid_coord(90.0, 180.0) is None
    assert server._invalid_coord(-90.0, -180.0) is None
    assert server._invalid_coord(CENTER_LAT, CENTER_LON) is None


def test_find_places_rejects_out_of_range_lat():
    for bad_lat in (91.0, -91.0):
        result = server.find_places(lat=bad_lat, lon=CENTER_LON)
        assert result["error"] == "bad_request"


def test_find_places_rejects_out_of_range_lon():
    for bad_lon in (181.0, -181.0):
        result = server.find_places(lat=CENTER_LAT, lon=bad_lon)
        assert result["error"] == "bad_request"


def test_find_places_rejects_swapped_lat_lon():
    result = server.find_places(lat=120.98, lon=14.60)
    assert result["error"] == "bad_request"
    assert "swap" in result["detail"]


def test_find_places_rejects_non_finite_lat():
    result = server.find_places(lat=float("nan"), lon=CENTER_LON)
    assert result["error"] == "bad_request"
    result = server.find_places(lat=float("inf"), lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_find_places_accepts_boundary_coords_without_bad_request():
    # Legitimately empty/off-fixture is fine; must NOT be rejected by the
    # coordinate-range check itself.
    for lat, lon in ((90.0, 0.0), (-90.0, 0.0), (0.0, 180.0), (0.0, -180.0)):
        result = server.find_places(lat=lat, lon=lon, radius_m=1000)
        assert result.get("error") != "bad_request"


def test_isochrone_rejects_out_of_range_coord():
    result = server.isochrone(lat=91.0, lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_summarize_area_rejects_out_of_range_coord():
    result = server.summarize_area(lat=CENTER_LAT, lon=181.0)
    assert result["error"] == "bad_request"


def test_resolve_place_rejects_swapped_near_lat_lon():
    # resolve_place was the one coordinate-taking tool left unguarded: a
    # swapped near_lat/near_lon fed bbox_around an inverted box that matched
    # zero rows, returning "no such place" instead of bad_request.
    result = server.resolve_place(query="Rustan's", near_lat=120.98, near_lon=14.60)
    assert result["error"] == "bad_request"
    assert "swap" in result["detail"]


def test_distance_matrix_rejects_out_of_range_origin():
    result = server.distance_matrix(
        origins=[{"lat": 91.0, "lon": CENTER_LON}],
        destinations=[{"lat": CENTER_LAT, "lon": CENTER_LON}],
    )
    assert result["error"] == "bad_request"
    assert "origins[0]" in result["detail"]


def test_distance_matrix_rejects_out_of_range_destination():
    result = server.distance_matrix(
        origins=[{"lat": CENTER_LAT, "lon": CENTER_LON}],
        destinations=[{"lat": CENTER_LAT, "lon": 200.0}],
    )
    assert result["error"] == "bad_request"
    assert "destinations[0]" in result["detail"]


def test_compare_areas_rejects_out_of_range_area():
    result = server.compare_areas(
        areas=[
            {"lat": CENTER_LAT, "lon": CENTER_LON},
            {"lat": 95.0, "lon": CENTER_LON},
        ]
    )
    assert result["error"] == "bad_request"
    assert "areas[1]" in result["detail"]


def test_reverse_geocode_rejects_out_of_range_coord():
    result = server.reverse_geocode(lat=91.0, lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_reverse_geocode_batch_flags_out_of_range_point_without_failing_batch():
    result = server.reverse_geocode_batch(
        points=[
            {"lat": CENTER_LAT, "lon": CENTER_LON},
            {"lat": 95.0, "lon": CENTER_LON},
        ]
    )
    rows = result["results"]
    assert len(rows) == 2
    assert "error" not in rows[0]
    assert "error" in rows[1]


def test_admin_lookup_rejects_out_of_range_coord():
    result = server.admin_lookup(lat=91.0, lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_within_distance_rejects_out_of_range_coord():
    result = server.within_distance(lat=91.0, lon=CENTER_LON, max_distance_m=1000)
    assert result["error"] == "bad_request"


def test_summarize_buildings_rejects_out_of_range_coord():
    result = server.summarize_buildings(lat=91.0, lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_buildings_at_rejects_out_of_range_coord():
    result = server.buildings_at(lat=91.0, lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_place_details_rejects_out_of_range_coord_when_both_given():
    result = server.place_details(name="anything", lat=91.0, lon=CENTER_LON)
    assert result["error"] == "bad_request"


def test_place_details_ignores_range_check_when_lat_lon_not_both_given():
    # id-only lookup: lat/lon absent entirely, must not spuriously fail the
    # coordinate check (place_details' own "requires id or name+lat+lon"
    # validation still applies downstream, independent of this check).
    result = server.place_details(id="does-not-exist")
    assert result.get("error") != "bad_request"


def test_find_places_tool_wraps_results_and_applies_budget():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    assert "results" in result
    assert len(result["results"]) == 10
    # The 10 returned rows comfortably fit the default token budget, but
    # this dense fixture cluster has more than 10 matches within 1000m
    # (see test_limit_fill) — pagination cursors (ROADMAP §4.4) overfetch
    # one row beyond limit to detect exactly that, so this answer is
    # honestly truncated (more rows exist) and carries a cursor, even
    # though no row was ever dropped for budget reasons.
    assert result["truncated"] is True
    assert "omitted_count" not in result  # nothing was budget-dropped
    assert "cursor" in result


def test_summarize_area_tool():
    result = server.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert "top_categories" in result
    assert result["total_places"] > 0


def test_find_places_tool_truncates_under_tiny_budget(monkeypatch):
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "10")
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert result["truncated"] is True
    assert result["omitted_count"] > 0


def test_warm_start_is_a_noop_without_warm_region(monkeypatch):
    monkeypatch.delenv("PLACEROOT_WARM_REGION", raising=False)
    server._warm_start()  # must not raise


def test_warm_start_logs_and_continues_on_malformed_region(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("PLACEROOT_WARM_REGION", "not-a-region")
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    with caplog.at_level(logging.WARNING, logger="placeroot.server"):
        server._warm_start()  # must not raise
    assert "malformed" in caplog.text


def test_warm_start_skipped_when_cache_disabled(monkeypatch):
    monkeypatch.setenv("PLACEROOT_WARM_REGION", f"{CENTER_LAT},{CENTER_LON},1000")
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    server._warm_start()  # must not raise, and must not attempt to query


def test_parse_warm_region_reexported_for_operators():
    # PLACEROOT_WARM_REGION's format is documented via this parser.
    assert parse_warm_region("40.7,-73.9,1000") == (40.7, -73.9, 1000.0)


def test_warm_start_materializes_via_existing_cache(monkeypatch):
    """Issue #314: _warm_start reuses cache.prewarm_bbox (force_sync),
    not a second cache and not a leaked PLACEROOT_CACHE_SYNC flag.
    """
    monkeypatch.setenv("PLACEROOT_WARM_REGION", f"{CENTER_LAT},{CENTER_LON},1000")
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)

    called = []

    def spy_prewarm(lat, lon, radius_m):
        called.append((lat, lon, radius_m))
        return {"status": "warmed"}

    monkeypatch.setattr(server, "_prewarm_region", spy_prewarm)
    server._warm_start()
    assert called == [(CENTER_LAT, CENTER_LON, 1000.0)]
    assert os.environ.get("PLACEROOT_CACHE_SYNC") is None


def test_warm_start_does_not_mutate_cache_sync_env(monkeypatch):
    monkeypatch.setenv("PLACEROOT_WARM_REGION", f"{CENTER_LAT},{CENTER_LON},1000")
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.setenv("PLACEROOT_CACHE_SYNC", "0")
    monkeypatch.setattr(server, "_prewarm_region", lambda *a, **k: {"status": "warmed"})
    server._warm_start()
    assert os.environ.get("PLACEROOT_CACHE_SYNC") == "0"


def test_warm_metadata_async_returns_without_waiting_for_the_probe(monkeypatch):
    """The pre-warm must be fire-and-forget: main() shouldn't stall startup
    waiting on a network round trip to the active dataset.
    """
    started = []

    def slow_warm_metadata():
        started.append(True)
        time.sleep(0.2)

    monkeypatch.setattr(server.overture, "warm_metadata", slow_warm_metadata)
    t0 = time.monotonic()
    server._warm_metadata_async()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1  # returned immediately, didn't wait for the 0.2s "probe"
    time.sleep(0.3)  # let the background thread finish before the test exits
    assert started == [True]

def test_warm_divisions_async_returns_without_waiting_for_the_build(monkeypatch):
    """Issue #93: the divisions-table warm must be fire-and-forget, same as
    _warm_metadata_async — main() shouldn't stall startup waiting on the
    ~20-30s one-time materialization.
    """
    started = []

    def slow_materialize():
        started.append(True)
        time.sleep(0.2)

    monkeypatch.setattr(server.geocoding, "_local_divisions_table", slow_materialize)
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    t0 = time.monotonic()
    server._warm_divisions_async()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1  # returned immediately, didn't wait for the "build"
    time.sleep(0.3)  # let the background thread finish before the test exits
    assert started == [True]


def test_warm_divisions_async_skipped_when_cache_disabled(monkeypatch):
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    called = []
    monkeypatch.setattr(server.geocoding, "_local_divisions_table", lambda: called.append(True))
    server._warm_divisions_async()
    time.sleep(0.05)  # would-be thread startup window
    assert called == []


def test_warm_divisions_async_is_non_fatal_on_failure(monkeypatch):
    """Issue #93: a materialization failure must never crash startup — this
    mirrors geocode._local_divisions_table()'s own internal
    log-and-swallow, but proves the warm hook itself doesn't propagate a
    background-thread exception into the caller either.
    """

    def boom():
        raise RuntimeError("materialization exploded")

    monkeypatch.setattr(server.geocoding, "_local_divisions_table", boom)
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    server._warm_divisions_async()  # must not raise
    time.sleep(0.1)  # let the background thread run (and fail) before exit


def test_render_map_tool_writes_artifact_from_find_places_output(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_ARTIFACT_DIR", str(tmp_path))
    found = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=5)
    result = server.render_map(found, title="Nearby")
    assert set(result) == {"path", "bytes", "features_rendered", "skipped_features"}
    assert result["features_rendered"] == len(found["results"])
    assert result["skipped_features"] == 0
    from pathlib import Path

    assert Path(result["path"]).exists()


def test_render_map_tool_handles_summarize_area_output(tmp_path, monkeypatch):
    monkeypatch.setenv("PLACEROOT_ARTIFACT_DIR", str(tmp_path))
    summary = server.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    result = server.render_map(summary, title="Area")
    assert result["features_rendered"] == 1  # one marker at the area center



def test_release_attribution_reaches_instructions():
    """Regression: MCPServer.instructions is a read-only property; main()
    must write through the low-level server or it crashes at startup."""
    from placeroot import server

    original = server.mcp._lowlevel_server.instructions
    try:
        server.mcp._lowlevel_server.instructions = "probe"
        assert server.mcp.instructions == "probe"
    finally:
        server.mcp._lowlevel_server.instructions = original
