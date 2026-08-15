"""Guard: the npm package's README stays in sync with the root one (issue #203).

`npm/README.md` is generated from `README.md` by scripts/sync_npm_readme.py —
the npm package is published from `npm/`, so it needs its own copy, and two
hand-written copies of the same tool table / profile tables / setup snippets
drift apart release by release.

This runs the generator's --check path, so editing README.md without
re-running the script fails CI rather than shipping a stale npm page. Same
shape as test_site_version_sync.py and test_site_tools_sync.py.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "sync_npm_readme.py"
NPM_README = ROOT / "npm" / "README.md"

sys.path.insert(0, str(ROOT / "scripts"))

from sync_npm_readme import SyncError, render  # noqa: E402


def _root_readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_npm_readme_is_up_to_date():
    """npm/README.md matches what the generator produces from README.md."""
    assert NPM_README.exists(), (
        "npm/README.md is missing — npmjs.com will show "
        '"ERROR: No README data found!" for the published package. '
        "Run: uv run python scripts/sync_npm_readme.py"
    )
    assert NPM_README.read_text(encoding="utf-8") == render(_root_readme()), (
        "npm/README.md is out of date with README.md. "
        "Run: uv run python scripts/sync_npm_readme.py"
    )


def test_check_flag_passes_on_committed_state():
    """The --check path CI would run agrees with the committed files."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_npm_readme_is_shipped_in_the_tarball():
    """`files` lists README.md, so it cannot be dropped from the package."""
    import json

    package = json.loads(
        (ROOT / "npm" / "package.json").read_text(encoding="utf-8")
    )
    assert "README.md" in package["files"], (
        "npm/package.json's `files` no longer lists README.md"
    )


def test_generated_readme_targets_npx_not_uvx():
    """The npm page's runnable snippets use the runner npm users have."""
    rendered = render(_root_readme())

    assert '"command": "npx"' in rendered
    assert "npx placeroot" in rendered

    # uvx survives in exactly three places, each of which is telling an npm
    # reader something true about the launcher rather than handing them a
    # command to run instead. Anything else is a leaked instruction.
    allowed_markers = (
        "thin wrapper",       # Quick start aside pointing at the Python server
        "spawns",             # launcher note under the intro
        "shells out",         # Requirements: the uv prerequisite
        "skips this launcher",  # Requirements: uvx as the equivalent path
    )
    for line in rendered.splitlines():
        if "uvx" in line:
            assert any(marker in line for marker in allowed_markers), (
                f"stray uvx instruction leaked into the npm README: {line!r}"
            )


def test_repo_facing_sections_are_dropped():
    """Development/Docs point at paths that do not exist inside the tarball."""
    rendered = render(_root_readme())
    assert "## Development" not in rendered
    assert "## Docs" not in rendered
    assert "uv run pytest" not in rendered


def test_no_repo_relative_links():
    """Relative links resolve on GitHub but 404 on npmjs.com.

    The rewrite in render() is what guarantees this; the test keeps it a
    tested invariant rather than a convention, across markdown links and
    raw HTML src/srcset/href alike.
    """
    import re

    rendered = render(_root_readme())
    relative = re.findall(r"\]\((?!https?://|#)([^)]+)\)", rendered)
    relative += re.findall(r'(?:src|srcset|href)="(?!https?://|#|mailto:)([^"]+)"', rendered)
    assert not relative, f"npm README has repo-relative links: {relative}"


def test_no_github_only_alert_markup():
    """GFM alerts ([!NOTE]) render as literal text on npmjs.com, so the
    generator downgrades them to a plain bold blockquote lead-in."""
    rendered = render(_root_readme())
    assert "[!NOTE]" not in rendered
    assert "> **Note:**" in rendered


def test_links_pin_the_release_tag_not_main():
    """Published package READMEs must point at the released version's docs,
    not at whatever main has become since — see the GITHUB_BLOB comment.
    The root README's hero banner is deliberately absolute-to-main (so
    mcpservers.org can render it); the rendered-output scan proves the
    re-pin rewrite catches it and anything like it added later."""
    import json

    from sync_npm_readme import GITHUB_BLOB, GITHUB_RAW

    version = json.loads(
        (ROOT / "npm" / "package.json").read_text(encoding="utf-8")
    )["version"]
    assert f"/blob/v{version}/" in GITHUB_BLOB
    assert f"/v{version}/" in GITHUB_RAW
    assert "/main/" not in GITHUB_BLOB and "/main/" not in GITHUB_RAW
    rendered = render(_root_readme())
    assert f"/blob/v{version}/" in rendered
    assert "chuofringer/placeroot/main/" not in rendered, (
        "an unpinned main-branch URL survived into the rendered npm README"
    )


def test_rewrites_match_the_pypi_substitutions():
    """pyproject.toml's fancy-pypi-readme substitutions are the PyPI copy of
    the script's REWRITES; if either side is edited alone, PyPI and npm
    render the same README differently. Patterns must be character-identical
    and replacements identical up to the version token ($HFPR_VERSION on the
    PyPI side, npm/package.json's version here)."""
    import tomllib

    from sync_npm_readme import _NPM_VERSION, REWRITES

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    substitutions = pyproject["tool"]["hatch"]["metadata"]["hooks"][
        "fancy-pypi-readme"
    ]["substitutions"]
    pypi_pairs = [(s["pattern"], s["replacement"]) for s in substitutions]
    script_pairs = [
        (pattern, replacement.replace(f"v{_NPM_VERSION}", "v$HFPR_VERSION"))
        for pattern, replacement in REWRITES
    ]
    assert script_pairs == pypi_pairs, (
        "scripts/sync_npm_readme.py REWRITES and pyproject.toml's "
        "fancy-pypi-readme substitutions have drifted apart"
    )


def test_generator_fails_loudly_on_a_renamed_section():
    """A dropped/renamed inherited section is an error, not a silent omission."""
    mangled = _root_readme().replace("## Quick start", "## Getting started")
    with pytest.raises(SyncError, match="missing sections"):
        render(mangled)


def test_generator_fails_loudly_on_a_reworded_launcher_aside():
    """The aside this generator rewrites must still be findable."""
    mangled = _root_readme().replace(
        "Prefer npm? Use", "npm works too — use"
    )
    with pytest.raises(SyncError, match="npm-launcher aside"):
        render(mangled)
