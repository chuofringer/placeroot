"""Bump the release version everywhere it is claimed, in one command.

A release version lives in four places that must agree, and until now each
was edited by hand:

  - pyproject.toml            the Python package version
  - npm/package.json          the launcher package, published as a pair
  - site/index.html           the developer-section chip + footer byline
  - site/add-to-your-ai.html  the "latest release" note

`tests/test_site_version_sync.py` fails when they disagree, which catches the
mistake but still leaves a human to make four edits. This script makes them
one edit, so the site ships with the release instead of trailing it.

It also fills the site's "new in this release" chip. By default the tool
names are derived — the MCP tools registered in the working tree, minus the
ones already registered at the previous release tag — so a release that adds
tools advertises them without anyone remembering to. `--highlight` overrides
that with free-text copy when a release's headline isn't a new tool.

Usage:
    uv run python scripts/bump_version.py 0.6.0
    uv run python scripts/bump_version.py 0.6.0 --new-tools route,land_use_at
    uv run python scripts/bump_version.py 0.6.0 --highlight "faster tile cache"
    uv run python scripts/bump_version.py 0.6.0 --since-tag v0.4.1
    uv run python scripts/bump_version.py 0.6.0 --dry-run

Deliberately NOT a pytest test_*.py file: it mutates the tree. Its pure
rewrite/derive helpers are unit-tested by tests/test_bump_version.py.
"""

import argparse
import asyncio
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PYPROJECT = REPO_ROOT / "pyproject.toml"
NPM_PACKAGE = REPO_ROOT / "npm" / "package.json"
# Registry distribution manifests (#172): both restate the package version and
# are guarded by tests/test_registry_manifests.py, so a bump that skips them
# fails the verify step.
SERVER_JSON = REPO_ROOT / "server.json"
MCPB_MANIFEST = REPO_ROOT / "mcpb" / "manifest.json"
INDEX_HTML = REPO_ROOT / "site" / "index.html"
SITE_PAGES = [
    INDEX_HTML,
    REPO_ROOT / "site" / "add-to-your-ai.html",
    REPO_ROOT / "site" / "how-it-works.html",
]

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# `version = "X.Y.Z"` in pyproject's [project] table — the first such line in
# the file (tool tables that carry versions come later).
_PYPROJECT_VERSION_RE = re.compile(r'(?m)^(version\s*=\s*")(\d+\.\d+\.\d+)(")')
_NPM_VERSION_RE = re.compile(r'("version"\s*:\s*")(\d+\.\d+\.\d+)(")')

# Any `vX.Y.Z` shown on a site page. Every one of them names the current
# release, which is exactly what test_site_version_sync.py asserts.
_SITE_VERSION_RE = re.compile(r"\bv(\d+\.\d+\.\d+)\b")

# The developer-section chip body, between the label and the enclosing span.
_CHIP_RE = re.compile(r"(new in this release: )(?P<body>.*?)(</span></div>)", re.DOTALL)
_CHIP_TOOL_SPAN = '<span style="color:#8fbf96">{name}</span>'

# Tool-decorated function names in a server.py source text. Used to read the
# tool surface of an older revision (which can't be imported), while the
# current surface comes from real MCP introspection. Matches both decorator
# spellings: `@mcp.tool()` (through v0.5.0) and `@_tool` (the profile-aware
# registration that replaced it in #182).
_REGISTERED_AT_REV_RE = re.compile(
    r"@(?:mcp\.tool\(\)|_tool(?:\([^\n]*\))?)\s*\ndef\s+([a-zA-Z0-9_]+)\s*\("
)

# Shown when a release adds no tools and the caller passed no --highlight.
DEFAULT_HIGHLIGHT = "correctness &amp; performance fixes"


class BumpError(RuntimeError):
    """A precondition failed; the tree is left untouched."""


def read_pyproject_version(text: str) -> str:
    match = _PYPROJECT_VERSION_RE.search(text)
    if not match:
        raise BumpError("pyproject.toml: no `version = \"X.Y.Z\"` line found")
    return match.group(2)


def read_npm_version(text: str) -> str:
    match = _NPM_VERSION_RE.search(text)
    if not match:
        raise BumpError('npm/package.json: no `"version": "X.Y.Z"` found')
    return match.group(2)


def bump_pyproject(text: str, new_version: str) -> str:
    return _PYPROJECT_VERSION_RE.sub(rf"\g<1>{new_version}\g<3>", text, count=1)


def bump_npm(text: str, new_version: str) -> str:
    return _NPM_VERSION_RE.sub(rf"\g<1>{new_version}\g<3>", text, count=1)


def bump_manifest(text: str, old_version: str, new_version: str) -> str:
    """Bump every `"version": "<old>"` in a registry manifest's text.

    Textual rather than json.load/dump so the files' existing formatting
    survives. server.json carries the version once at the top level and once
    per packages[] entry; mcpb/manifest.json once. Only exact matches of the
    current version are touched, so fields like `manifest_version` (or a
    pinned dependency that happens to carry a semver) are left alone.
    """
    return text.replace(f'"version": "{old_version}"', f'"version": "{new_version}"')


def bump_site_versions(text: str, new_version: str) -> str:
    """Rewrite every vX.Y.Z on a page to the new version.

    Blunt on purpose: the guard asserts *no* page mentions a version other
    than the current one, so "replace them all" and "replace the right ones"
    are the same operation.
    """
    return _SITE_VERSION_RE.sub(f"v{new_version}", text)


