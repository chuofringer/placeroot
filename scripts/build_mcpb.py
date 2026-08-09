"""Build the Claude Desktop Extension bundle (placeroot.mcpb), issue #233.

A .mcpb is a zip a user installs in one click from Claude Desktop's Chat
surface (Settings → Extensions) — no config file, no JSON, no terminal.
This stages exactly what `mcpb/manifest.json`'s server config needs at
runtime (`uv run --directory ${__dirname} placeroot`): the manifest at the
bundle root, plus pyproject.toml, uv.lock, src/, README and LICENSE — then
shells out to the official packer.

Honest limitation, stated wherever the bundle is offered: the server type
is `uv`, and Claude Desktop does not provide a Python/uv runtime, so the
user's machine still needs uv installed. One-click removes the config
step, not the runtime prerequisite.

Usage:
    uv run python scripts/build_mcpb.py [out.mcpb]     # default: dist/placeroot.mcpb

Requires Node (npx) for @anthropic-ai/mcpb. Never run from pytest — it
shells out and writes outside tmp; tests/test_registry_manifests.py guards
the manifest itself instead.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Pinned so the release-time artifact is reproducible — an unpinned packer
# would build each release with whatever CLI shipped that day, and a
# breaking change would redden the release run after PyPI already
# published (the mcpb job runs parallel to the publish jobs, not ahead).
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"

# Everything `uv run --directory <bundle>` needs to resolve and run the
# server, and nothing else — no tests, no site, no fixtures.
STAGED = ["pyproject.toml", "uv.lock", "README.md", "LICENSE"]
STAGED_TREES = ["src"]


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "dist" / "placeroot.mcpb"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mcpb-stage-") as tmp:
        stage = Path(tmp) / "placeroot"
        stage.mkdir()
        shutil.copy2(REPO_ROOT / "mcpb" / "manifest.json", stage / "manifest.json")
        for name in STAGED:
            shutil.copy2(REPO_ROOT / name, stage / name)
        for tree in STAGED_TREES:
            shutil.copytree(
                REPO_ROOT / tree, stage / tree,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        if shutil.which("npx") is None:
            print(
                "npx not found — the packer is a Node CLI. Install Node 18+ "
                "(or run where node is available) and retry.",
                file=sys.stderr,
            )
            return 1
        result = subprocess.run(
            ["npx", "--yes", MCPB_CLI, "pack", str(stage), str(out)],
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    print(f"built {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
