"""Guard: docs/REFERENCE.md's tool catalog stays in sync with the registered
MCP tools (issue #288).

docs/REFERENCE.md hardcodes a markdown table under "## All N tools" (one row
per tool, first column a backticked name). Nothing ties those rows to the
tools actually registered in server.py, so a tool add/remove/rename can
silently leave the catalog stale — the same staleness class
test_site_tools_sync.py already guards for the marketing site, and the exact
hole issue #277 hit (a doc row naming a tool that does not exist).

This reuses the tool-introspection approach from test_tool_registry.py
(asyncio.run(server.mcp.list_tools())) as the source of truth.
"""

import asyncio
import re
from pathlib import Path

from placeroot import server

DOCS_DIR = Path(__file__).parent.parent / "docs"
REFERENCE_MD = DOCS_DIR / "REFERENCE.md"

# Catalog heading near the top of REFERENCE.md, e.g. "## All 29 tools".
_HEADING_RE = re.compile(r"^## All (\d+) tools\s*$", re.MULTILINE)

# First-column tool names: | `find_places` | ...
# Identifiers only — prompt-table rows (`/mcp__placeroot__...`) do not match.
_TOOL_ROW_RE = re.compile(r"^\|\s*`([a-zA-Z_][a-zA-Z0-9_]*)`\s*\|", re.MULTILINE)

# The catalog table opener; used to locate the right table inside the section.
_TABLE_HEADER_RE = re.compile(
    r"^\|\s*Tool\s*\|\s*Answers\s*\|\s*\n\|[-:| ]+\|\s*\n",
    re.MULTILINE,
)


def _registered_tool_names() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def _catalog_section() -> str:
    doc = REFERENCE_MD.read_text(encoding="utf-8")
    heading = _HEADING_RE.search(doc)
    assert heading, "docs/REFERENCE.md: couldn't locate the '## All N tools' heading"
    start = heading.end()
    next_heading = re.search(r"^## ", doc[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(doc)
    return doc[start:end]


def _catalog_tool_names() -> list[str]:
    section = _catalog_section()
    table = _TABLE_HEADER_RE.search(section)
    assert table, (
        "docs/REFERENCE.md: couldn't locate the '| Tool | Answers |' "
        "catalog table under '## All N tools'"
    )
    # First-column backticks only; ignore batch siblings mentioned inline in
    # the Answers column, and never walk later tables (prompts, profiles).
    return _TOOL_ROW_RE.findall(section[table.end() :])


def _heading_tool_count() -> int | None:
    doc = REFERENCE_MD.read_text(encoding="utf-8")
    match = _HEADING_RE.search(doc)
    if not match:
        return None
    return int(match.group(1))


def test_catalog_names_are_all_real_registered_tools():
    catalog_tools = _catalog_tool_names()
    assert catalog_tools, "docs/REFERENCE.md: no tool names found in the catalog table"
    registered = _registered_tool_names()

    catalog_set = set(catalog_tools)
    in_catalog_not_registered = catalog_set - registered
    assert not in_catalog_not_registered, (
        "docs/REFERENCE.md lists tools that are not registered MCP tools in "
        f"server.py (renamed or removed?): {sorted(in_catalog_not_registered)}. "
        f"Currently registered tools: {sorted(registered)}"
    )


def test_catalog_shows_every_registered_tool():
    catalog_set = set(_catalog_tool_names())
    registered = _registered_tool_names()

    missing_from_catalog = registered - catalog_set
    assert not missing_from_catalog, (
        "server.py registers tools that are missing from docs/REFERENCE.md's "
        f"catalog table: {sorted(missing_from_catalog)}. Add a row for each "
        "(a mention in another row's Answers column does not count)."
    )


def test_catalog_has_no_duplicate_entries():
    catalog_tools = _catalog_tool_names()
    seen = set()
    dupes = {t for t in catalog_tools if t in seen or seen.add(t)}
    assert not dupes, (
        f"docs/REFERENCE.md: duplicate tool entries in the catalog table: {sorted(dupes)}"
    )


def test_heading_tool_count_matches_catalog():
    claimed = _heading_tool_count()
    if claimed is None:
        return
    shown = len(_catalog_tool_names())
    registered = len(_registered_tool_names())
    assert claimed == shown, (
        f"docs/REFERENCE.md: heading claims {claimed} tools but the catalog "
        f"table actually has {shown} rows. Update the heading (or the table) "
        "so they match."
    )
    assert claimed == registered, (
        f"docs/REFERENCE.md: heading claims {claimed} tools but server.py "
        f"registers {registered}. Update the heading so it matches."
    )
