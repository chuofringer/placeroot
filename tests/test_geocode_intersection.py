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
    # One top-level anchor, the geocode_address shape, never repeated per row.
    assert "anchor" not in hit
    assert result["anchor"]["name"] == "Grid City"
    assert result["anchor"]["id"] == "gers-div-grid-city"
    assert result["anchor"]["country"] == "US"
    assert "truncated" not in result


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
        g.add_node(f"e_{nid}", lat, lon + 0.001)
        g.add_node(f"n_{nid}", lat + 0.001, lon)
        g.add_edge(nid, f"w_{nid}", 50.0, 50.0, directed=False, name="Divided Ave")
        g.add_edge(nid, f"e_{nid}", 50.0, 50.0, directed=False, name="Divided Ave")
        g.add_edge(nid, f"n_{nid}", 50.0, 50.0, directed=False, name="Cross St")

    # A separate 2nd intersection 1.5 km away
    far_lat, far_lon = center_lat + 0.015, center_lon
    g.add_node("far_cross", far_lat, far_lon)
    g.add_node("far_w", far_lat, far_lon - 0.001)
    g.add_node("far_e", far_lat, far_lon + 0.001)
    g.add_node("far_n", far_lat + 0.001, far_lon)
    g.add_edge("far_cross", "far_w", 50.0, 50.0, directed=False, name="Divided Ave")
    g.add_edge("far_cross", "far_e", 50.0, 50.0, directed=False, name="Divided Ave")
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



# --- Second review round ----------------------------------------------------

CENTER_LAT, CENTER_LON = 40.74, -73.99
# Matches scripts/build_fixture.py's GEOCODE_ANCHOR_AREAS row for Grid City.
GRID_CITY_BBOX = (-74.05, 40.70, -73.93, 40.78)


def _fake_grid_city_anchor(monkeypatch, bbox=GRID_CITY_BBOX):
    """Resolve "Grid City" without DuckDB, so a test that also swaps the graph
    exercises the node loop alone."""
    anchor = {
        "name": "Grid City", "id": "gers-div-grid-city", "country": "US",
        "lat": CENTER_LAT, "lon": CENTER_LON,
        "admin_context": ["United States", "New York"],
    }

    def resolve(city, *, action_label="scan"):
        return geocode._ResolvedAnchor(
            anchor=anchor, bbox=bbox, notes=[], top=anchor, rejected=[], too_broad=None
        )

    monkeypatch.setattr(geocode, "_resolve_city_anchor", resolve)


def _junction(g, nid, lat, lon, name_ew, name_n, name_e=None):
    """A T-junction at (lat, lon): `name_ew` runs west and east through it,
    `name_n` leaves north. `name_e` renames the eastern leg."""
    g.add_node(nid, lat, lon)
    g.add_node(f"{nid}_w", lat, lon - 0.001)
    g.add_node(f"{nid}_e", lat, lon + 0.001)
    g.add_node(f"{nid}_n", lat + 0.001, lon)
    g.add_edge(nid, f"{nid}_w", 100.0, 100.0, directed=False, name=name_ew)
    g.add_edge(nid, f"{nid}_e", 100.0, 100.0, directed=False, name=name_e or name_ew)
    g.add_edge(nid, f"{nid}_n", 100.0, 100.0, directed=False, name=name_n)


def test_anchor_is_top_level_and_not_repeated_per_row(monkeypatch):
    _fake_grid_city_anchor(monkeypatch)
    g = routing.Graph()
    _junction(g, "x", CENTER_LAT + 0.001, CENTER_LON, "Main St", "Cross Ave")
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g)

    res = geocode.geocode_intersection("Main St", "Cross Ave", "Grid City")
    assert res["anchor"] == {
        "name": "Grid City", "id": "gers-div-grid-city", "country": "US",
        "admin_context": ["United States", "New York"],
    }
    assert res["results"] == [
        {"lat": CENTER_LAT + 0.001, "lon": CENTER_LON, "streets": ["Main St", "Cross Ave"]}
    ]


