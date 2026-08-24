import math

from placeroot import geometry_ops, server

P = lambda lat, lon: {"lat": lat, "lon": lon}  # noqa: E731


# ---------------------------------------------------------------------------
# Point math
# ---------------------------------------------------------------------------


def test_distance_equator_one_degree_lon():
    out = server.geometry_op("distance", point=P(0, 0), point2=P(0, 1))
    # ~111.32 km per degree of longitude at the equator.
    assert math.isclose(out["distance_m"], 111_320.0, rel_tol=0.01)


def test_distance_matches_distance_matrix_semantics():
    a, b = P(40.0, -73.0), P(41.0, -74.0)
    d = server.geometry_op("distance", point=a, point2=b)["distance_m"]
    dm = server.distance_matrix([a], [b])["elements"][0]["distance_m"]
    # distance_matrix rounds to the nearest meter; geometry_op does not.
    assert math.isclose(d, dm, abs_tol=1.0)


def test_bearing_cardinal_directions():
    origin = P(0, 0)
    north = server.geometry_op("bearing", point=origin, point2=P(1, 0))["bearing_deg"]
    east = server.geometry_op("bearing", point=origin, point2=P(0, 1))["bearing_deg"]
    south = server.geometry_op("bearing", point=origin, point2=P(-1, 0))["bearing_deg"]
    west = server.geometry_op("bearing", point=origin, point2=P(0, -1))["bearing_deg"]
    assert math.isclose(north, 0.0, abs_tol=0.5)
    assert math.isclose(east, 90.0, abs_tol=0.5)
    assert math.isclose(south, 180.0, abs_tol=0.5)
    assert math.isclose(west, 270.0, abs_tol=0.5)


def test_destination_round_trips_with_distance_and_bearing():
    origin = P(37.7749, -122.4194)
    dest = server.geometry_op("destination", point=origin, bearing_deg=45.0, distance_m=5000.0)[
        "point"
    ]
    back_distance = server.geometry_op("distance", point=origin, point2=dest)["distance_m"]
    back_bearing = server.geometry_op("bearing", point=origin, point2=dest)["bearing_deg"]
    assert math.isclose(back_distance, 5000.0, rel_tol=1e-6)
    assert math.isclose(back_bearing, 45.0, abs_tol=1e-6)


def test_destination_north_increases_latitude_only():
    origin = P(10.0, 20.0)
    dest = server.geometry_op("destination", point=origin, bearing_deg=0.0, distance_m=1000.0)[
        "point"
    ]
    assert dest["lat"] > origin["lat"]
    assert math.isclose(dest["lon"], origin["lon"], abs_tol=1e-9)


def test_midpoint_is_symmetric_and_equidistant():
    a, b = P(10.0, 20.0), P(30.0, -40.0)
    m1 = server.geometry_op("midpoint", point=a, point2=b)["point"]
    m2 = server.geometry_op("midpoint", point=b, point2=a)["point"]
    assert math.isclose(m1["lat"], m2["lat"], abs_tol=1e-9)
    assert math.isclose(m1["lon"], m2["lon"], abs_tol=1e-9)
    d_am = server.geometry_op("distance", point=a, point2=m1)["distance_m"]
    d_mb = server.geometry_op("distance", point=m1, point2=b)["distance_m"]
    assert math.isclose(d_am, d_mb, rel_tol=1e-3)


