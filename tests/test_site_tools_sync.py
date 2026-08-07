"""Guard: the marketing site's tool list stays in sync with the registered
MCP tools (issue #94).

site/index.html hardcodes the developer-section tool list (the green mono
`tool_name` entries in the `pr-toolgrid` grid) plus a human-readable count
claim ("... Fourteen tools." / "2 · CALL — fourteen tools"). Nothing ties
either of those to the tools actually registered in server.py, so a tool
add/remove/rename can silently leave the site stale — the "6 vs 14"
staleness bug this guard exists to prevent from recurring.

This reuses the tool-introspection approach from test_tool_registry.py
(asyncio.run(server.mcp.list_tools())) as the source of truth.
"""

import asyncio
import re
from pathlib import Path

from placeroot import server

SITE_DIR = Path(__file__).parent.parent / "site"
INDEX_HTML = SITE_DIR / "index.html"

# Tool-name entries in the pr-toolgrid: green mono divs of the form
# <div style="font:600 12px 'Fira Code',monospace;color:#8fbf96">NAME</div>
_TOOLGRID_BLOCK_RE = re.compile(
    r'<div class="pr-toolgrid"[^>]*>(?P<body>.*?)</div>\s*</div>\s*</div>',
    re.DOTALL,
)
_TOOL_NAME_RE = re.compile(
    r'<div style="font:600 12px \'Fira Code\',monospace;color:#8fbf96">'
    r"(?P<name>[a-zA-Z0-9_]+)</div>"
)

# Spelled-out counts the page uses for its headline tool-count claims.
# Minimal word<->number map: only covers counts this site has plausibly
# shown (small single-digit-to-teens counts for a tool list). Extend if
# the tool count grows past "twenty".
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty-one": 21,
}

# Headline copy that claims a tool count, and the regex to pull the count
# word/digits out of it. Both currently say "fourteen"; kept as separate
# patterns since they're independent bits of copy that could drift apart.
_COUNT_CLAIM_PATTERNS = [
    re.compile(r"One line\. No keys\. (?P<count>\w+) tools\.", re.IGNORECASE),
    re.compile(r"2 · CALL — (?P<count>\w+) tools", re.IGNORECASE),
]


def _registered_tool_names() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def _site_tool_names() -> list[str]:
    doc = INDEX_HTML.read_text(encoding="utf-8")
    match = _TOOLGRID_BLOCK_RE.search(doc)
    assert match, "index.html: couldn't locate the pr-toolgrid block at all"
    return _TOOL_NAME_RE.findall(match.group("body"))


def _claimed_counts() -> list[int]:
    doc = INDEX_HTML.read_text(encoding="utf-8")
    counts = []
    for pattern in _COUNT_CLAIM_PATTERNS:
        m = pattern.search(doc)
        assert m, f"index.html: expected count-claim copy matching {pattern.pattern!r} not found"
        word = m.group("count").strip().lower()
        if word.isdigit():
            counts.append(int(word))
        else:
            assert word in _NUMBER_WORDS, (
                f"index.html: count word {word!r} not in the word->number map "
                f"({sorted(_NUMBER_WORDS)}); add it if the site started using it"
            )
            counts.append(_NUMBER_WORDS[word])
    return counts


def test_toolgrid_names_are_all_real_registered_tools():
    site_tools = _site_tool_names()
    assert site_tools, "index.html: no tool names found in the pr-toolgrid block"
    registered = _registered_tool_names()

    site_set = set(site_tools)
    on_site_not_registered = site_set - registered
    assert not on_site_not_registered, (
        "index.html lists tools that are not registered MCP tools in "
        f"server.py (renamed or removed?): {sorted(on_site_not_registered)}. "
        f"Currently registered tools: {sorted(registered)}"
    )


def test_toolgrid_shows_every_registered_tool():
    # The site currently shows the full tool list (not a curated subset).
    # If a curated subset becomes intentional, relax this to a subset
    # assertion — the count-matches-shown check below still guards drift.
    site_set = set(_site_tool_names())
    registered = _registered_tool_names()

    missing_from_site = registered - site_set
    assert not missing_from_site, (
        "server.py registers tools that are missing from index.html's "
        f"pr-toolgrid: {sorted(missing_from_site)}. Add them to the "
        "developer-section tool list on the marketing site."
    )


def test_toolgrid_has_no_duplicate_entries():
    site_tools = _site_tool_names()
    seen = set()
    dupes = {t for t in site_tools if t in seen or seen.add(t)}
    assert not dupes, f"index.html: duplicate tool entries in pr-toolgrid: {sorted(dupes)}"


def test_headline_tool_count_matches_tools_shown_on_page():
    shown = len(_site_tool_names())
    for claimed in _claimed_counts():
        assert claimed == shown, (
            f"index.html: headline claims {claimed} tools but the "
            f"pr-toolgrid actually shows {shown} tool entries. Update the "
            "copy (or the grid) so they match."
        )
