"""Class-based road avoidance: route()/from_to()'s avoid=["motorway", ...] (#425).

Runs against the same committed 20x20 grid fixture as the other routing
tests (scripts/build_routing_fixture.py). The fixture already carries the
one thing this feature needs: a motorway "shortcut" segment directly
joining grid nodes SHORTCUT["from"] and SHORTCUT["to"], which are also
connected the long way round through ordinary residential grid edges. So a
drive route between those two nodes takes the motorway by default and must
take the (longer) grid path when the caller avoids it — no fixture change,
no synthetic graph.
"""

import asyncio
import inspect

import pytest

from placeroot import routing, server

from ._routing_fixture import build_routing_fixture as fx

# The two ends of the fixture's motorway shortcut.
FROM_LAT, FROM_LON = fx.node_latlon(*fx.SHORTCUT["from"])
TO_LAT, TO_LON = fx.node_latlon(*fx.SHORTCUT["to"])


def _build_spy(monkeypatch) -> list:
    """Record every build_graph call that actually reaches the extraction."""
    built = []
    real = routing.build_graph

    def spy(*args, **kwargs):
        built.append(kwargs.get("avoid", ()))
        return real(*args, **kwargs)

    monkeypatch.setattr(routing, "build_graph", spy)
    return built


# --- the route actually changes ------------------------------------------


def test_avoid_motorway_takes_the_longer_grid_path_on_drive():
    plain = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive")
    avoided = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["motorway"])
    assert "error" not in avoided
    assert avoided["distance_m"] > plain["distance_m"]
    assert avoided["avoid"] == ["motorway"]
    assert "avoid_note" not in avoided  # drive really does avoid something


def test_avoid_removes_the_motorway_edge_from_the_built_graph():
    graph = routing.build_graph(
        fx.ORIGIN_LAT, fx.ORIGIN_LON, 3000, mode="drive", avoid=["motorway"]
    )
    a, b = fx.node_id(*fx.SHORTCUT["from"]), fx.node_id(*fx.SHORTCUT["to"])
    assert b not in {n for n, _w, _len in graph.adjacency[a]}
    # ...and the same graph without avoid still has it.
    plain = routing.build_graph(fx.ORIGIN_LAT, fx.ORIGIN_LON, 3000, mode="drive")
    assert b in {n for n, _w, _len in plain.adjacency[a]}


def test_avoiding_a_class_also_removes_its_link_ramps():
    assert routing._avoid_expanded(["motorway"]) == {"motorway", "motorway_link"}
    assert routing._excluded_classes_for("drive", ["trunk"]) == (
        set(routing.EXCLUDED_DRIVE_CLASSES) | {"trunk", "trunk_link"}
    )


# --- cache identity -------------------------------------------------------


def test_different_avoid_sets_never_share_a_cached_graph(monkeypatch):
    routing.clear_graph_cache()
    built = _build_spy(monkeypatch)
    routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive")
    routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["motorway"])
    routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["trunk"])
    routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["motorway", "trunk"])
    assert len(built) == 4, "each distinct avoid set is its own graph"
    # ...and each of those four is now separately warm: no rebuild on repeat.
    del built[:]
    for avoid in (None, ["motorway"], ["trunk"], ["trunk", "motorway"]):
        routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=avoid)
    assert built == []
    avoid_tags = {key[4] for key in routing._graph_cache}
    assert len(avoid_tags) == 4
    assert "" in avoid_tags  # the plain graph's tag


def test_avoid_order_and_duplicates_do_not_split_the_cache(monkeypatch):
    routing.clear_graph_cache()
    built = _build_spy(monkeypatch)
    routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["motorway", "trunk"])
    routing.route(
        FROM_LAT,
        FROM_LON,
        TO_LAT,
        TO_LON,
        mode="drive",
        avoid=["trunk", "motorway", "trunk"],
    )
    assert len(built) == 1


