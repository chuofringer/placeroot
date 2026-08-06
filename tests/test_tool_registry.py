"""Every tool-shaped function in server.py must be registered over MCP.

Regression guard for the class of bug PR #60 found: server.isochrone
shipped without its @mcp.tool() decorator — callable as a plain function
(so every direct-call test passed) but absent from the MCP registry.
"""

import asyncio
import inspect

from placeroot import server

# Public module-level functions that are deliberately NOT MCP tools.
NON_TOOLS = {"main", "parse_transport_args"}


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
        f"(missing @mcp.tool()?): {sorted(missing)}"
    )
    # And nothing registered that isn't a module function (drift the other way).
    unknown = registered - intended
    assert not unknown, f"registered tools with no matching server.py function: {sorted(unknown)}"