def render_chip_body(new_tools: list[str], highlight: str | None) -> str:
    """The chip's inner HTML: tool names, or free text, or the fallback."""
    if highlight:
        return highlight
    if not new_tools:
        return DEFAULT_HIGHLIGHT
    return " · ".join(_CHIP_TOOL_SPAN.format(name=name) for name in new_tools)


def set_chip_body(index_text: str, body: str) -> str:
    if not _CHIP_RE.search(index_text):
        raise BumpError(
            "site/index.html: the 'new in this release' chip is missing — the "
            "markup changed; update _CHIP_RE in this script (and the guard in "
            "tests/test_site_version_sync.py) to match."
        )
    return _CHIP_RE.sub(lambda m: f"{m.group(1)}{body}{m.group(3)}", index_text, count=1)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BumpError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def latest_release_tag() -> str | None:
    """Newest vX.Y.Z tag, or None when the repo has no release tags yet."""
    out = _git("tag", "--list", "v*", "--sort=-v:refname")
    tags = [line.strip() for line in out.splitlines() if line.strip()]
    return tags[0] if tags else None


def tools_registered_at(tag: str) -> set[str]:
    """Tool names registered in server.py as of `tag`.

    Read by parsing that revision's source rather than importing it — an old
    revision can't be imported into this process, and the decorator list is
    an unambiguous, stable thing to parse.
    """
    source = _git("show", f"{tag}:src/placeroot/server.py")
    return set(_REGISTERED_AT_REV_RE.findall(source))


def tools_registered_now() -> set[str]:
    """Tool names the working tree actually registers (real introspection)."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from placeroot import server  # imported late: needs the path above

    return {tool.name for tool in asyncio.run(server.mcp.list_tools())}


def derive_new_tools(since_tag: str | None) -> list[str]:
    """Tools registered now but not at `since_tag`, sorted alphabetically."""
    if since_tag is None:
        return []
    previous = tools_registered_at(since_tag)
    current = tools_registered_now()
    return sorted(current - previous)


def plan_changes(
    new_version: str,
    new_tools: list[str],
    highlight: str | None,
) -> tuple[dict[Path, str], str]:
    """Compute every file's new text without writing anything."""
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    npm_text = NPM_PACKAGE.read_text(encoding="utf-8")

    old_version = read_pyproject_version(pyproject_text)
    npm_version = read_npm_version(npm_text)
    if npm_version != old_version:
        raise BumpError(
            f"pyproject.toml is at {old_version} but npm/package.json is at "
            f"{npm_version}; reconcile them before bumping."
        )
    if new_version == old_version:
        raise BumpError(f"already at {new_version} — nothing to bump.")

    updates: dict[Path, str] = {
        PYPROJECT: bump_pyproject(pyproject_text, new_version),
        NPM_PACKAGE: bump_npm(npm_text, new_version),
    }
    for manifest in (SERVER_JSON, MCPB_MANIFEST):
        updates[manifest] = bump_manifest(
            manifest.read_text(encoding="utf-8"), old_version, new_version
        )

    chip_body = render_chip_body(new_tools, highlight)
    for page in SITE_PAGES:
        text = bump_site_versions(page.read_text(encoding="utf-8"), new_version)
        if page == INDEX_HTML:
            text = set_chip_body(text, chip_body)
        updates[page] = text

    return updates, old_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("version", help="the new version, e.g. 0.6.0")
    parser.add_argument(
        "--new-tools",
        help="comma-separated tool names for the site chip (default: derived "
        "from the diff against the previous release tag)",
    )
    parser.add_argument(
        "--highlight",
        help="free-text chip copy, for a release whose headline isn't a new "
        "tool; overrides --new-tools",
    )
    parser.add_argument(
        "--since-tag",
        help="baseline tag for deriving new tools (default: the newest vX.Y.Z tag)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    args = parser.parse_args(argv)

    if not _SEMVER_RE.match(args.version):
        parser.error(f"version must look like X.Y.Z, got {args.version!r}")

    try:
        if args.highlight:
            new_tools: list[str] = []
        elif args.new_tools:
            new_tools = [t.strip() for t in args.new_tools.split(",") if t.strip()]
        else:
            since = args.since_tag or latest_release_tag()
            new_tools = derive_new_tools(since)
            print(f"Deriving new tools against {since or '(no release tag yet)'}")

        updates, old_version = plan_changes(args.version, new_tools, args.highlight)
    except BumpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    chip = args.highlight or (", ".join(new_tools) if new_tools else DEFAULT_HIGHLIGHT)
    print(f"{old_version} -> {args.version}")
    print(f"site chip: {chip}")

    for path, text in updates.items():
        rel = path.relative_to(REPO_ROOT)
        if path.read_text(encoding="utf-8") == text:
            print(f"  unchanged  {rel}")
            continue
        if args.dry_run:
            print(f"  would edit {rel}")
        else:
            path.write_text(text, encoding="utf-8")
            print(f"  updated    {rel}")

    if args.dry_run:
        print("\nDry run — nothing written.")
    else:
        # uv.lock pins placeroot's own version, so it goes stale on a bump.
        # Refreshing it is left to the caller (the prepare-release workflow
        # runs `uv lock`) rather than shelling out to uv from here.
        print("\nNext:")
        print("  uv lock")
        print("  uv run pytest tests/test_site_version_sync.py tests/test_site_tools_sync.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