def test_disk_persist_name_and_tag_round_trip():
    tile = routing._graph_cache_tile(FROM_LAT, FROM_LON)
    plain = routing._graph_disk_name("drive", "baked", tile, 1000, False)
    avoiding = routing._graph_disk_name("drive", "baked", tile, 1000, False, "motorway")
    assert plain != avoiding
    # A graph persisted before #425 keeps its exact filename.
    assert plain == f"drive_baked_n_r1000_t{tile[0]}_{tile[1]}.pkl"
    assert routing._disk_name_avoid_tag(plain) == ""
    assert routing._disk_name_avoid_tag(avoiding) == "motorway"


def test_avoid_graph_on_disk_is_not_served_to_a_plain_request(tmp_path, monkeypatch):
    """The disk peek globs by mode/speed prefix, which matches avoid files
    too — the tag filter is what keeps a plain route off an avoiding graph."""
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)
    routing.clear_graph_cache()
    routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["motorway"])
    routing.clear_graph_cache()  # in-process equivalent of a restart
    assert not routing.route_graph_is_cached(FROM_LAT, FROM_LON, TO_LAT, TO_LON, "drive")
    assert routing.route_graph_is_cached(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, "drive", avoid=["motorway"]
    )


# --- the needs_confirm gate ----------------------------------------------


def test_avoid_miss_on_a_warm_plain_graph_needs_confirm():
    routing.clear_graph_cache()
    warm = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", confirm=True)
    assert "error" not in warm
    # The plain graph is warm; the avoiding one has never been built here,
    # and building it is the same cold cost the gate exists to announce.
    asked = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["motorway"])
    assert asked["error"] == "needs_confirm"
    confirmed = server.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["motorway"], confirm=True
    )
    assert "error" not in confirmed
    # Now warm in its own right: the same avoiding call no longer asks.
    again = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["motorway"])
    assert "error" not in again
    assert again["distance_m"] == confirmed["distance_m"]


def test_a_no_op_avoid_reuses_the_warm_graph_and_never_asks():
    routing.clear_graph_cache()
    assert "error" not in server.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", confirm=True
    )
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="walk", avoid=["motorway"])
    assert "error" not in result


# --- walk/cycle: a documented no-op --------------------------------------


@pytest.mark.parametrize("mode", ["walk", "cycle"])
def test_avoid_is_a_no_op_for_modes_that_already_exclude_those_classes(mode):
    plain = routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode=mode)
    avoided = routing.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode=mode, avoid=["motorway", "trunk"]
    )
    assert avoided["distance_m"] == plain["distance_m"]
    assert avoided["duration_s"] == plain["duration_s"]
    assert avoided["avoid"] == ["motorway", "trunk"]
    assert mode in avoided["avoid_note"]
    assert routing._graph_cache_avoid_tag(mode, ["motorway", "trunk"]) == ""


# --- invalid values -------------------------------------------------------


@pytest.mark.parametrize("bad", [["tolls"], ["ferries"], ["residential"], "motorway", [3]])
def test_invalid_avoid_is_bad_request_listing_the_valid_values(bad):
    result = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=bad)
    assert result["error"] == "bad_request"
    assert result["supported"] == ["motorway", "trunk"]
    assert "avoid" in result["detail"]


def test_invalid_avoid_raises_from_the_routing_layer():
    with pytest.raises(ValueError, match="motorway"):
        routing.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=["tolls"])


def test_normalize_avoid_sorts_and_deduplicates():
    assert routing.normalize_avoid(None) == ()
    assert routing.normalize_avoid([]) == ()
    assert routing.normalize_avoid(["trunk", "motorway", "trunk"]) == ("motorway", "trunk")


def test_graph_is_cached_treats_an_invalid_avoid_as_a_miss():
    assert routing.graph_is_cached(FROM_LAT, FROM_LON, "drive", avoid=["tolls"]) is False


# --- no_route hint --------------------------------------------------------


