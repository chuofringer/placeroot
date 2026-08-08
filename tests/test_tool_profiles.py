"""PLACEROOT_TOOLS subset profiles (issue #182).

The point of the feature is that an unselected tool is never registered, so
every assertion here goes through the MCP server's own registry
(list_tools()) rather than the internal selection set — if filtering ever
degraded into post-hoc hiding, these would still be the tests that catch it.
"""

import asyncio

import pytest

from placeroot import server, tool_profiles


def _names(spec) -> set[str]:
    return {t.name for t in asyncio.run(server.build_server(spec).list_tools())}


def _all_names() -> set[str]:
    return set(server._TOOL_FUNCS)


def test_unset_registers_every_tool():
    assert _names(None) == _all_names()


def test_module_level_server_is_the_full_surface_by_default():
    """Production import path: no env var set, nothing filtered."""
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert registered == _all_names()


@pytest.mark.parametrize("spec", ["all", "", "   ", ",,"])
def test_all_and_empty_specs_register_every_tool(spec):
    assert _names(spec) == _all_names()


def test_all_wins_over_other_entries():
    assert _names("core,all") == _all_names()


def test_core_profile():
    assert _names("core") == {
        "find_places",
        "geocode",
        "reverse_geocode",
        "place_details",
        "resolve_place",
        "summarize_area",
        "route",
        "data_version",
    }


@pytest.mark.parametrize("profile", sorted(tool_profiles.PROFILES))
def test_each_profile_registers_exactly_its_members_plus_always_included(profile):
    assert _names(profile) == set(tool_profiles.PROFILES[profile]) | set(
        tool_profiles.ALWAYS_INCLUDED
    )


def test_explicit_tool_list():
    assert _names("find_places,geocode,route") == {
        "find_places",
        "geocode",
        "route",
        "data_version",
    }


def test_mixed_profile_and_tool_list_unions():
    assert _names("routing,find_places") == (
        set(tool_profiles.PROFILES["routing"]) | {"find_places", "data_version"}
    )


def test_overlapping_profiles_union_without_duplicating():
    """core and search share tools; the union is a set, and list_tools has no dupes."""
    tools = asyncio.run(server.build_server("core,search").list_tools())
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    assert set(names) == (
        set(tool_profiles.PROFILES["core"])
        | set(tool_profiles.PROFILES["search"])
        | set(tool_profiles.ALWAYS_INCLUDED)
    )


def test_entries_are_case_and_whitespace_insensitive():
    assert _names(" Core , ROUTE ") == _names("core,route")


def test_data_version_is_always_included():
    """Even a selection that names only one unrelated tool keeps it."""
    for spec in ("geometry", "simplify_geometry", "routing"):
        assert "data_version" in _names(spec)


def test_unknown_name_fails_fast_with_the_valid_names():
    with pytest.raises(tool_profiles.InvalidToolSelection) as excinfo:
        server.build_server("core,rooting")
    message = str(excinfo.value)
    assert "rooting" in message
    # Both halves of the vocabulary, so the operator can see which they meant.
    for valid in ("core", "search", "routing", "analysis", "geometry", "all"):
        assert valid in message
    assert "find_places" in message


def test_unknown_name_reports_every_bad_entry_at_once():
    with pytest.raises(tool_profiles.InvalidToolSelection) as excinfo:
        server.build_server("nope,core,alsonope")
    message = str(excinfo.value)
    assert "alsonope" in message and "nope" in message


def test_env_var_drives_the_selection(monkeypatch):
    monkeypatch.setenv("PLACEROOT_TOOLS", "geometry")
    registered = {t.name for t in asyncio.run(server.build_server().list_tools())}
    assert registered == {"simplify_geometry", "render_map", "data_version"}


def test_env_var_unset_is_the_full_surface(monkeypatch):
    monkeypatch.delenv("PLACEROOT_TOOLS", raising=False)
    assert {t.name for t in asyncio.run(server.build_server().list_tools())} == _all_names()


def test_profiles_cover_every_tool():
    """A tool belonging to no profile would be unreachable except by name."""
    covered = set(tool_profiles.ALWAYS_INCLUDED)
    for members in tool_profiles.PROFILES.values():
        covered |= set(members)
    assert covered == _all_names()


def test_profiles_name_no_tool_that_does_not_exist():
    for name, members in tool_profiles.PROFILES.items():
        unknown = set(members) - _all_names()
        assert not unknown, f"profile {name!r} names nonexistent tool(s): {sorted(unknown)}"


def test_a_subset_server_still_calls_the_tools_it_kept():
    """Registration-time filtering doesn't change what a kept tool does."""
    tools = asyncio.run(server.build_server("core").list_tools())
    assert {t.name for t in tools} >= {"data_version"}
    result = asyncio.run(server.build_server("core").call_tool("data_version", {}))
    assert result is not None
