"""Security-hardening regression tests.

These cover fixes for resource-exhaustion / injection surfaces reachable
straight from tool arguments:

  - an oversized query bbox (from an abusive radius_m) must not fan out into
    a tile-per-thread cache-materialization storm (cache.MAX_TILES_PER_QUERY)
  - the query layer clamps radius_m to geo.MAX_QUERY_RADIUS_M so a single
    call can't request a planet-spanning bbox / upstream scan
  - simplify_geometry rejects an adversarially large geometry rather than
    tying the server up in O(n^2) RDP + repeated serialization
  - S3 region/endpoint/credential values are escaped before being
    interpolated into DuckDB SET statements
"""

import duckdb
import pytest

from placeroot import buildings, cache, db, geo, overture, server, simplify

from .conftest import FIXTURE_PATH

RELEASE = "2026-07-22.0"
THEME = "places"


# --- tile-cache fan-out guard ------------------------------------------------


@pytest.fixture
def cache_on(tmp_path, monkeypatch):
    """Re-enable the tile cache (the autouse offline_data fixture turns it
    off) with an isolated cache dir."""
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    monkeypatch.delenv("PLACEROOT_CACHE_SYNC", raising=False)
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "cache"))


def test_oversized_query_bbox_skips_tile_cache_without_fanout(cache_on, monkeypatch):
    """A bbox spanning far more than MAX_TILES_PER_QUERY tiles must fall back
    to a direct upstream scan (return None) without scheduling a single tile
    fetch — otherwise it would spawn one thread + COPY per tile."""
    # Pin the fingerprint so this isolates the tile-count cap, independent of
    # the upstream schema probe.
    monkeypatch.setattr(cache, "resolve_fingerprint", lambda *a, **k: "testfp")
    spawned = []
    monkeypatch.setattr(
        cache, "_materialize_in_background", lambda *a, **k: spawned.append(a)
    )
    # ~200 x 100 degrees -> ~20000 one-degree tiles, well over the cap.
    big_bbox = (-100.0, -50.0, 100.0, 50.0)
    assert len(cache.tiles_for_bbox(*big_bbox)) > cache.MAX_TILES_PER_QUERY

    result = cache.local_paths_for_query(
        duckdb.connect(), RELEASE, THEME, big_bbox, str(FIXTURE_PATH), duckdb.connect
    )
    assert result is None
    assert spawned == []  # cap short-circuited before any tile fan-out


def test_normal_query_bbox_still_uses_the_cache(cache_on, monkeypatch):
    """A small bbox (a handful of tiles) is unaffected by the cap — it still
    materializes and serves tiles as before."""
    monkeypatch.setattr(cache, "resolve_fingerprint", lambda *a, **k: "testfp")
    monkeypatch.setenv("PLACEROOT_CACHE_SYNC", "1")  # materialize inline, deterministically
    small_bbox = (-74.0, 40.0, -73.0, 41.0)
    assert len(cache.tiles_for_bbox(*small_bbox)) <= cache.MAX_TILES_PER_QUERY

    con = duckdb.connect()  # plain connection: local fixture COPY needs no httpfs
    paths = cache.local_paths_for_query(
        con, RELEASE, THEME, small_bbox, str(FIXTURE_PATH), lambda: con
    )
    assert paths and all("tile_" in p for p in paths)


# --- radius clamp ------------------------------------------------------------


def test_clamp_radius_bounds_and_handles_non_finite():
    assert geo.clamp_radius_m(1_000) == 1_000
    assert geo.clamp_radius_m(1e12) == geo.MAX_QUERY_RADIUS_M
    assert geo.clamp_radius_m(-5) == 0.0
    assert geo.clamp_radius_m(float("inf")) == 0.0
    assert geo.clamp_radius_m(float("nan")) == 0.0


def test_area_geometry_clamps_oversized_radius():
    _bbox_f, _dist_f, params, bbox = overture.area_geometry(0.0, 0.0, 1e9)
    # The distance parameter and the bbox both reflect the clamped radius.
    assert params["radius_m"] == geo.MAX_QUERY_RADIUS_M
    xmin, ymin, xmax, ymax = bbox
    assert (xmax - xmin) < 360  # not a world-spanning box


def test_buildings_bbox_filter_clamps_oversized_radius():
    _filter, params = buildings._bbox_filter(0.0, 0.0, 1e9)
    assert params["radius_m"] == geo.MAX_QUERY_RADIUS_M
    assert (params["xmax"] - params["xmin"]) < 360


# --- simplify_geometry input cap ---------------------------------------------


def test_oversized_geometry_is_rejected():
    huge = [[i * 1e-6, 0.0] for i in range(simplify.MAX_INPUT_POINTS + 1)]
    with pytest.raises(simplify.InvalidGeometry):
        simplify.simplify_geometry({"type": "LineString", "coordinates": huge}, max_tokens=500)