def test_rename_point_on_one_road_is_not_a_crossing(monkeypatch):
    """Main St becoming Broadway at a degree-2 node is one road, not two meeting."""
    _fake_grid_city_anchor(monkeypatch)
    g = routing.Graph()
    lat = CENTER_LAT + 0.001
    g.add_node("rename", lat, CENTER_LON)
    g.add_node("rename_w", lat, CENTER_LON - 0.001)
    g.add_node("rename_e", lat, CENTER_LON + 0.001)
    g.add_edge("rename", "rename_w", 100.0, 100.0, directed=False, name="Main St")
    g.add_edge("rename", "rename_e", 100.0, 100.0, directed=False, name="Broadway")
    # ...and the real junction further out, which must still be found.
    _junction(g, "real", CENTER_LAT + 0.005, CENTER_LON, "Main St", "Broadway")
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g)

    res = geocode.geocode_intersection("Main St", "Broadway", "Grid City")
    assert [r["lat"] for r in res["results"]] == [CENTER_LAT + 0.005]

    # With only the rename point in the graph: both streets exist, no crossing.
    g2 = routing.Graph()
    g2.add_node("rename", lat, CENTER_LON)
    g2.add_node("rename_w", lat, CENTER_LON - 0.001)
    g2.add_node("rename_e", lat, CENTER_LON + 0.001)
    g2.add_edge("rename", "rename_w", 100.0, 100.0, directed=False, name="Main St")
    g2.add_edge("rename", "rename_e", 100.0, 100.0, directed=False, name="Broadway")
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g2)
    res2 = geocode.geocode_intersection("Main St", "Broadway", "Grid City")
    assert res2["results"] == []
    assert "do not intersect" in res2["note"]


def test_longer_street_name_is_a_different_street(monkeypatch):
    """Only a directional suffix may extend a query; "Broadway Terrace" is not
    "Broadway", and a lone "Main" is not "Main Ave"."""
    assert geocode._matches_street("Broadway Terrace", {"broadway"}) is False
    assert geocode._matches_street("Park Ave Ext", {"park ave", "park avenue"}) is False
    assert geocode._matches_street("Main St Bridge", {"main st", "main street"}) is False
    assert geocode._matches_street("Main Ave", {"main"}) is False
    assert geocode._matches_street("Main Street North", {"main street"}) is True
    assert geocode._matches_street("Pennsylvania Avenue NW", {"pennsylvania avenue"}) is True
    assert geocode._matches_street("Pennsylvania Avenue", {"pennsylvania avenue nw"}) is False

    _fake_grid_city_anchor(monkeypatch)
    g = routing.Graph()
    _junction(g, "x", CENTER_LAT + 0.001, CENTER_LON, "Broadway", "Broadway Terrace")
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g)
    res = geocode.geocode_intersection("Broadway", "Broadway Terrace", "Grid City")
    assert "same street" not in res.get("note", "")
    assert res["results"][0]["streets"] == ["Broadway", "Broadway Terrace"]


def test_crossings_outside_the_city_extent_are_not_reported(monkeypatch):
    """A cached graph wider than the city must not hand back the next town's
    crossing under this city's anchor."""
    # A city extent ~1.1 km across, well inside the 5 km graph.
    _fake_grid_city_anchor(monkeypatch, bbox=(-73.995, 40.735, -73.985, 40.745))
    g = routing.Graph()
    _junction(g, "inside", CENTER_LAT + 0.001, CENTER_LON, "Main St", "1st St")
    _junction(g, "outside", CENTER_LAT + 0.02, CENTER_LON, "Main St", "1st St")
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g)

    res = geocode.geocode_intersection("Main St", "1st St", "Grid City")
    assert [r["lat"] for r in res["results"]] == [CENTER_LAT + 0.001]

    g2 = routing.Graph()
    _junction(g2, "outside", CENTER_LAT + 0.02, CENTER_LON, "Main St", "1st St")
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g2)
    res2 = geocode.geocode_intersection("Main St", "1st St", "Grid City")
    assert res2["results"] == []
    assert "neither" in res2["note"]


