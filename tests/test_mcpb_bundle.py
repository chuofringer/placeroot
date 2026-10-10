"""The Desktop Extension bundle can't go stale or outgrow its host silently (#233).

placeroot.mcpb is the public one-click install for Claude Desktop's Chat
surface, served from https://placeroot.dev/placeroot.mcpb. It is a build
product, not a committed file: release.yml builds it from the tagged tree
and attaches it to the GitHub release; deploy-site.yml / pages.yml build
it into site/ (gitignored) right before the site deploy. Both run these
tests against the bundle they just built, so a bundle whose manifest
version doesn't match pyproject, or that would be refused by Cloudflare
Pages, fails the run before anything is published.

Which bundle: $PLACEROOT_MCPB if set, else dist/placeroot.mcpb (the
script's default output), else site/placeroot.mcpb (a local site build).
With none built — the ordinary offline `uv run pytest` — the bundle
content tests skip; only the install-page link guard always runs.

Scope note: this is a VERSION guard, not a content guard, by design. The
bundle is rebuilt from whatever tree the workflow checks out; a
rebuild-and-compare guard isn't practical because zip timestamps make
the output non-deterministic.
"""

import json
import os
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_CANDIDATES = (
    REPO_ROOT / "dist" / "placeroot.mcpb",
    REPO_ROOT / "site" / "placeroot.mcpb",
)

# Cloudflare Pages refuses to upload any single file larger than this, and
# the bundle is served from there (site/), so it is a hard ceiling on what
# the bundle may grow to — see test_bundle_fits_the_pages_file_limit.
PAGES_FILE_LIMIT_MIB = 25


def _built_bundle() -> Path | None:
    override = os.environ.get("PLACEROOT_MCPB")
    if override:
        return Path(override)
    for candidate in BUNDLE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture(scope="module")
def bundle() -> Path:
    path = _built_bundle()
    if path is None:
        pytest.skip(
            "no built bundle (run `uv run python scripts/build_mcpb.py`, or set "
            "PLACEROOT_MCPB to one)"
        )
    assert path.is_file(), f"PLACEROOT_MCPB points at {path}, which does not exist"
    return path


def _bundle_manifest(bundle: Path) -> dict:
    with zipfile.ZipFile(bundle) as z:
        return json.loads(z.read("manifest.json"))


def test_bundle_is_a_zip(bundle):
    assert zipfile.is_zipfile(bundle)


def test_bundle_fits_the_pages_file_limit(bundle):
    """The bundle ships from Cloudflare Pages, which rejects files over 25 MiB.

    Nothing in the build warns about this: the bundle grows with whatever
    src/placeroot/data carries, so a pin bump that leaves the previous
    release's artifacts beside the new ones silently doubles it and the
    site deploy — not the test suite, not the release — is what fails,
    after the release is already tagged (v0.9.8 hit exactly that at 42.8
    MiB). Deleting the superseded release's artifact sets is the fix
    docs/PIN.md already sanctions; this asserts it actually happened.
    """
    size_mib = bundle.stat().st_size / (1024 * 1024)
    assert size_mib < PAGES_FILE_LIMIT_MIB, (
        f"{bundle.name} is {size_mib:.1f} MiB, over Cloudflare Pages' "
        f"{PAGES_FILE_LIMIT_MIB} MiB per-file limit — the site deploy will fail. "
        "Usually this means src/placeroot/data still carries a superseded "
        "release's artifacts (manifests/, geocode-index/, land-cover-grid/); "
        "delete them per docs/PIN.md and rebuild the bundle."
    )


def test_bundle_version_matches_the_package(bundle):
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert _bundle_manifest(bundle)["version"] == pyproject["project"]["version"], (
        f"{bundle} was built for a different version — rebuild it: "
        "uv run python scripts/build_mcpb.py"
    )


def test_bundle_manifest_matches_the_checked_in_manifest(bundle):
    committed = json.loads((REPO_ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert _bundle_manifest(bundle) == committed


def test_bundle_carries_the_runtime_essentials(bundle):
    with zipfile.ZipFile(bundle) as z:
        names = set(z.namelist())
    for required in ("manifest.json", "pyproject.toml", "uv.lock", "src/placeroot/server.py"):
        assert required in names, f"bundle is missing {required}"


def test_install_page_links_the_built_bundle():
    # placeroot.dev serves the site with an HTML fallback instead of 404s,
    # so a filename mismatch between the page and the file the deploy
    # workflows build into site/ would hand the user an HTML page named
    # placeroot.mcpb with nothing failing. Pin the link to the exact name
    # deploy-site.yml / pages.yml write (and .gitignore ignores).
    page = (REPO_ROOT / "site" / "add-to-your-ai.html").read_text(encoding="utf-8")
    assert '"placeroot.mcpb"' in page, "install page no longer links placeroot.mcpb"
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "site/placeroot.mcpb" in gitignore, (
        "site/placeroot.mcpb must stay gitignored — it is built at deploy time, "
        "never committed"
    )