def test_midpoint_of_a_point_with_itself_is_itself():
    a = P(51.5, -0.1)
    m = server.geometry_op("midpoint", point=a, point2=a)["point"]
    assert math.isclose(m["lat"], a["lat"], abs_tol=1e-9)
    assert math.isclose(m["lon"], a["lon"], abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------


def _square_km_ring(cx, cy, half_km=0.5):
    """~1km x 1km square ring around (cx, cy) degrees, using local meters/degree."""
    mpd_lat = 111_320.0
    mpd_lon = 111_320.0 * math.cos(math.radians(cy))
    dlat = half_km * 1000.0 / mpd_lat
    dlon = half_km * 1000.0 / mpd_lon
    return [
        [cx - dlon, cy - dlat],
        [cx + dlon, cy - dlat],
        [cx + dlon, cy + dlat],
        [cx - dlon, cy + dlat],
        [cx - dlon, cy - dlat],
    ]


def test_area_of_one_km_square_is_close_to_one_million_m2():
    ring = _square_km_ring(-73.9, 40.7)
    geom = {"type": "Polygon", "coordinates": [ring]}
    out = server.geometry_op("area", geometry=geom)
    assert math.isclose(out["area_m2"], 1_000_000.0, rel_tol=0.02)
    assert math.isclose(out["area_km2"], 1.0, rel_tol=0.02)


def test_area_with_hole_is_smaller_than_without():
    outer = _square_km_ring(0, 0, half_km=1.0)
    hole = _square_km_ring(0, 0, half_km=0.25)
    with_hole = server.geometry_op(
        "area", geometry={"type": "Polygon", "coordinates": [outer, hole]}
    )["area_m2"]
    without_hole = server.geometry_op("area", geometry={"type": "Polygon", "coordinates": [outer]})[
        "area_m2"
    ]
    assert with_hole < without_hole


def test_length_of_known_line():
    line = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
    out = server.geometry_op("length", geometry={"type": "LineString", "coordinates": line})
    leg1 = server.geometry_op("distance", point=P(0, 0), point2=P(0, 1))["distance_m"]
    leg2 = server.geometry_op("distance", point=P(0, 1), point2=P(1, 1))["distance_m"]
    assert math.isclose(out["length_m"], leg1 + leg2, rel_tol=1e-9)


def test_bbox_of_polygon():
    ring = [[-10.0, -5.0], [10.0, -5.0], [10.0, 5.0], [-10.0, 5.0], [-10.0, -5.0]]
    out = server.geometry_op("bbox", geometry={"type": "Polygon", "coordinates": [ring]})
    assert out["bbox"] == [-10.0, -5.0, 10.0, 5.0]


def test_bbox_of_point():
    out = server.geometry_op("bbox", geometry={"type": "Point", "coordinates": [1.5, 2.5]})
    assert out["bbox"] == [1.5, 2.5, 1.5, 2.5]


def test_centroid_of_square_is_center():
    ring = _square_km_ring(5.0, 45.0)
    out = server.geometry_op("centroid", geometry={"type": "Polygon", "coordinates": [ring]})
    assert math.isclose(out["point"]["lat"], 45.0, abs_tol=1e-6)
    assert math.isclose(out["point"]["lon"], 5.0, abs_tol=1e-6)


def test_centroid_of_point_is_itself():
    out = server.geometry_op("centroid", geometry={"type": "Point", "coordinates": [3.0, 4.0]})
    assert out["point"] == {"lat": 4.0, "lon": 3.0}


# ---------------------------------------------------------------------------
# Derive (geometry-returning)
# ---------------------------------------------------------------------------


def test_buffer_contains_its_center_and_has_expected_vertex_count():
    center = P(40.0, -73.0)
    out = server.geometry_op("buffer", point=center, radius_m=500.0)
    geom = out["geometry"]
    assert geom["type"] == "Polygon"
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1]
    assert len(ring) - 1 == geometry_ops.BUFFER_VERTICES
    contains = server.geometry_op("point_in_polygon", points=[center], geometry=geom)["results"]
    assert contains == [True]
    # Each vertex should be ~radius_m from center.
    for lon, lat in ring[:-1]:
        d = server.geometry_op("distance", point=center, point2=P(lat, lon))["distance_m"]
        assert math.isclose(d, 500.0, rel_tol=0.02)


def test_convex_hull_of_square_plus_interior_point_is_the_square():
    points = [P(0, 0), P(0, 1), P(1, 1), P(1, 0), P(0.5, 0.5)]
    out = server.geometry_op("convex_hull", points=points)
    ring = out["geometry"]["coordinates"][0]
    hull_lonlat = {tuple(c) for c in ring[:-1]}
    corners = {(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)}
    assert hull_lonlat == corners


def test_convex_hull_respects_batch_cap():
    points = [P(i * 0.001, 0) for i in range(101)]
    out = server.geometry_op("convex_hull", points=points)
    assert out["error"] == "bad_request"


def test_convex_hull_needs_three_distinct_points():
    out = server.geometry_op("convex_hull", points=[P(0, 0), P(0, 0)])
    assert out["error"] == "bad_request"


def test_geometry_returning_ops_respect_token_budget():
    center = P(0, 0)
    out = server.geometry_op("buffer", point=center, radius_m=1000.0)
    from placeroot import budget

    assert budget.estimate_tokens({"geometry": out["geometry"]}) <= geometry_ops.GEOMETRY_MAX_TOKENS


