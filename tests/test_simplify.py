import math
import random

import pytest

from placeroot import budget, server, simplify


def _noisy_circle(n=1000, cx=-73.9, cy=40.7, r=0.01, noise=0.0005, seed=1):
    rng = random.Random(seed)
    coords = []
    for i in range(n):
        theta = 2 * math.pi * i / n
        radius = r + noise * rng.uniform(-1, 1)
        coords.append([cx + radius * math.cos(theta), cy + radius * math.sin(theta)])
    coords.append(coords[0])  # close the ring
    return coords


def test_noisy_circle_polygon_fits_budget_with_bounded_deviation():
    ring = _noisy_circle()
    geom = {"type": "Polygon", "coordinates": [ring]}
    out = simplify.simplify_geometry(geom, max_tokens=300)
    assert out["original_points"] == len(ring)
    assert out["kept_points"] < len(ring)
    assert budget.estimate_tokens({"geometry": out["geometry"]}) <= 300
    # Circle radius ~0.01 deg ~= 1100m; a bounded simplification shouldn't blow
    # past the shape's own scale.
    assert out["max_deviation_m"] < 1500
    # A valid ring: still closed.
    coords = out["geometry"]["coordinates"][0]
    assert coords[0] == coords[-1]


def test_linestring_simplifies_and_reports_fewer_points():
    line = [[i * 0.0001, math.sin(i * 0.05) * 0.001] for i in range(500)]
    out = simplify.simplify_geometry({"type": "LineString", "coordinates": line}, max_tokens=200)
    assert out["kept_points"] < out["original_points"]
    assert budget.estimate_tokens({"geometry": out["geometry"]}) <= 200


def test_already_small_geometry_is_returned_unsimplified_when_it_fits():
    line = [[0, 0], [1, 1], [2, 0]]
    out = simplify.simplify_geometry({"type": "LineString", "coordinates": line}, max_tokens=2000)
    assert out["original_points"] == 3
    assert out["kept_points"] == 3
    assert out["max_deviation_m"] == 0.0


def test_point_passes_through_unchanged():
    geom = {"type": "Point", "coordinates": [1.0, 2.0]}
    out = simplify.simplify_geometry(geom, max_tokens=10)
    assert out["geometry"] == geom
    assert out["original_points"] == 1
    assert out["kept_points"] == 1
    assert out["max_deviation_m"] == 0.0


def test_multipoint_passes_through_unchanged():
    geom = {"type": "MultiPoint", "coordinates": [[0, 0], [1, 1], [2, 2]]}
    out = simplify.simplify_geometry(geom, max_tokens=10)
    assert out["kept_points"] == 3


