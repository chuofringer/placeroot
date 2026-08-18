"""Guard the REFERENCE.md tool catalog against registered-tool drift.

The reference guide maintains a hand-written table of every MCP tool.  Keep
that table synchronized with the server registry so tool additions, removals,
and renames cannot leave the public reference stale (issue #288).

This mirrors ``test_site_tools_sync.py`` and uses the MCP server's tool list as
the source of truth.
"""

import asyncio
import re
from pathlib import Path

from placeroot import server

REFERENCE_MD = Path(__file__).parent.parent / "docs" / "REFERENCE.md"

_TOOL_CATALOG_RE = re.compile(
    r"^## All \d+ tools\s*$\n(?P<body>.*?)(?=^## |\Z)",
    re.DOTALL | re.MULTILINE,
)
_TOOL_ROW_RE = re.compile(
    r"^\|\s*`(?P<name>[a-zA-Z0-9_]+)`\s*\|",
    re.MULTILINE,
)


def _registered_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(server.mcp.list_tools())}


def _reference_tool_names() -> list[str]:
    document = REFERENCE_MD.read_text(encoding="utf-8")
    catalog = _TOOL_CATALOG_RE.search(document)
    assert catalog, "REFERENCE.md: couldn't locate the 'All N tools' catalog section"
    return _TOOL_ROW_RE.findall(catalog.group("body"))


def test_reference_catalog_names_are_all_registered_tools():
    reference_tools = _reference_tool_names()
    assert reference_tools, "REFERENCE.md: no tool rows found in the tool catalog"
    registered = _registered_tool_names()

    stale_rows = set(reference_tools) - registered
    assert not stale_rows, (
        "REFERENCE.md lists tools that are not registered MCP tools in "
        f"server.py (renamed or removed?): {sorted(stale_rows)}. "
        f"Currently registered tools: {sorted(registered)}"
    )


def test_reference_catalog_shows_every_registered_tool():
    reference_set = set(_reference_tool_names())
    registered = _registered_tool_names()

    missing_rows = registered - reference_set
    assert not missing_rows, (
        "server.py registers tools that are missing from REFERENCE.md's "
        f"tool catalog: {sorted(missing_rows)}. Add a row for each tool."
    )


def test_reference_catalog_has_no_duplicate_entries():
    reference_tools = _reference_tool_names()
    seen: set[str] = set()
    duplicates = {tool for tool in reference_tools if tool in seen or seen.add(tool)}
    assert not duplicates, (
        f"REFERENCE.md: duplicate tool rows in the catalog: {sorted(duplicates)}"
    )