# ---------------------------------------------------------------------------
# Predicates / selection
# ---------------------------------------------------------------------------


def _square_polygon():
    ring = [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0], [0.0, 0.0]]
    return {"type": "Polygon", "coordinates": [ring]}


def test_point_in_polygon_batch():
    square = _square_polygon()
    points = [P(5, 5), P(50, 50), P(1, 1), P(-1, -1)]
    out = server.geometry_op("point_in_polygon", points=points, geometry=square)
    assert out["results"] == [True, False, True, False]


def test_point_in_polygon_with_hole():
    outer = [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0], [0.0, 0.0]]
    hole = [[4.0, 4.0], [4.0, 6.0], [6.0, 6.0], [6.0, 4.0], [4.0, 4.0]]
    geom = {"type": "Polygon", "coordinates": [outer, hole]}
    out = server.geometry_op("point_in_polygon", points=[P(5, 5), P(1, 1)], geometry=geom)
    assert out["results"] == [False, True]


def test_point_in_polygon_batch_cap():
    square = _square_polygon()
    points = [P(5, 5)] * 101
    out = server.geometry_op("point_in_polygon", points=points, geometry=square)
    assert out["error"] == "bad_request"


def test_nearest_point():
    target = P(0.0, 0.0)
    candidates = [P(10, 10), P(0.001, 0.001), P(5, 5)]
    out = server.geometry_op("nearest_point", point=target, points=candidates)
    assert out["index"] == 1
    assert out["distance_m"] < 1000


def test_nearest_point_on_line_snaps_and_reports_fraction():
    line = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.0, 10.0]]}
    # A point slightly east of the midpoint of the line.
    query = P(5.0, 0.01)
    out = server.geometry_op("nearest_point_on_line", point=query, geometry=line)
    assert math.isclose(out["point"]["lat"], 5.0, abs_tol=0.01)
    assert math.isclose(out["point"]["lon"], 0.0, abs_tol=1e-6)
    assert math.isclose(out["fraction"], 0.5, abs_tol=0.01)
    assert out["distance_m"] > 0


def test_nearest_point_on_line_endpoint_snap():
    line = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.0, 10.0]]}
    query = P(-5.0, 0.0)  # south of the line's start
    out = server.geometry_op("nearest_point_on_line", point=query, geometry=line)
    assert math.isclose(out["fraction"], 0.0, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# Validation matrix
# ---------------------------------------------------------------------------


def test_unknown_op():
    out = server.geometry_op("frobnicate")
    assert out["error"] == "bad_request"
    assert "frobnicate" in out["detail"]


def test_missing_params_per_op_named_in_error():
    out = server.geometry_op("buffer", point=P(0, 0))
    assert out["error"] == "bad_request"
    assert "op=buffer needs point and radius_m" in out["detail"]

    out = server.geometry_op("distance", point=P(0, 0))
    assert out["error"] == "bad_request"
    assert "op=distance needs point and point2" in out["detail"]


def test_bad_coords_lat_out_of_range():
    out = server.geometry_op("distance", point=P(91.0, 0.0), point2=P(0.0, 0.0))
    assert out["error"] == "bad_request"


def test_bad_coords_swapped_lat_lon():
    out = server.geometry_op("distance", point=P(200.0, 10.0), point2=P(0.0, 0.0))
    assert out["error"] == "bad_request"


def test_bad_geometry_type_for_op():
    out = server.geometry_op(
        "area", geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]}
    )
    assert out["error"] == "bad_request"


def test_malformed_geometry():
    out = server.geometry_op("bbox", geometry={"type": "Polygon"})
    assert out["error"] == "bad_request"


def test_point_missing_lat_lon_fields():
    out = server.geometry_op("distance", point={"lat": 0.0}, point2=P(0.0, 0.0))
    assert out["error"] == "bad_request"


# ---------------------------------------------------------------------------
# Regressions from the #365 review
# ---------------------------------------------------------------------------


def _signed_ring_area(ring):
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def test_buffer_near_antimeridian_is_rejected_not_garbage():
    out = server.geometry_op("buffer", point=P(0.0, 179.99), radius_m=5000.0)
    assert out["error"] == "bad_request"
    assert "antimeridian" in out["detail"]