def test_two_point_line_cannot_simplify_further():
    out = simplify.simplify_geometry(
        {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, max_tokens=5
    )
    assert out["kept_points"] == 2
    assert out["original_points"] == 2


@pytest.mark.parametrize(
    "geojson",
    [
        None,
        {},
        {"type": "Polygon"},  # missing coordinates
        {"type": "NotAType", "coordinates": [[0, 0]]},
        {"type": "Polygon", "coordinates": "not-a-list"},
        {"type": "Polygon", "coordinates": []},
        {"type": "LineString", "coordinates": [["a", "b"]]},
    ],
)
def test_malformed_geometry_raises_invalid_geometry(geojson):
    with pytest.raises(simplify.InvalidGeometry):
        simplify.simplify_geometry(geojson, max_tokens=500)


def test_unsupported_type_message_lists_supported_types():
    with pytest.raises(simplify.InvalidGeometry) as excinfo:
        simplify.simplify_geometry(
            {"type": "NotAType", "coordinates": [[0, 0]]}, max_tokens=500
        )
    detail = excinfo.value.detail
    assert repr("NotAType") in detail
    for gtype in simplify.SUPPORTED_TYPES:
        assert gtype in detail


def test_multipolygon_simplifies_each_ring():
    ring_a = _noisy_circle(n=300, cx=-73.9, cy=40.7)
    ring_b = _noisy_circle(n=300, cx=-73.8, cy=40.8, seed=2)
    geom = {"type": "MultiPolygon", "coordinates": [[ring_a], [ring_b]]}
    out = simplify.simplify_geometry(geom, max_tokens=400)
    assert out["kept_points"] < out["original_points"]
    assert budget.estimate_tokens({"geometry": out["geometry"]}) <= 400


def test_token_target_honored_across_a_range_of_budgets():
    ring = _noisy_circle()
    geom = {"type": "Polygon", "coordinates": [ring]}
    for max_tokens in (50, 100, 250, 500, 1000):
        out = simplify.simplify_geometry(geom, max_tokens=max_tokens)
        assert budget.estimate_tokens({"geometry": out["geometry"]}) <= max_tokens


def _assert_valid_ring(coords):
    """A valid GeoJSON polygon ring: >=4 positions, >=3 distinct vertices, closed."""
    assert len(coords) >= 4
    assert coords[0] == coords[-1]
    distinct = {tuple(c) for c in coords}
    assert len(distinct) >= 3


def test_simplify_geometry_never_collapses_a_ring_to_two_points():
    # issue #135 repro: every interior point lies within epsilon of the
    # start->end chord, so unpadded RDP would keep only the two (identical)
    # endpoints -- an invalid ring.
    ring = [[0.0, 0.0], [0.0001, 0.00001], [0.0001, 0.0001], [0.00001, 0.0001], [0.0, 0.0]]
    out = simplify.simplify_geometry({"type": "Polygon", "coordinates": [ring]}, max_tokens=20)
    coords = out["geometry"]["coordinates"][0]
    assert coords != [[0.0, 0.0], [0.0, 0.0]]
    _assert_valid_ring(coords)


def test_noisy_circle_polygon_stays_valid_ring_at_tiny_budget():
    ring = _noisy_circle(n=500)
    geom = {"type": "Polygon", "coordinates": [ring]}
    out = simplify.simplify_geometry(geom, max_tokens=15)
    coords = out["geometry"]["coordinates"][0]
    _assert_valid_ring(coords)
    # Bounded deviation relative to the circle's own scale (~1100m radius).
    assert out["max_deviation_m"] < 2000


def test_multipolygon_rings_stay_valid_at_tiny_budget():
    ring_a = [[0.0, 0.0], [0.0001, 0.00001], [0.0001, 0.0001], [0.00001, 0.0001], [0.0, 0.0]]
    ring_b = _noisy_circle(n=200, cx=-73.8, cy=40.8, seed=3)
    geom = {"type": "MultiPolygon", "coordinates": [[ring_a], [ring_b]]}
    out = simplify.simplify_geometry(geom, max_tokens=20)
    for poly in out["geometry"]["coordinates"]:
        for ring in poly:
            _assert_valid_ring(ring)


def test_closed_linestring_is_not_padded_like_a_ring():
    # A LineString that happens to be closed (coords[0] == coords[-1]) is
    # still a LineString, not a polygon ring -- it's valid GeoJSON with as
    # few as 2 points, so it must not be force-padded to 4.
    n = 200
    line = [[i * 0.0001, 0.0] for i in range(n)]
    line.append([0.0, 0.0])  # closes it, collinear with everything else
    out = simplify.simplify_geometry({"type": "LineString", "coordinates": line}, max_tokens=10)
    assert out["geometry"]["coordinates"][0] == out["geometry"]["coordinates"][-1]
    assert out["kept_points"] < 4


def test_polygon_simplification_unchanged_with_generous_budget():
    ring = _noisy_circle(n=500)
    geom = {"type": "Polygon", "coordinates": [ring]}
    out = simplify.simplify_geometry(geom, max_tokens=100_000)
    assert out["kept_points"] == out["original_points"]
    assert out["geometry"]["coordinates"][0] == ring


# --- server tool ---


def test_server_tool_simplifies_and_reports_fields():
    ring = _noisy_circle()
    geom = {"type": "Polygon", "coordinates": [ring]}
    result = server.simplify_geometry(geom, max_tokens=300)
    assert "error" not in result
    assert set(result) == {"geometry", "max_deviation_m", "original_points", "kept_points"}


def test_server_tool_structured_error_on_malformed_input():
    result = server.simplify_geometry({"type": "Polygon"}, max_tokens=300)
    assert result["error"] == "invalid_geometry"
    assert "detail" in result