def test_server_tool_structured_error_on_oversized_geometry():
    huge = [[i * 1e-6, 0.0] for i in range(simplify.MAX_INPUT_POINTS + 1)]
    result = server.simplify_geometry({"type": "LineString", "coordinates": huge}, max_tokens=500)
    assert result["error"] == "invalid_geometry"
    assert "detail" in result


def test_geometry_at_the_cap_is_still_accepted():
    at_cap = [[i * 1e-6, 0.0] for i in range(simplify.MAX_INPUT_POINTS)]
    out = simplify.simplify_geometry(
        {"type": "LineString", "coordinates": at_cap}, max_tokens=500
    )
    assert out["original_points"] == simplify.MAX_INPUT_POINTS
    assert out["kept_points"] <= out["original_points"]


# --- SQL string escaping -----------------------------------------------------


def test_sql_str_escapes_single_quotes():
    assert db._sql_str("us-west-2") == "'us-west-2'"
    assert db._sql_str("a'b") == "'a''b'"
    assert db._sql_str("'; DROP TABLE t; --") == "'''; DROP TABLE t; --'"


def test_sql_str_roundtrips_through_duckdb_parser():
    """The escaped literal must parse back to the exact original value — a
    value carrying a quote can neither break the statement nor inject SQL."""
    con = duckdb.connect()  # core parser only, no httpfs needed
    for value in ["us-west-2", "a'b", "'; DROP TABLE t; --", "trailing'"]:
        (out,) = con.execute(f"SELECT {db._sql_str(value)}").fetchone()
        assert out == value


# --- Second-pass hardening: DoS bounds and defense-in-depth casts ---


def _staircase(n):
    # Near-worst-case for Douglas-Peucker: resists simplification, so each
    # split drops ~one point — the O(n^2) shape that motivated the op budget.
    coords = [[i * 1e-4, (i % 2) * 1e-5 + i * 1e-9] for i in range(n)]
    return {"type": "LineString", "coordinates": coords}


def test_simplify_rejects_oversized_geometry():
    with pytest.raises(simplify.InvalidGeometry):
        simplify.simplify_geometry(_staircase(simplify.MAX_INPUT_POINTS + 1))


def test_simplify_op_budget_bounds_adversarial_input():
    import time

    t0 = time.monotonic()
    result = simplify.simplify_geometry(_staircase(simplify.MAX_INPUT_POINTS), max_tokens=500)
    elapsed = time.monotonic() - t0
    # Without the op budget this staircase runs O(n^2) x ~80 fit iterations
    # (~88s at 100k pre-fix). The budget must keep it to a few seconds even
    # on a slow CI box; 20s is a generous ceiling that still catches a
    # regression that removed the bound.
    assert elapsed < 20.0
    assert result["geometry"]["type"] == "LineString"
    assert result["kept_points"] >= 2  # endpoints always survive


def test_simplify_budget_does_not_harm_easily_simplified_geometry():
    # A smooth shape simplifies with few ops, never touching the budget —
    # confirm the bound doesn't over-simplify legitimate input.
    import math

    circle = {
        "type": "LineString",
        "coordinates": [
            [math.cos(t / 200 * 2 * math.pi) * 0.01, math.sin(t / 200 * 2 * math.pi) * 0.01]
            for t in range(200)
        ],
    }
    result = simplify.simplify_geometry(circle, max_tokens=2000)
    assert result["kept_points"] > 8  # not collapsed to endpoints


def test_resolve_place_token_fanout_is_capped():
    from placeroot import geocode

    huge = " ".join(f"word{i}xyz" for i in range(5000))
    tokens = geocode._significant_tokens(huge)
    assert len(tokens) <= geocode._MAX_RESOLVE_TOKENS


def test_query_limit_is_cast_and_clamped(cache_on, monkeypatch):
    overture.set_data_path(str(FIXTURE_PATH))
    try:
        # A float limit (a non-MCP caller could pass one) must not reach the
        # SQL LIMIT clause as-is; int()+clamp handles it without error.
        rows = overture.find_places(40.7, -73.9, radius_m=2000, limit=3.9)
        assert len(rows) <= overture.MAX_ROWS
        rows = overture.find_places(40.7, -73.9, radius_m=2000, limit=10_000)
        assert len(rows) <= overture.MAX_ROWS
    finally:
        overture.set_data_path(None)


def test_release_env_override_rejects_non_release_shaped_value(monkeypatch):
    from placeroot import release

    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", "../../etc/passwd")
    monkeypatch.setattr(release, "_discover", lambda: None)
    release.reset_cache()
    try:
        # Junk is ignored; falls through to discovery (None here) then the pin.
        assert release.resolve_release() == release.PINNED_RELEASE
    finally:
        release.reset_cache()
