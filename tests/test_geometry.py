import math

import pytest

from placeroot import db, overture

from ._geo import haversine_m


def test_bbox_around_contains_circle():
    lat, lon, radius_m = 40.7, -73.9, 500
    xmin, ymin, xmax, ymax = overture._bbox_around(lat, lon, radius_m)
    assert xmin < lon < xmax
    assert ymin < lat < ymax
    # box must reach at least as far as the circle in every direction
    assert (lat - ymin) * 111_320.0 >= radius_m - 1
    assert (ymax - lat) * 111_320.0 >= radius_m - 1


def test_bbox_around_high_latitude_stays_finite():
    # near the pole, cos(lat) -> 0; the clamp in _bbox_around must prevent
    # dlon from blowing up to inf/nan.
    xmin, ymin, xmax, ymax = overture._bbox_around(89.9, 15.0, 500)
    assert all(math.isfinite(v) for v in (xmin, ymin, xmax, ymax))


def test_bbox_around_clamps_latitude_to_poles():
    """Issue #42: a radius circle near a pole must not produce ymin/ymax
    outside [-90, 90] — that's fed straight into a bbox filter downstream."""
    xmin, ymin, xmax, ymax = overture._bbox_around(89.9, 15.0, 50_000)
    assert ymax == 90.0
    xmin, ymin, xmax, ymax = overture._bbox_around(-89.9, 15.0, 50_000)
    assert ymin == -90.0


def test_bbox_around_clamps_degenerate_span_at_pole():
    """Issue #163 (A1): at lat=90 (a VALID coordinate) cos(lat) floors to
    1e-6, so dlon would otherwise blow up to ~4.49M degrees even at a
    modest radius. bbox_around must clamp the half-width so the box never
    exceeds full-globe coverage (a wider box is meaningless anyway)."""
    xmin, ymin, xmax, ymax = overture._bbox_around(90.0, 0.0, 500_000)
    assert xmax - xmin <= 360.0
    assert ymax - ymin <= 180.0
    assert all(math.isfinite(v) for v in (xmin, ymin, xmax, ymax))


def test_bbox_around_leaves_longitude_unwrapped():
    """_bbox_around itself still doesn't wrap/clamp longitude at the seam —
    that's intentional (see its docstring): area_geometry() and
    cache.tiles_for_bbox() are the ones responsible for handling a box that
    crosses +/-180, using this raw, possibly out-of-range box as input."""
    xmin, ymin, xmax, ymax = overture._bbox_around(10.0, 179.98, 5000)
    assert xmax > 180


def test_bbox_filter_sql_unchanged_for_non_crossing_box():
    """Issue #42: the common case (no antimeridian involved) must produce
    byte-identical SQL to before the fix — no perf regression."""
    filter_sql, params = overture._bbox_filter_sql(-74.0, 40.0, -73.0, 41.0)
    assert filter_sql == (
        "bbox.xmax >= $xmin AND bbox.xmin <= $xmax"
        " AND bbox.ymax >= $ymin AND bbox.ymin <= $ymax"
    )
    assert params == {"xmin": -74.0, "ymin": 40.0, "xmax": -73.0, "ymax": 41.0}


def test_bbox_filter_sql_wraps_at_east_seam():
    """A box that overshoots +180 (e.g. centered near lon=179.99) splits
    into an OR of an east-of-seam box and a west-of-seam box."""
    filter_sql, params = overture._bbox_filter_sql(179.9, 9.0, 180.4, 11.0)
    assert "OR" in filter_sql
    assert params["xmin"] == pytest.approx(179.9)  # east box: unchanged, already in range
    assert params["xmax"] == pytest.approx(180.4 - 360.0)  # west box: wrapped in range
    assert params["ymin"] == 9.0
    assert params["ymax"] == 11.0


def test_bbox_filter_sql_wraps_at_west_seam():
    """A box that undershoots -180 (e.g. centered near lon=-179.99) also
    splits into an OR of the two seam-adjacent boxes."""
    filter_sql, params = overture._bbox_filter_sql(-180.4, 9.0, -179.9, 11.0)
    assert "OR" in filter_sql
    assert params["xmin"] == pytest.approx(-180.4 + 360.0)  # east box: wrapped in range
    assert params["xmax"] == pytest.approx(-179.9)  # west box: unchanged, already in range


def test_area_geometry_returns_raw_bbox_for_tile_lookups():
    """The 4th return value is the raw (possibly out-of-range) box from
    _bbox_around, not derived from the SQL filter's own params — those
    diverge once the box crosses the seam (see _bbox_filter_sql)."""
    _, _, _, bbox, _radius_m = overture.area_geometry(10.0, 179.99, 5000)
    xmin, ymin, xmax, ymax = bbox
    assert xmax > 180  # unwrapped, as _bbox_around produced it


def test_find_places_returns_both_sides_of_antimeridian():
    """Issue #42: a search centered on the seam must find places on both
    sides — the bug was the bbox prefilter silently dropping the far side
    even though the (already seam-safe) distance filter would have kept it."""
    results = overture.find_places(10.0, 179.99, radius_m=5000, limit=25)
    names = {r["name"] for r in results}
    assert "Dateline West" in names
    assert "Dateline West Cafe" in names
    assert "Dateline East" in names
    assert "Dateline East Bank" in names
    for r in results:
        assert r["distance_m"] <= 5000


def test_summarize_area_counts_both_sides_of_antimeridian():
    result = overture.summarize_area(10.0, 179.99, radius_m=5000)
    assert result["total_places"] == 4


def test_distance_expr_matches_independent_haversine():
    # area_geometry's SQL distance expression, evaluated through DuckDB,
    # must agree with a from-scratch Python haversine implementation.
    lat, lon = 40.7, -73.9
    other_lat, other_lon = 40.705, -73.895
    expected = haversine_m(lat, lon, other_lat, other_lon)

    con = overture._conn()
    sql = f"""
        SELECT {overture._DISTANCE_EXPR}
        FROM (
            SELECT {{'xmin': $other_lon, 'ymin': $other_lat}}
                ::STRUCT(xmin DOUBLE, ymin DOUBLE) AS bbox
        )
    """
    params = {"lat": lat, "lon": lon, "other_lat": other_lat, "other_lon": other_lon}
    # shared_conn()'s contract: hold conn_lock around every query on it. A
    # daemon autowarm thread left running by an earlier test file queries
    # the same connection, and two threads on one DuckDB connection can
    # hand each other's result sets back — fetchone() returned None here
    # once in CI (#448's PR, test (3.13) matrix job).
    with db.conn_lock:
        (got,) = con.execute(sql, params).fetchone()
    assert got == pytest.approx(expected, abs=1.0)
