"""Server-level and engine-level tests for geocode_intersection (#448).

Locates where two named streets cross within a resolved city anchor.
Reuses the 20x20 street grid offline fixture ("Grid Ave {j}", "Grid St {i}"),
with city anchor "Grid City" at (40.74, -73.99).
"""

import math

from placeroot import geocode, overture, routing, server, tool_profiles
from tests._routing_fixture import build_routing_fixture as fx


def test_happy_path_finds_exact_fixture_intersection():
    """Named grid fixture yields exact node coordinates for Grid Ave 2 × Grid St 3."""
    result = server.geocode_intersection("Grid Ave 2", "Grid St 3", "Grid City")

    assert "error" not in result
    results = result["results"]
    assert len(results) == 1
    hit = results[0]

    expected_lat, expected_lon = fx.node_latlon(3, 2)
    assert math.isclose(hit["lat"], expected_lat, abs_tol=1e-6)
    assert math.isclose(hit["lon"], expected_lon, abs_tol=1e-6)

    assert hit["streets"] == ["Grid Ave 2", "Grid St 3"]
    assert hit["anchor"]["name"] == "Grid City"
    assert hit["anchor"]["country"] == "US"


def test_case_insensitive_matching():
    """Matching is case-insensitive."""
    result = server.geocode_intersection("grid ave 2", "GRID ST 3", "Grid City")

    assert len(result["results"]) == 1
    hit = result["results"][0]
    expected_lat, expected_lon = fx.node_latlon(3, 2)
    assert math.isclose(hit["lat"], expected_lat, abs_tol=1e-6)
    assert math.isclose(hit["lon"], expected_lon, abs_tol=1e-6)


def test_abbreviation_and_ordinal_variants():
    """Expansions (Avenue/Ave, Street/St, 2nd/2, 3rd/3) resolve to the same intersection."""
    result = server.geocode_intersection("Grid Avenue 2nd", "Grid Street 3rd", "Grid City")

    assert len(result["results"]) == 1
    hit = result["results"][0]
    expected_lat, expected_lon = fx.node_latlon(3, 2)
    assert math.isclose(hit["lat"], expected_lat, abs_tol=1e-6)
    assert math.isclose(hit["lon"], expected_lon, abs_tol=1e-6)


def test_parallel_streets_return_empty_results_and_note():
    """Two parallel streets never intersect -> empty results + descriptive note."""
    result = server.geocode_intersection("Grid Ave 2", "Grid Ave 3", "Grid City")

    assert result["results"] == []
    assert "note" in result
    assert "do not intersect" in result["note"]


def test_unresolved_second_street_names_the_missing_street():
    """When street_b does not exist in the city graph, the note names street_b."""
    result = server.geocode_intersection("Grid Ave 2", "Nonexistent Boulevard", "Grid City")

    assert result["results"] == []
    assert "note" in result
    assert "Nonexistent Boulevard" in result["note"]
    assert "did not resolve" in result["note"]


def test_unresolved_first_street_names_the_missing_street():
    """When street_a does not exist in the city graph, the note names street_a."""
    result = server.geocode_intersection("Nonexistent Boulevard", "Grid St 3", "Grid City")

    assert result["results"] == []
    assert "note" in result
    assert "Nonexistent Boulevard" in result["note"]
    assert "did not resolve" in result["note"]


def test_neither_street_resolves():
    """When neither street matches any edge in the city graph, note names both."""
    result = server.geocode_intersection("Fake Road", "Bogus Lane", "Grid City")

    assert result["results"] == []
    assert "note" in result
    assert "Fake Road" in result["note"]
    assert "Bogus Lane" in result["note"]


def test_missing_city_returns_empty_results_and_note():
    """Missing city refuses scan honestly."""
    result = server.geocode_intersection("Grid Ave 2", "Grid St 3", "")

    assert result["results"] == []
    assert "note" in result


def test_missing_street_names_return_empty_results_and_note():
    """Missing street_a or street_b returns empty results and note."""
    result_both = server.geocode_intersection("", "", "Grid City")
    assert result_both["results"] == []
    assert "note" in result_both

    result_a = server.geocode_intersection("", "Grid St 3", "Grid City")
    assert result_a["results"] == []
    assert "note" in result_a

    result_b = server.geocode_intersection("Grid Ave 2", "", "Grid City")
    assert result_b["results"] == []
    assert "note" in result_b