def test_no_route_hint_names_the_avoid_set():
    """The fixture's only river crossing is a footway bridge, so the two
    banks are disconnected for drive. What is asserted here is the hint
    wiring: when the call carried an avoid set, the answer offers "retry
    without avoid" first and keeps the mode's own next move after it."""
    near = fx.node_latlon(fx.RIVER_GAP_I, fx.BRIDGE_J)
    far = fx.node_latlon(fx.RIVER_GAP_I + 1, fx.BRIDGE_J)
    result = routing.route(near[0], near[1], far[0], far[1], mode="drive", avoid=["motorway"])
    assert result["error"] == "no_route"
    assert "avoid=['motorway']" in result["try"]
    assert "retry without avoid" in result["try"]
    assert routing._NO_ROUTE_TRY["drive"] in result["try"]
    assert result["avoid"] == ["motorway"]


def test_no_route_hint_is_unchanged_without_avoid():
    near = fx.node_latlon(fx.RIVER_GAP_I, fx.BRIDGE_J)
    far = fx.node_latlon(fx.RIVER_GAP_I + 1, fx.BRIDGE_J)
    result = routing.route(near[0], near[1], far[0], far[1], mode="drive")
    assert result["try"] == routing._NO_ROUTE_TRY["drive"]
    assert "avoid" not in result


# --- regression: nothing moves without avoid ------------------------------


def test_calls_without_avoid_are_byte_identical():
    routing.clear_graph_cache()
    before = server.route(FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", confirm=True)
    routing.clear_graph_cache()
    explicit_none = server.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=None, confirm=True
    )
    routing.clear_graph_cache()
    empty_list = server.route(
        FROM_LAT, FROM_LON, TO_LAT, TO_LON, mode="drive", avoid=[], confirm=True
    )
    for other in (explicit_none, empty_list):
        assert {k: v for k, v in other.items() if k != "progress"} == {
            k: v for k, v in before.items() if k != "progress"
        }
    assert "avoid" not in before


# --- from_to -------------------------------------------------------------


def test_from_to_passes_avoid_through_to_the_route():
    plain = server.from_to(
        from_={"lat": FROM_LAT, "lon": FROM_LON},
        to={"lat": TO_LAT, "lon": TO_LON},
        mode="drive",
        confirm=True,
    )
    avoided = server.from_to(
        from_={"lat": FROM_LAT, "lon": FROM_LON},
        to={"lat": TO_LAT, "lon": TO_LON},
        mode="drive",
        avoid=["motorway"],
        confirm=True,
    )
    assert "error" not in avoided
    assert avoided["avoid"] == ["motorway"]
    assert avoided["distance_m"] > plain["distance_m"]


def test_from_to_invalid_avoid_is_bad_request():
    result = server.from_to(
        from_={"lat": FROM_LAT, "lon": FROM_LON},
        to={"lat": TO_LAT, "lon": TO_LON},
        mode="drive",
        avoid=["tolls"],
    )
    assert result["error"] == "bad_request"
    assert result["supported"] == ["motorway", "trunk"]


# --- published schema (#419's assertion must keep passing) ----------------


@pytest.mark.parametrize("tool_name", ["route", "from_to"])
def test_published_schema_shows_avoid_with_its_enum(tool_name):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools[tool_name].input_schema["properties"]
    fn = getattr(server, tool_name)
    accepted = {"from" if name == "from_" else name for name in inspect.signature(fn).parameters}
    assert set(props) == accepted  # the #328/#395/#419 signature-vs-schema rule
    assert props["avoid"]["items"]["enum"] == ["motorway", "trunk"]
    assert "toll" in props["avoid"]["description"]


@pytest.mark.parametrize("tool_name", ["route", "from_to"])
def test_published_validator_passes_avoid_through(tool_name):
    tool = server.mcp._tool_manager.get_tool(tool_name)
    kwargs = tool.fn_metadata.validate_arguments(
        {"from": "A", "to": "B", "mode": "drive", "avoid": ["motorway"]}
    )
    assert kwargs["avoid"] == ["motorway"]
