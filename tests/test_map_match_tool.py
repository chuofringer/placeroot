"""Server-level tests for map_match (#442): the MCP surface over
map_match.match_trace (#440, #441).

Same offline fixture as test_map_match_snap.py/test_map_match_stitch.py — a
20x20 street grid, 100m spacing, named streets ("Grid Ave {j}" for row j);
see scripts/build_routing_fixture.py and tests/_routing_fixture.py. The
autouse offline_data fixture in tests/conftest.py points routing.py at it
and clears the graph cache before every test. No network, no live Overture
scan.
"""

from placeroot import budget, geometry_ops, map_match, routing, server

from ._routing_fixture import build_routing_fixture as fx


def _row_trace(row_j: int, columns: list[int], offset_m: float = 5.0) -> list[dict]:
    """Points a few meters off the (i, row_j)-(i+1, row_j) grid edges, along
    row_j — same construction test_map_match_stitch.py's _row_trace uses."""
    points = []
    for i in columns:
        lat, lon = fx.node_latlon(i, row_j)
        lat, lon = fx._offset(lat, lon, 20.0, 90)
        lat, lon = fx._offset(lat, lon, offset_m, 0)
        points.append({"lat": lat, "lon": lon})
    return points


def test_happy_path_matches_the_named_fixture_street():
    points = _row_trace(2, [2, 3, 4, 5])

    result = server.map_match(points, mode="walk")

    assert "error" not in result
    assert "Grid Ave 2" in result["roads"]
    assert result["matched_length_m"] > 0
    assert result["unmatched_points"] == []
    geometry = result["geometry"]
    assert geometry["type"] == "LineString"
    assert len(geometry["coordinates"]) >= 2
    for lon, lat in geometry["coordinates"]:
        assert -180.0 <= lon <= 180.0
        assert -90.0 <= lat <= 90.0


def test_over_cap_points_is_bad_request():
    lat, lon = fx.node_latlon(2, 2)
    points = [{"lat": lat, "lon": lon}] * (map_match.MAX_TRACE_POINTS + 1)

    result = server.map_match(points, mode="walk")

    assert result["error"] == "bad_request"


def test_invalid_coordinate_names_its_index():
    lat, lon = fx.node_latlon(2, 2)
    points = [
        {"lat": lat, "lon": lon},
        {"lat": 95.0, "lon": lon},
    ]

    result = server.map_match(points, mode="walk")

    assert result["error"] == "bad_request"
    assert "points[1]" in result["detail"]


def test_unsupported_mode_is_the_standard_mode_error():
    lat, lon = fx.node_latlon(2, 2)
    result = server.map_match([{"lat": lat, "lon": lon}], mode="teleport")

    assert result["error"] == "unsupported_mode"
    assert "supported" in result


def test_all_unmatched_trace_is_a_real_answer_not_an_error():
    far_points = [
        fx._offset(fx.ORIGIN_LAT, fx.ORIGIN_LON, 700.0 + step * 50.0, 225) for step in range(3)
    ]
    points = [{"lat": lat, "lon": lon} for lat, lon in far_points]

    result = server.map_match(points, mode="walk")

    assert "error" not in result
    assert result["matched_length_m"] == 0.0
    assert result["roads"] == []
    assert result["geometry"] == {"type": "LineString", "coordinates": []}
    assert result["unmatched_points"] == [0, 1, 2]
    assert "note" in result


def test_geometry_respects_the_token_budget():
    # A long trace along many consecutive grid edges, so the raw stitched
    # polyline has enough vertices to need simplifying.
    points = _row_trace(2, list(range(0, 18)))

    result = server.map_match(points, mode="walk")

    assert "error" not in result
    tokens = budget.estimate_tokens({"geometry": result["geometry"]})
    assert tokens <= geometry_ops.GEOMETRY_MAX_TOKENS


def test_radius_too_large_is_a_structured_answer(monkeypatch):
    """#443's review finding at the public layer: a trace spanning past the
    mode's extraction cap must come back as the same structured
    radius_too_large its sibling routing tools return, never a raised
    routing.RadiusTooLarge."""
    monkeypatch.setattr(
        server.mapmatch,
        "match_trace",
        lambda points, mode: (_ for _ in ()).throw(routing.RadiusTooLarge(5300.0, 5000.0)),
    )

    result = server.map_match([{"lat": 0.0, "lon": 0.0}], mode="walk")

    assert result["error"] == "radius_too_large"
    assert result["max_radius_m"] == 5000.0
    assert "detail" in result


def test_single_matched_point_yields_valid_empty_linestring():
    """One matched point can't make an RFC 7946 LineString (>=2 positions):
    the answer normalizes to empty coordinates plus its own note, instead of
    the invalid one-position LineString it used to emit."""
    near, far_a, far_b = (
        _row_trace(2, [3])[0],
        *(
            {"lat": lat, "lon": lon}
            for lat, lon in (
                fx._offset(fx.ORIGIN_LAT, fx.ORIGIN_LON, 700.0, 225),
                fx._offset(fx.ORIGIN_LAT, fx.ORIGIN_LON, 800.0, 225),
            )
        ),
    )

    result = server.map_match([far_a, near, far_b], mode="walk")

    assert "error" not in result
    assert result["matched_length_m"] == 0.0
    assert result["roads"] == []
    assert result["geometry"] == {"type": "LineString", "coordinates": []}
    assert result["unmatched_points"] == [0, 2]
    assert "note" in result
    assert "one point" in result["note"].lower()
