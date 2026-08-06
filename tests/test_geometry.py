import math

import pytest

from placeroot import overture

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


def test_bbox_around_does_not_wrap_antimeridian():
    """Documents current (unfixed) behavior: no seam handling.

    A search near lon=180 produces an out-of-range box instead of wrapping,
    so it will miss places just across the antimeridian. Not in scope to
    fix here — this test pins down what's true today.
    """
    xmin, ymin, xmax, ymax = overture._bbox_around(10.0, 179.98, 5000)
    assert xmax > 180  # out of valid longitude range: proof it didn't wrap


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
    (got,) = con.execute(sql, params).fetchone()
    assert got == pytest.approx(expected, abs=1.0)
