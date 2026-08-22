"""PLACEROOT_TOOLS subset profiles (issue #182).

The point of the feature is that an unselected tool is never registered, so
every assertion here goes through the MCP server's own registry
(list_tools()) rather than the internal selection set — if filtering ever
degraded into post-hoc hiding, these would still be the tests that catch it.
"""

import asyncio
import logging
import re

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


def test_all_does_not_excuse_an_unknown_name():
    """"all,typo" must fail like "typo", not silently load everything."""
    with pytest.raises(tool_profiles.InvalidToolSelection) as excinfo:
        server.build_server("all,typo")
    assert "typo" in str(excinfo.value)


def test_core_profile():
    assert _names("core") == {
        "find_places",
        "geocode",
        "reverse_geocode",
        "place_details",
        "resolve_place",
        "search_categories",
        "summarize_area",
        "route",
        "places_along_route",
        "neighborhood_verdict",
        "warmup_city",
        "from_to",
        "find_near",
        "ground_location",
        "data_version",
        "preferences",
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
        "preferences",
    }


def test_mixed_profile_and_tool_list_unions():
    assert _names("routing,find_places") == (
        set(tool_profiles.PROFILES["routing"]) | {"find_places", "data_version", "preferences"}
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
        assert "preferences" in _names(spec)


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
    assert registered == {"simplify_geometry", "render_map", "data_version", "preferences"}


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


def test_a_typo_inside_a_profile_definition_fails_at_build(monkeypatch):
    """The inverse of the coverage test: a bad name in PROFILES must not be silent.

    Before this, `selected |= PROFILES[entry]` trusted the definition, so a
    typo just dropped that tool from the profile with nothing to notice —
    unlike a typo in the env var, which fails loudly.
    """
    monkeypatch.setitem(
        tool_profiles.PROFILES, "core", frozenset({"find_places", "find_playces"})
    )
    with pytest.raises(tool_profiles.InvalidProfileDefinition) as excinfo:
        server.build_server("core")
    assert "find_playces" in str(excinfo.value)


def test_a_typo_inside_an_unselected_profile_still_fails(monkeypatch):
    """Checked on every build, including the default one, not just when selected."""
    monkeypatch.setitem(tool_profiles.PROFILES, "geometry", frozenset({"render_maps"}))
    with pytest.raises(tool_profiles.InvalidProfileDefinition):
        server.build_server(None)


def test_build_server_logs_the_active_selection(caplog):
    with caplog.at_level(logging.INFO, logger="placeroot.server"):
        server.build_server("core")
    assert "PLACEROOT_TOOLS=core" in caplog.text
    registered = len(tool_profiles.PROFILES["core"]) + len(
        tool_profiles.ALWAYS_INCLUDED
    )
    assert f"registered {registered} of" in caplog.text


@pytest.mark.parametrize("spec", [None, "", "   "])
def test_the_full_surface_is_logged_as_all(spec, caplog):
    """Empty and whitespace specs load everything; the log has to say so."""
    with caplog.at_level(logging.INFO, logger="placeroot.server"):
        server.build_server(spec)
    assert f"registered {len(_all_names())} of {len(_all_names())} tools" in caplog.text
    assert "PLACEROOT_TOOLS=all" in caplog.text


def test_no_profile_describes_a_tool_it_does_not_register():
    """A tool's description must not name a tool the agent cannot see.

    Under a subset, an instruction like "check the slug with
    search_categories" points at nothing — the agent's recovery path from a
    zero-result answer dead-ends. Fix a hit either by adding the named tool
    to the profile or by describing the other tool by what it does instead
    of by name; both are legitimate, and which one depends on whether the
    reference is a next step or just context.

    The match is on tool names as whole words, so a description that uses
    "route" or "geocode" as ordinary English will trip this. That's the
    intended sensitivity: rephrase rather than exempt.

    A prohibition ("Do not call geocode(), resolve_place(), or
    geocode_batch() first.") is not a next-step pointer, so those clauses
    are stripped before the check.
    """
    all_names = _all_names()
    do_not_call = re.compile(r"do not call [^.]*\.?", re.IGNORECASE)
    dangling: list[str] = []
    for profile in sorted(tool_profiles.PROFILES):
        tools = asyncio.run(server.build_server(profile).list_tools())
        absent = all_names - {t.name for t in tools}
        for tool in tools:
            desc = do_not_call.sub("", tool.description or "")
            named = sorted(a for a in absent if re.search(rf"\b{a}\b", desc))
            if named:
                dangling.append(f"{profile}/{tool.name} -> {', '.join(named)}")
    assert not dangling, "descriptions naming unregistered tools:\n" + "\n".join(dangling)


def test_a_subset_server_still_calls_the_tools_it_kept():
    """Registration-time filtering doesn't change what a kept tool does."""
    tools = asyncio.run(server.build_server("core").list_tools())
    assert {t.name for t in tools} >= {"data_version"}
    result = asyncio.run(server.build_server("core").call_tool("data_version", {}))
    assert result is not None