def test_buffer_radius_is_capped():
    out = server.geometry_op("buffer", point=P(40.0, -73.0), radius_m=1e7)
    assert out["error"] == "bad_request"
    assert str(int(geometry_ops.MAX_BUFFER_RADIUS_M)) in out["detail"]
    # At the cap itself (away from the antimeridian) the ring is still valid.
    ok = server.geometry_op(
        "buffer", point=P(0.0, 0.0), radius_m=geometry_ops.MAX_BUFFER_RADIUS_M
    )
    assert ok["geometry"]["type"] == "Polygon"


def test_buffer_exterior_ring_is_counterclockwise():
    out = server.geometry_op("buffer", point=P(40.0, -73.0), radius_m=500.0)
    ring = out["geometry"]["coordinates"][0]
    assert _signed_ring_area(ring) > 0  # CCW per RFC 7946, matching convex_hull
    hull = server.geometry_op(
        "convex_hull", points=[P(0, 0), P(0, 1), P(1, 1), P(1, 0)]
    )["geometry"]["coordinates"][0]
    assert _signed_ring_area(hull) > 0


def test_geometry_inputs_respect_simplify_vertex_cap():
    from placeroot import simplify

    n = simplify.MAX_INPUT_POINTS + 1
    line = {"type": "LineString", "coordinates": [[i * 1e-6, 0.0] for i in range(n)]}
    out = server.geometry_op("bbox", geometry=line)
    assert out["error"] == "bad_request"
    assert str(simplify.MAX_INPUT_POINTS) in out["detail"]


def test_point_in_polygon_results_are_never_budget_truncated(monkeypatch):
    # A tiny token budget must not drop or misalign the positional booleans.
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "5")
    square = _square_polygon()
    points = [P(5, 5), P(50, 50)] * 50  # 100 points, alternating in/out
    out = server.geometry_op("point_in_polygon", points=points, geometry=square)
    assert out["results"] == [True, False] * 50


# ---------------------------------------------------------------------------
# Set ops: union / intersect / difference (issue #404)
# ---------------------------------------------------------------------------


def _poly(ring):
    return {"type": "Polygon", "coordinates": [ring]}


def _unit_square(x0, y0, x1, y1):
    return _poly([[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]])


def test_overlapping_union_area_is_union_of_squares():
    a = _unit_square(0, 0, 2, 2)  # area 4
    b = _unit_square(1, 1, 3, 3)  # area 4, overlap 1
    out = server.geometry_op("union", geometry=a, geometry2=b)
    assert out["geometry"]["type"] == "Polygon"
    assert math.isclose(out["area_km2"], geometry_ops.area(a)["area_km2"] * 7 / 4, rel_tol=1e-3)


def test_overlapping_intersect_area_is_overlap_only():
    a = _unit_square(0, 0, 2, 2)
    b = _unit_square(1, 1, 3, 3)
    out = server.geometry_op("intersect", geometry=a, geometry2=b)
    assert out["geometry"]["type"] == "Polygon"
    # overlap is the unit square [1,1]-[2,2]: 1/4 the area of `a`.
    assert math.isclose(out["area_km2"], geometry_ops.area(a)["area_km2"] / 4, rel_tol=1e-3)


def test_overlapping_difference_area_is_a_minus_overlap():
    a = _unit_square(0, 0, 2, 2)
    b = _unit_square(1, 1, 3, 3)
    out = server.geometry_op("difference", geometry=a, geometry2=b)
    assert out["geometry"]["type"] == "Polygon"
    assert math.isclose(out["area_km2"], geometry_ops.area(a)["area_km2"] * 3 / 4, rel_tol=1e-3)


def test_disjoint_union_is_multipolygon_with_summed_area():
    a = _unit_square(0, 0, 1, 1)
    b = _unit_square(10, 10, 11, 11)
    out = server.geometry_op("union", geometry=a, geometry2=b)
    assert out["geometry"]["type"] == "MultiPolygon"
    expected = geometry_ops.area(a)["area_km2"] + geometry_ops.area(b)["area_km2"]
    # area() reprojects to one reference latitude for the *whole* combined
    # geometry, vs. each square's own reference latitude individually — a
    # small mismatch at this latitude spread is expected (see geometry_ops.py
    # module docstring's accuracy notes), not a rel_tol=1e-6 match.
    assert math.isclose(out["area_km2"], expected, rel_tol=1e-2)


