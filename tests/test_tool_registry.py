"""Every tool-shaped function in server.py must be registered over MCP,
and every registered tool must be annotated (issue #193).

Regression guard for the class of bug PR #60 found: server.isochrone
shipped without its tool decorator — callable as a plain function
(so every direct-call test passed) but absent from the MCP registry.
"""

import asyncio
import inspect

import pytest

from placeroot import server, tool_profiles

# Public module-level functions that are deliberately NOT MCP tools.
NON_TOOLS = {"main", "parse_transport_args", "build_server"}


def _intended_tool_names() -> set[str]:
    names = set()
    for name, obj in vars(server).items():
        if name.startswith("_") or name in NON_TOOLS:
            continue
        if not inspect.isfunction(obj):
            continue
        if obj.__module__ != "placeroot.server":
            continue
        names.add(name)
    return names


def test_every_tool_function_is_registered():
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    intended = _intended_tool_names()
    missing = intended - registered
    assert not missing, (
        f"tool functions defined in server.py but not registered over MCP "
        f"(missing @_tool?): {sorted(missing)}"
    )
    # And nothing registered that isn't a module function (drift the other way).
    unknown = registered - intended
    assert not unknown, f"registered tools with no matching server.py function: {sorted(unknown)}"


# What a PlaceRoot lookup tool claims about itself: a read, closed-world.
EXPECTED_HINTS = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": False,
}

# render_map is the one tool that is not a pure read: it writes a new
# timestamped HTML file on every call. readOnlyHint is what clients gate
# auto-approval on, so this must not claim the lookup hints.
WRITING_TOOLS = {
    "render_map": {
        "read_only_hint": False,
        "destructive_hint": False,
        "idempotent_hint": False,
        "open_world_hint": False,
    },
}


def _expected_hints(name: str) -> dict:
    return WRITING_TOOLS.get(name, EXPECTED_HINTS)


def test_every_registered_tool_is_annotated():
    """No tool may reach tools/list without the read-only hints.

    A new tool added with @_tool("...") gets these for free from
    build_server(); one added with a bare @mcp.tool() would not, and this
    is what catches that.
    """
    unannotated = [t.name for t in asyncio.run(server.mcp.list_tools()) if t.annotations is None]
    assert not unannotated, f"tools registered without annotations: {sorted(unannotated)}"


def test_every_registered_tool_declares_the_expected_hints():
    wrong = {}
    for tool in asyncio.run(server.mcp.list_tools()):
        actual = {k: getattr(tool.annotations, k) for k in EXPECTED_HINTS}
        if actual != _expected_hints(tool.name):
            wrong[tool.name] = actual
    assert not wrong, f"tools with unexpected annotation hints: {wrong}"


def test_render_map_is_not_advertised_as_read_only():
    """It writes a file; readOnlyHint drives client auto-approval.

    Asserted by name rather than only through the table above, because the
    failure this guards against is exactly someone folding render_map back
    into the read-only default and the table quietly following.
    """
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    annotations = tools["render_map"].annotations
    assert annotations.read_only_hint is False
    assert annotations.idempotent_hint is False, (
        "two identical render_map calls write two differently named files"
    )
    assert annotations.destructive_hint is False, "it only ever adds files"
    assert annotations.open_world_hint is False


def test_every_other_tool_is_still_read_only():
    """The exception stays an exception: the other 24 are pure lookups."""
    reads = [
        t.name
        for t in asyncio.run(server.mcp.list_tools())
        if t.name not in WRITING_TOOLS
    ]
    assert len(reads) == len(server._TOOL_FUNCS) - len(WRITING_TOOLS)
    not_read_only = [
        t.name
        for t in asyncio.run(server.mcp.list_tools())
        if t.name not in WRITING_TOOLS and t.annotations.read_only_hint is not True
    ]
    assert not not_read_only, f"lookup tools that lost readOnlyHint: {not_read_only}"


def test_annotation_overrides_are_declared_for_real_tools():
    """_TOOL_ANNOTATIONS can't drift from _TOOL_FUNCS, or from this test."""
    assert set(server._TOOL_ANNOTATIONS) == set(WRITING_TOOLS)
    assert set(server._TOOL_ANNOTATIONS) <= set(server._TOOL_FUNCS)


def test_every_registered_tool_has_a_nonempty_unique_title():
    tools = asyncio.run(server.mcp.list_tools())
    untitled = [t.name for t in tools if not (t.title or "").strip()]
    assert not untitled, f"tools without a display title: {sorted(untitled)}"
    titles = [t.title for t in tools]
    dupes = sorted({t for t in titles if titles.count(t) > 1})
    assert not dupes, f"display titles must be unique, these are shared: {dupes}"
    # A title is a short human name, not a restatement of the description.
    too_long = {t.name: t.title for t in tools if len(t.title) > 40}
    assert not too_long, f"display titles should be short human names: {too_long}"


def test_titles_are_defined_for_exactly_the_marked_tools():
    """_TOOL_TITLES can't drift from _TOOL_FUNCS — build_server() indexes it
    by name, so a missing entry would be a KeyError at import.
    """
    assert set(server._TOOL_TITLES) == set(server._TOOL_FUNCS)


@pytest.mark.parametrize("profile", sorted(tool_profiles.PROFILES))
def test_subset_profiles_are_annotated_too(profile):
    """Annotations are applied at registration, so every PLACEROOT_TOOLS
    subset must carry them exactly like the full surface (issue #182+#193).
    """
    tools = asyncio.run(server.build_server(profile).list_tools())
    assert tools, f"profile {profile} registered no tools"
    for tool in tools:
        assert tool.annotations is not None, f"{profile}/{tool.name} is unannotated"
        actual = {k: getattr(tool.annotations, k) for k in EXPECTED_HINTS}
        assert actual == _expected_hints(tool.name), (
            f"{profile}/{tool.name} is annotated differently under a subset "
            f"than on the full surface: {actual}"
        )
        assert (tool.title or "").strip(), f"{profile}/{tool.name} lost its title"


def test_tool_decorator_requires_a_title():
    """The mechanism itself: @_tool without a title is a loud failure, not a
    silently untitled tool. (Guards the "new tool ships unannotated" case at
    import time, before any test can even run.)
    """
    with pytest.raises(TypeError):
        server._tool(lambda: None)  # bare use, as if @_tool with no argument