def test_unresolved_city_returns_empty_results_and_note():
    """City that doesn't exist in Overture returns empty results with note."""
    result = server.geocode_intersection("Grid Ave 2", "Grid St 3", "ZzzzqqxxCity123")

    assert result["results"] == []
    assert "note" in result
    assert "ZzzzqqxxCity123" in result["note"]


def test_multiple_crossings_ordered_by_distance_and_capped(monkeypatch):
    """Multiple crossings are ordered nearest-to-center first and capped at 5."""
    # Build a graph with a street crossing another at 7 distinct locations
    center_lat, center_lon = 40.74, -73.99
    g = routing.Graph()
    g.has_shapes = True

    # Center node
    g.add_node("c_0", center_lat, center_lon)

    # 7 intersection nodes at increasing distances north
    for idx in range(1, 8):
        lat = center_lat + idx * 0.001
        lon = center_lon
        nid = f"cross_{idx}"
        g.add_node(nid, lat, lon)
        # Add edges on Main St (horizontal) and Cross Ave (vertical)
        g.add_node(f"west_{idx}", lat, lon - 0.001)
        g.add_node(f"east_{idx}", lat, lon + 0.001)
        g.add_edge(nid, f"west_{idx}", 100.0, 100.0, directed=False, name="Main St")
        g.add_edge(nid, f"east_{idx}", 100.0, 100.0, directed=False, name="Main St")

        g.add_node(f"north_{idx}", lat + 0.0005, lon)
        g.add_edge(nid, f"north_{idx}", 50.0, 50.0, directed=False, name="Cross Ave")

    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *args, **kwargs: g)

    result = geocode.geocode_intersection("Main St", "Cross Ave", "Grid City")

    assert len(result["results"]) == geocode.INTERSECTION_MAX_LIMIT
    assert len(result["results"]) == 5

    # Check ordering: increasing distance from anchor center
    distances = [
        math.hypot(r["lat"] - center_lat, r["lon"] - center_lon)
        for r in result["results"]
    ]
    assert distances == sorted(distances)


def test_search_profile_contains_geocode_intersection():
    """geocode_intersection is registered under the search tool profile."""
    assert "geocode_intersection" in tool_profiles.PROFILES["search"]


def test_server_upstream_error_handling(monkeypatch):
    """UpstreamUnavailable is converted to standard structured error."""
    def fail(*_a, **_k):
        raise overture.UpstreamUnavailable("S3 down")

    monkeypatch.setattr(geocode, "geocode_intersection", fail)
    result = server.geocode_intersection("Grid Ave 2", "Grid St 3", "Grid City")
    assert result["error"] == "upstream_unavailable"


def test_server_schema_error_handling(monkeypatch):
    """SchemaDegraded is converted to standard structured error."""
    def fail(*_a, **_k):
        raise overture.SchemaDegraded("missing column")

    monkeypatch.setattr(geocode, "geocode_intersection", fail)
    result = server.geocode_intersection("Grid Ave 2", "Grid St 3", "Grid City")
    assert result["error"] == "schema_degraded"


# --- Review findings regression tests ---------------------------------------


def test_self_intersection_rejected():
    """Identical or normalized-equivalent street names are rejected with clear note."""
    # Exact same
    res = server.geocode_intersection("Main Street", "Main Street", "Grid City")
    assert res["results"] == []
    assert "refer to the same street" in res["note"]

    # Abbreviation variant
    res2 = server.geocode_intersection("Main Street", "Main St", "Grid City")
    assert res2["results"] == []
    assert "refer to the same street" in res2["note"]

    # Ordinal variant
    res3 = server.geocode_intersection("5th Ave", "Fifth Avenue", "Grid City")
    assert res3["results"] == []
    assert "refer to the same street" in res3["note"]

    # Cardinal / quadrant variant
    res4 = server.geocode_intersection("W 42nd St", "West 42nd Street", "Grid City")
    assert res4["results"] == []
    assert "refer to the same street" in res4["note"]

    # Prefix variant
    res5 = server.geocode_intersection("Market St", "Market St NW", "Grid City")
    assert res5["results"] == []
    assert "refer to the same street" in res5["note"]