def test_disjoint_intersect_is_empty():
    a = _unit_square(0, 0, 1, 1)
    b = _unit_square(10, 10, 11, 11)
    out = server.geometry_op("intersect", geometry=a, geometry2=b)
    assert out == {"empty": True, "note": "geometry and geometry2 do not overlap"}


def test_disjoint_difference_is_identity():
    a = _unit_square(0, 0, 1, 1)
    b = _unit_square(10, 10, 11, 11)
    out = server.geometry_op("difference", geometry=a, geometry2=b)
    assert out["geometry"]["type"] == "Polygon"
    # Same reference-latitude caveat as the union case above.
    assert math.isclose(out["area_km2"], geometry_ops.area(a)["area_km2"], rel_tol=1e-2)


def test_contained_intersect_is_the_inner_shape():
    outer = _unit_square(0, 0, 10, 10)
    inner = _unit_square(2, 2, 4, 4)
    out = server.geometry_op("intersect", geometry=outer, geometry2=inner)
    assert math.isclose(out["area_km2"], geometry_ops.area(inner)["area_km2"], rel_tol=1e-3)


def test_contained_difference_of_inner_from_outer_is_empty():
    # geometry2 (outer) fully covers geometry (inner) -> empty difference.
    outer = _unit_square(0, 0, 10, 10)
    inner = _unit_square(2, 2, 4, 4)
    out = server.geometry_op("difference", geometry=inner, geometry2=outer)
    assert out == {"empty": True, "note": "geometry is fully covered by geometry2"}


def test_contained_difference_of_outer_minus_inner_keeps_a_hole():
    outer = _unit_square(0, 0, 10, 10)
    inner = _unit_square(2, 2, 4, 4)
    out = server.geometry_op("difference", geometry=outer, geometry2=inner)
    expected = geometry_ops.area(outer)["area_km2"] - geometry_ops.area(inner)["area_km2"]
    assert math.isclose(out["area_km2"], expected, rel_tol=1e-3)


def test_multipolygon_input_union():
    mp = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]],
            [[[5.0, 5.0], [5.0, 6.0], [6.0, 6.0], [6.0, 5.0], [5.0, 5.0]]],
        ],
    }
    other = _unit_square(0.5, 0.5, 1.5, 1.5)
    out = server.geometry_op("union", geometry=mp, geometry2=other)
    assert out["geometry"]["type"] == "MultiPolygon"
    assert out["area_km2"] > 0


def test_budget_bounded_large_vertex_result():
    n = 3000
    ring = [
        [10.0 * math.cos(2 * math.pi * i / n), 10.0 * math.sin(2 * math.pi * i / n)]
        for i in range(n)
    ]
    ring.append(ring[0])
    circle = _poly(ring)
    far = _unit_square(100.0, 100.0, 101.0, 101.0)
    out = server.geometry_op("union", geometry=circle, geometry2=far)
    assert out["geometry"]["type"] == "MultiPolygon"
    assert out["kept_points"] < out["original_points"]
    # Never an unbounded coordinate dump.
    total_pts = sum(len(ring) for poly in out["geometry"]["coordinates"] for ring in poly)
    assert total_pts < 200


def test_union_missing_geometry2_is_bad_request():
    a = _unit_square(0, 0, 1, 1)
    out = server.geometry_op("union", geometry=a)
    assert out["error"] == "bad_request"
    assert "geometry2" in out["detail"]


def test_intersect_wrong_type_for_geometry2():
    a = _unit_square(0, 0, 1, 1)
    line = {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]}
    out = server.geometry_op("intersect", geometry=a, geometry2=line)
    assert out["error"] == "bad_request"


def test_difference_malformed_geometry2():
    a = _unit_square(0, 0, 1, 1)
    out = server.geometry_op("difference", geometry=a, geometry2={"type": "Polygon"})
    assert out["error"] == "bad_request"


def test_union_op_appears_in_schema_docs():
    assert "union" in server._GEOMETRY_OP_REQUIRED
    assert "intersect" in server._GEOMETRY_OP_REQUIRED
    assert "difference" in server._GEOMETRY_OP_REQUIRED
    assert server._GEOMETRY_OP_REQUIRED["union"] == ("geometry", "geometry2")
    assert server._GEOMETRY_OP_REQUIRED["intersect"] == ("geometry", "geometry2")
    assert server._GEOMETRY_OP_REQUIRED["difference"] == ("geometry", "geometry2")
