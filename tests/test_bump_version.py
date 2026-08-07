"""Offline tests for scripts/bump_version.py — the release version bumper.

The script rewrites pyproject.toml, npm/package.json and the site's version
copy in one shot, so that a release can't ship a website still advertising
the previous one. It is the thing standing between "cut a release" and "the
site is stale", which makes its rewrite helpers worth testing directly:
a silent no-op rewrite would leave a stale site that the sync guards then
fail on, mid-release.

These exercise the pure helpers (string in, string out) plus the end-to-end
plan against the real repo files — never writing to the tree.
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# scripts/ isn't a package (and bump_version.py is deliberately not importable
# as tests.*), so load it by path.
_spec = importlib.util.spec_from_file_location(
    "bump_version", REPO_ROOT / "scripts" / "bump_version.py"
)
bump_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_version)


PYPROJECT_SAMPLE = """[project]
name = "placeroot"
version = "0.5.0"
description = "Ground AI agents in open map data."

[tool.ruff]
line-length = 100
"""

NPM_SAMPLE = """{
  "name": "placeroot",
  "version": "0.5.0",
  "license": "MIT"
}
"""

CHIP_SAMPLE = (
    '<div class="pr-release" style="margin-top:12px">'
    '<span style="border-radius:99px;font-weight:600">v0.5.0</span>'
    "<span>new in this release: "
    '<span style="color:#8fbf96">route</span> · '
    '<span style="color:#8fbf96">land_use_at</span></span></div>'
)


def test_reads_the_current_versions():
    assert bump_version.read_pyproject_version(PYPROJECT_SAMPLE) == "0.5.0"
    assert bump_version.read_npm_version(NPM_SAMPLE) == "0.5.0"


def test_bumps_pyproject_project_version_only():
    out = bump_version.bump_pyproject(PYPROJECT_SAMPLE, "0.6.0")
    assert 'version = "0.6.0"' in out
    assert '"0.5.0"' not in out
    # Untouched neighbours: the bump must not disturb other tables.
    assert "line-length = 100" in out
    assert 'name = "placeroot"' in out


def test_bumps_npm_version():
    out = bump_version.bump_npm(NPM_SAMPLE, "0.6.0")
    assert '"version": "0.6.0"' in out
    assert '"license": "MIT"' in out


def test_bumps_every_version_string_on_a_page():
    page = "shows v0.5.0 here and v0.5.0 there, plus PlaceRoot v0.5.0 in the footer"
    out = bump_version.bump_site_versions(page, "0.6.0")
    assert "0.5.0" not in out
    assert out.count("v0.6.0") == 3


def test_site_bump_leaves_non_version_numbers_alone():
    page = "1.4 KB · 482 KB · ≤ 2K tokens · ~60× smaller · v0.5.0"
    out = bump_version.bump_site_versions(page, "0.6.0")
    assert "1.4 KB · 482 KB · ≤ 2K tokens · ~60× smaller · v0.6.0" == out


def test_chip_renders_tool_names_in_the_tool_style():
    body = bump_version.render_chip_body(["route", "land_use_at"], None)
    assert body == (
        '<span style="color:#8fbf96">route</span> · '
        '<span style="color:#8fbf96">land_use_at</span>'
    )


def test_chip_highlight_overrides_tool_names():
    body = bump_version.render_chip_body(["route"], "faster tile cache")
    assert body == "faster tile cache"


def test_chip_falls_back_when_a_release_adds_no_tools():
    assert bump_version.render_chip_body([], None) == bump_version.DEFAULT_HIGHLIGHT


def test_set_chip_body_replaces_only_the_chip():
    out = bump_version.set_chip_body(CHIP_SAMPLE, "faster tile cache")
    assert "new in this release: faster tile cache</span></div>" in out
    assert "route" not in out
    # The version chip beside it is untouched (bump_site_versions owns that).
    assert "v0.5.0" in out


def test_set_chip_body_fails_loudly_if_the_markup_moved():
    with pytest.raises(bump_version.BumpError, match="chip is missing"):
        bump_version.set_chip_body("<div>no chip here</div>", "anything")


def test_plan_changes_rewrites_every_tracked_file():
    updates, old_version = bump_version.plan_changes("99.0.0", ["route"], None)

    assert re.match(r"^\d+\.\d+\.\d+$", old_version)
    assert bump_version.read_pyproject_version(updates[bump_version.PYPROJECT]) == "99.0.0"
    assert bump_version.read_npm_version(updates[bump_version.NPM_PACKAGE]) == "99.0.0"
    for page in bump_version.SITE_PAGES:
        text = updates[page]
        assert f"v{old_version}" not in text, f"{page.name}: stale version left behind"
    assert "new in this release: " in updates[bump_version.INDEX_HTML]


def test_plan_changes_refuses_a_no_op_bump():
    current = bump_version.read_pyproject_version(
        bump_version.PYPROJECT.read_text(encoding="utf-8")
    )
    with pytest.raises(bump_version.BumpError, match="already at"):
        bump_version.plan_changes(current, [], None)


def test_plan_changes_writes_nothing():
    before = {p: p.read_text(encoding="utf-8") for p in bump_version.SITE_PAGES}
    bump_version.plan_changes("99.0.0", ["route"], None)
    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text, f"{path.name} was mutated"


def test_derived_tools_are_empty_without_a_baseline_tag():
    assert bump_version.derive_new_tools(None) == []


def test_current_tool_surface_is_read_by_real_introspection():
    # Guards the introspection path the derive step depends on: if this ever
    # returns nothing, every release would silently claim "no new tools".
    tools = bump_version.tools_registered_now()
    assert "find_places" in tools
    assert len(tools) > 10
