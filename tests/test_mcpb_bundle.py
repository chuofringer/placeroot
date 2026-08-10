"""The site-hosted Desktop Extension bundle can't go stale silently (#233).

site/placeroot.mcpb is the public one-click install for Claude Desktop's
Chat surface, served from placeroot.dev. It is a build artifact
committed to git, which is exactly the shape that drifts: a release bump
that forgets `uv run python scripts/build_mcpb.py site/placeroot.mcpb`
would ship a site advertising version X while handing out bundle X-1.
These tests make that forgetting fail CI instead.

Scope note: this is a VERSION guard, not a content guard, by design. A
src/ change merged without a version bump leaves the committed bundle
reflecting the last released state — same as PyPI/npm — and the Prepare
Release workflow rebuilds it with every bump. (A rebuild-and-compare
guard isn't practical: zip timestamps make the output non-deterministic.)
"""

import json
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE = REPO_ROOT / "site" / "placeroot.mcpb"


def _bundle_manifest() -> dict:
    with zipfile.ZipFile(BUNDLE) as z:
        return json.loads(z.read("manifest.json"))


def test_site_bundle_exists_and_is_a_zip():
    assert BUNDLE.is_file(), "site/placeroot.mcpb missing — run scripts/build_mcpb.py"
    assert zipfile.is_zipfile(BUNDLE)


def test_site_bundle_version_matches_the_package():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert _bundle_manifest()["version"] == pyproject["project"]["version"], (
        "site/placeroot.mcpb was built for a different version — rebuild it: "
        "uv run python scripts/build_mcpb.py site/placeroot.mcpb"
    )


def test_site_bundle_manifest_matches_the_checked_in_manifest():
    committed = json.loads((REPO_ROOT / "mcpb" / "manifest.json").read_text())
    assert _bundle_manifest() == committed


def test_install_page_links_the_guarded_bundle():
    # placeroot.dev serves the site with an HTML fallback instead of 404s,
    # so a filename mismatch between the page and the file would hand the
    # user an HTML page named placeroot.mcpb with nothing failing. Pin the
    # link to the exact file these tests guard.
    page = (REPO_ROOT / "site" / "add-to-your-ai.html").read_text(encoding="utf-8")
    assert '"placeroot.mcpb"' in page, "install page no longer links site/placeroot.mcpb"


def test_site_bundle_carries_the_runtime_essentials():
    with zipfile.ZipFile(BUNDLE) as z:
        names = set(z.namelist())
    for required in ("manifest.json", "pyproject.toml", "uv.lock", "src/placeroot/server.py"):
        assert required in names, f"bundle is missing {required}"