def test_truncated_graph_is_surfaced_not_asserted_as_absence(monkeypatch):
    _fake_grid_city_anchor(monkeypatch)
    g = routing.Graph()
    g.truncated = True
    _junction(g, "x", CENTER_LAT + 0.001, CENTER_LON, "Main St", "Elm St")
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g)

    res = geocode.geocode_intersection("Main St", "Oak St", "Grid City")
    assert res["results"] == []
    assert res["truncated"] is True
    assert '"Oak St" did not resolve in the extracted part of Grid City' in res["note"]
    assert "segment cap" in res["note"]

    hit = geocode.geocode_intersection("Main St", "Elm St", "Grid City")
    assert len(hit["results"]) == 1
    assert hit["truncated"] is True
    assert "segment cap" in hit["note"]


def test_large_city_note_says_the_search_was_bounded(monkeypatch):
    """A bbox wider than the walk cap: an empty answer says how far was searched."""
    _fake_grid_city_anchor(monkeypatch, bbox=(-74.3, 40.5, -73.7, 41.0))
    g = routing.Graph()
    _junction(g, "x", CENTER_LAT + 0.001, CENTER_LON, "Main St", "Elm St")
    monkeypatch.setattr(routing, "_get_or_build_graph", lambda *a, **k: g)

    res = geocode.geocode_intersection("Main St", "Oak St", "Grid City")
    assert res["results"] == []
    assert "5.0 km" in res["note"]
    assert "neighborhood" in res["note"]


def test_edge_names_follow_the_lightest_parallel_edge():
    """name_between names the edge dijkstra traverses: on parallel edges the
    lightest wins, first-registered on a tie, and an unnamed lighter edge
    clears a heavier named one's slot -- on shapeless and shape graphs alike."""
    for has_shapes in (False, True):
        g = routing.Graph()
        g.has_shapes = has_shapes
        g.add_node("a", 0.0, 0.0)
        g.add_node("b", 0.0, 0.001)
        g.add_edge("a", "b", 50.0, 50.0, directed=False, name=None)
        g.add_edge("a", "b", 100.0, 100.0, directed=False, name="Main St")
        assert g.name_between("a", "b") is None
        assert g.name_between("b", "a") is None

        g = routing.Graph()
        g.has_shapes = has_shapes
        g.add_node("a", 0.0, 0.0)
        g.add_node("b", 0.0, 0.001)
        g.add_edge("a", "b", 100.0, 100.0, directed=False, name="Main St")
        g.add_edge("a", "b", 50.0, 50.0, directed=False, name="Service Rd")
        assert g.name_between("a", "b") == "Service Rd"
        g.add_edge("a", "b", 50.0, 50.0, directed=False, name="Tie Loser")
        assert g.name_between("a", "b") == "Service Rd"
        g.add_edge("a", "b", 25.0, 25.0, directed=False, name=None)
        assert g.name_between("a", "b") is None

        # One-way edges compete per direction only.
        g = routing.Graph()
        g.has_shapes = has_shapes
        g.add_node("a", 0.0, 0.0)
        g.add_node("b", 0.0, 0.001)
        g.add_edge("a", "b", 100.0, 100.0, directed=True, name="Northbound")
        g.add_edge("b", "a", 50.0, 50.0, directed=True, name="Southbound")
        assert g.name_between("a", "b") == "Northbound"
        assert g.name_between("b", "a") == "Southbound"


def test_disk_format_bumped_for_edge_names_on_shapeless_graphs():
    """Format-2 shapeless pickles carry no edge names; they must miss, not load."""
    assert routing.GRAPH_DISK_FORMAT >= 3
