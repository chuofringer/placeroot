"""Build the Claude Desktop Extension bundle (placeroot.mcpb), issue #233.

A .mcpb is a zip a user installs in one click from Claude Desktop's Chat
surface (Settings → Extensions) — no config file, no JSON, no terminal.
This stages exactly what `mcpb/manifest.json`'s server config needs at
runtime (`uv run --directory ${__dirname} placeroot`): the manifest at the
bundle root, plus pyproject.toml, uv.lock, src/, README and LICENSE — then
shells out to the official packer.

The bundle is a build product, not a committed file: release.yml builds
it from the tagged tree and attaches it to the GitHub release, and
deploy-site.yml / pages.yml build it into site/ right before the site
deploy so https://placeroot.dev/placeroot.mcpb keeps serving (the path is
gitignored). tests/test_mcpb_bundle.py checks whichever build it is
pointed at (PLACEROOT_MCPB, else dist/placeroot.mcpb, else
site/placeroot.mcpb) and skips when none exists.

Honest limitation, stated wherever the bundle is offered: the server type
is `uv`, and Claude Desktop does not provide a Python/uv runtime, so the
user's machine still needs uv installed. One-click removes the config
step, not the runtime prerequisite.

Usage:
    uv run python scripts/build_mcpb.py [OUT]       # default: dist/placeroot.mcpb
    uv run python scripts/build_mcpb.py --dry-run   # stage + list, no packer

Requires Node (npx) for @anthropic-ai/mcpb. Never run from pytest — it
shells out and writes outside tmp; tests/test_registry_manifests.py guards
the manifest itself instead.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "dist" / "placeroot.mcpb"

# Pinned so the release-time artifact is reproducible — an unpinned packer
# would build each release with whatever CLI shipped that day, and a
# breaking change would redden the release run after PyPI already
# published (the mcpb job runs parallel to the publish jobs, not ahead).
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"

# Everything `uv run --directory <bundle>` needs to resolve and run the
# server, and nothing else — no tests, no site, no fixtures.
STAGED = ["pyproject.toml", "uv.lock", "README.md", "LICENSE"]
STAGED_TREES = ["src"]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the Claude Desktop Extension bundle (placeroot.mcpb).",
    )
    parser.add_argument(
        "out",
        nargs="?",
        type=Path,
        default=DEFAULT_OUT,
        help=f"where to write the bundle (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="stage the bundle contents and list them, but do not run the packer "
        "or write OUT (works offline, no Node needed)",
    )
    return parser.parse_args(argv)


def stage(stage_dir: Path) -> list[Path]:
    """Copy the bundle inputs into `stage_dir`; returns the staged files."""
    stage_dir.mkdir()
    shutil.copy2(REPO_ROOT / "mcpb" / "manifest.json", stage_dir / "manifest.json")
    shutil.copy2(REPO_ROOT / "mcpb" / "icon.png", stage_dir / "icon.png")
    for name in STAGED:
        shutil.copy2(REPO_ROOT / name, stage_dir / name)
    for tree in STAGED_TREES:
        shutil.copytree(
            REPO_ROOT / tree,
            stage_dir / tree,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    return sorted(p for p in stage_dir.rglob("*") if p.is_file())


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    out: Path = args.out
    with tempfile.TemporaryDirectory(prefix="mcpb-stage-") as tmp:
        stage_dir = Path(tmp) / "placeroot"
        files = stage(stage_dir)
        if args.dry_run:
            total = sum(p.stat().st_size for p in files)
            for p in files:
                print(p.relative_to(stage_dir).as_posix())
            print(
                f"dry run: {len(files)} files, {total / (1024 * 1024):.1f} MiB unpacked; "
                f"would write {out}",
                file=sys.stderr,
            )
            return 0
        if shutil.which("npx") is None:
            print(
                "npx not found — the packer is a Node CLI. Install Node 18+ "
                "(or run where node is available) and retry.",
                file=sys.stderr,
            )
            return 1
        out.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["npx", "--yes", MCPB_CLI, "pack", str(stage_dir), str(out)],
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    print(f"built {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