def test_directional_suffix_word_boundary_prefix_matching(monkeypatch):
    """Directional suffixes on OSM edge names match via word-boundary prefix."""
    center_lat, center_lon = 40.74, -73.99
    g = routing.Graph()
    g.add_node("center", center_lat, center_lon)
    g.add_node("cross", center_lat + 0.001, center_lon)
    g.add_node("w", center_lat + 0.001, center_lon - 0.001)
    g.add_node("e", center_lat + 0.001, center_lon + 0.001)
    g.add_node("n", center_lat + 0.002, center_lon)

    # Road edges in graph have directional suffix "NW" and "S"
    g.add_edge("cross", "w", 100.0, 100.0, directed=False, name="Pennsylvania Avenue NW")
    g.add_edge("cross", "e", 100.0, 100.0, directed=False, name="Pennsylvania Avenue NW")
    g.add_edge("cross", "n", 100.0, 100.0, directed=False, name="14th Street NW")

    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g)

    # Caller query without directional suffix should match
    res = geocode.geocode_intersection("Pennsylvania Ave", "14th St", "Grid City")
    assert len(res["results"]) == 1
    assert res["results"][0]["streets"] == ["Pennsylvania Avenue NW", "14th Street NW"]

    # Non-word-boundary prefix should NOT match (e.g. "Penn" != "Pennsylvania")
    res_no = geocode.geocode_intersection("Penn", "14th St", "Grid City")
    assert res_no["results"] == []
    assert "did not resolve" in res_no["note"]


def test_divided_road_junction_clustering(monkeypatch):
    """Divided road junction near-duplicates within 50m are clustered to 1 result."""
    center_lat, center_lon = 40.74, -73.99
    g = routing.Graph()
    g.add_node("center", center_lat, center_lon)

    # 4 junction nodes forming a dual-carriageway box intersection ~15m apart
    # cross_1 is closest to center
    nodes = [
        ("cross_1", center_lat + 0.0010, center_lon),
        ("cross_2", center_lat + 0.0011, center_lon),
        ("cross_3", center_lat + 0.0010, center_lon + 0.0001),
        ("cross_4", center_lat + 0.0011, center_lon + 0.0001),
    ]
    for nid, lat, lon in nodes:
        g.add_node(nid, lat, lon)
        g.add_node(f"w_{nid}", lat, lon - 0.001)
        g.add_node(f"n_{nid}", lat + 0.001, lon)
        g.add_edge(nid, f"w_{nid}", 50.0, 50.0, directed=False, name="Divided Ave")
        g.add_edge(nid, f"n_{nid}", 50.0, 50.0, directed=False, name="Cross St")

    # A separate 2nd intersection 1.5 km away
    far_lat, far_lon = center_lat + 0.015, center_lon
    g.add_node("far_cross", far_lat, far_lon)
    g.add_node("far_w", far_lat, far_lon - 0.001)
    g.add_node("far_n", far_lat + 0.001, far_lon)
    g.add_edge("far_cross", "far_w", 50.0, 50.0, directed=False, name="Divided Ave")
    g.add_edge("far_cross", "far_n", 50.0, 50.0, directed=False, name="Cross St")

    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g)

    res = geocode.geocode_intersection("Divided Ave", "Cross St", "Grid City")
    # 4 near-duplicate junction nodes clustered into 1, plus 1 far intersection = 2 results total
    assert len(res["results"]) == 2
    # The nearest of the 4 was cross_1
    assert math.isclose(res["results"][0]["lat"], center_lat + 0.0010, abs_tol=1e-6)
    assert math.isclose(res["results"][1]["lat"], far_lat, abs_tol=1e-6)


def test_intersection_specific_notes_do_not_borrow_address_scan_language():
    """Unresolved notes use intersection-specific wording, not address scan / address_at."""
    res_no_city = server.geocode_intersection("Main St", "Cross St", "")
    assert "no city to search in" in res_no_city["note"]
    assert "address" not in res_no_city["note"].lower()

    res_unresolved = server.geocode_intersection("Main St", "Cross St", "NonExistentCity999")
    assert "did not resolve to any place" in res_unresolved["note"]
    assert "search intersections within" in res_unresolved["note"]
    assert "address" not in res_unresolved["note"].lower()


def test_want_shapes_false_in_graph_extraction(monkeypatch):
    """Graph extraction for intersections does not request shapes."""
    captured_args = {}
    orig_fn = routing._get_or_build_graph

    def spy_get_graph(*args, **kwargs):
        captured_args["want_shapes"] = kwargs.get("want_shapes")
        captured_args["radius_m"] = args[2] if len(args) > 2 else kwargs.get("extraction_radius_m")
        return orig_fn(*args, **kwargs)

    monkeypatch.setattr(routing, "_get_or_build_graph", spy_get_graph)

    res = server.geocode_intersection("Grid Ave 2", "Grid St 3", "Grid City")
    assert len(res["results"]) == 1
    assert captured_args.get("want_shapes") is False
    assert captured_args.get("radius_m") <= routing.WALK_MAX_RADIUS_M

