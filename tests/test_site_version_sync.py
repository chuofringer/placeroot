"""Guard: the marketing site's version claims stay in sync with the release.

The site now states which release it describes (a `v0.5.0` chip in the
developer section, the footer byline, and the installer page's "latest
release" note). Nothing ties that copy to the actual package version, so a
release could otherwise ship a site advertising the previous one — the same
staleness class `test_site_tools_sync.py` guards for the tool list.

Checks, all offline:
- `npm/package.json` matches `pyproject.toml` (they are published as a pair).
- Every `vX.Y.Z` version string on the site equals the package version.
- Each page that should carry the version actually does.
- The "new in this release" chip names real registered MCP tools.
"""

import asyncio
import json
import re
import tomllib
from pathlib import Path

from placeroot import server

ROOT = Path(__file__).parent.parent
SITE_DIR = ROOT / "site"
INDEX_HTML = SITE_DIR / "index.html"

# Pages that must display the current version, and the copy that carries it.
_VERSION_COPY = {
    "index.html": [
        re.compile(r'border-radius:99px;font-weight:600">v(?P<v>\d+\.\d+\.\d+)</span>'),
        re.compile(r"PlaceRoot v(?P<v>\d+\.\d+\.\d+) · MIT licensed"),
    ],
    "add-to-your-ai.html": [
        re.compile(r"latest release <span[^>]*>v(?P<v>\d+\.\d+\.\d+)</span>"),
    ],
}

# Any version-looking string anywhere on the site, to catch a stale mention
# in copy the patterns above don't know about.
_ANY_VERSION_RE = re.compile(r"\bv(\d+\.\d+\.\d+)\b")

# The developer section's "new in this release: tool · tool" chip.
_NEW_IN_RELEASE_RE = re.compile(r"new in this release:(?P<body>.*?)</div>", re.DOTALL)
_CHIP_TOOL_RE = re.compile(r'<span style="color:#8fbf96">(?P<name>[a-zA-Z0-9_]+)</span>')


def _package_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _registered_tool_names() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_npm_package_version_matches_pyproject():
    npm_version = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))["version"]
    assert npm_version == _package_version(), (
        f"npm/package.json says {npm_version} but pyproject.toml says "
        f"{_package_version()} — the two are published as a pair and must match."
    )


def test_site_states_the_current_version():
    expected = _package_version()
    for page, patterns in _VERSION_COPY.items():
        doc = (SITE_DIR / page).read_text(encoding="utf-8")
        for pattern in patterns:
            match = pattern.search(doc)
            assert match, (
                f"site/{page}: expected version copy matching {pattern.pattern!r} "
                "not found — did the markup change? Keep a visible version "
                "claim (and this guard) in place."
            )
            assert match.group("v") == expected, (
                f"site/{page} advertises v{match.group('v')} but the package "
                f"is {expected}. Bump the site copy as part of the release."
            )


def test_no_stale_version_strings_anywhere_on_the_site():
    expected = _package_version()
    for page in ("index.html", "how-it-works.html", "add-to-your-ai.html"):
        doc = (SITE_DIR / page).read_text(encoding="utf-8")
        stale = {v for v in _ANY_VERSION_RE.findall(doc) if v != expected}
        assert not stale, (
            f"site/{page} mentions version(s) {sorted(stale)} but the package "
            f"is {expected} — update every version claim on the page."
        )


def test_new_in_release_chip_names_registered_tools():
    doc = INDEX_HTML.read_text(encoding="utf-8")
    match = _NEW_IN_RELEASE_RE.search(doc)
    assert match, "index.html: the 'new in this release' chip is missing"

    named = _CHIP_TOOL_RE.findall(match.group("body"))
    assert named, "index.html: the 'new in this release' chip lists no tools"

    unknown = set(named) - _registered_tool_names()
    assert not unknown, (
        f"index.html's 'new in this release' chip names {sorted(unknown)}, "
        "which are not registered MCP tools in server.py."
    )
