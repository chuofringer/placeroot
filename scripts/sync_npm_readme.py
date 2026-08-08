#!/usr/bin/env python3
"""Generates `npm/README.md` from the root `README.md`.

The npm package is published from `npm/` (see .github/workflows/release.yml),
so the root README never reaches the tarball — npm only picks up a README
sitting in the package directory. Keeping a second hand-written README there
means two files making the same claims about tool counts, profiles and setup,
drifting apart release by release. This derives one from the other instead.

    uv run python scripts/sync_npm_readme.py          # write npm/README.md
    uv run python scripts/sync_npm_readme.py --check  # exit 1 if out of date

`tests/test_npm_readme_sync.py` runs the --check path, so drift fails CI the
same way the site version/tool guards do.

What the generator does:

- Keeps the sections in INHERITED_SECTIONS verbatim, so the tool table, the
  PLACEROOT_TOOLS profile table and the positioning copy have exactly one
  source of truth. Sections not listed (Development, Docs) are repo-facing:
  they point at paths that do not exist inside the tarball, which would
  render as broken links on npmjs.com.
- Rewrites the runner from `uvx` to `npx` throughout, because someone reading
  this on npmjs.com arrived for the npm package. The one root-README line
  that already talks about the npm launcher is swapped for its uv counterpart
  rather than being mangled into "npx also works, use npx".
- Splices in the npm-only sections (the launcher explanation, Requirements,
  Documentation) that have no business in the root README.

Adding a section to the root README does NOT add it here — extend
INHERITED_SECTIONS deliberately, then re-run. The check fails loudly rather
than silently shipping whichever subset happened to match.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_README = ROOT / "README.md"
NPM_README = ROOT / "npm" / "README.md"

GENERATED_BANNER = (
    "<!-- Generated from the root README.md by scripts/sync_npm_readme.py.\n"
    "     Edit that file (or the script's npm-only sections), then re-run:\n"
    "         uv run python scripts/sync_npm_readme.py -->"
)

# Root-README `## ` headings carried into the npm package, in output order.
# The document intro (everything above the first `## `) is always included.
INHERITED_SECTIONS = (
    "Why PlaceRoot",
    "Quick start",
    "What it can do",
    "Loading fewer tools (`PLACEROOT_TOOLS`)",
    "Design notes",
)

# Placeholder held across the uvx -> npx rewrite so deliberately-uv text
# survives it. Chosen to be something no README would contain on its own.
_KEEP_UVX = "\x00KEEP_UVX\x00"

# The root README's aside pointing uv readers at the npm launcher. On npm the
# arrow points the other way. Exact-match: if this copy is reworded upstream
# the generator fails rather than emitting a self-contradicting sentence.
NPX_ASIDE = (
    "(`npx placeroot` also works, if you'd rather use the npm launcher: "
    'set `"command": "npx"`.)'
)
UVX_ASIDE = (
    f"(`{_KEEP_UVX} placeroot` also works, if you'd rather run the Python "
    f'server directly: set `"command": "{_KEEP_UVX}"`. This package is a thin '
    "wrapper around exactly that.)"
)

LAUNCHER_NOTE = f"""\
This npm package is the Node launcher. The server itself is Python, distributed \
on PyPI; `npx placeroot` spawns `{_KEEP_UVX} placeroot` under the hood and passes \
through arguments, stdio, and the exit code, so it behaves exactly like running \
`{_KEEP_UVX} placeroot` directly."""

REQUIREMENTS_SECTION = f"""\
## Requirements

- **Node 18+** for the launcher itself.
- **[uv](https://docs.astral.sh/uv/)** on your `PATH` — the launcher shells out \
to `{_KEEP_UVX}`. If it isn't installed, the command exits with install \
instructions rather than a stack trace. `pip install uv` works too.

If you already use Python tooling, `{_KEEP_UVX} placeroot` skips this launcher \
entirely and is equivalent."""

DOCS_SECTION = """\
## Documentation

Full docs, the complete tool reference, and setup guides: \
**[placeroot.dev](https://placeroot.dev)**"""

LICENSE_SECTION = """\
## License

MIT"""


class SyncError(RuntimeError):
    """The root README no longer matches what this generator expects."""


def _split_sections(markdown: str) -> tuple[str, dict[str, str]]:
    """Splits a README into (intro, {heading: section-text-including-heading})."""
    parts = re.split(r"^## ", markdown, flags=re.MULTILINE)
    intro = parts[0]
    sections: dict[str, str] = {}
    for part in parts[1:]:
        heading = part.split("\n", 1)[0].strip()
        if heading in sections:
            raise SyncError(f"README.md has two `## {heading}` sections")
        sections[heading] = "## " + part.rstrip() + "\n"
    return intro, sections


def _to_npx(text: str) -> str:
    """Rewrites the uv runner to the npm one, sparing _KEEP_UVX placeholders."""
    text = text.replace("`uvx placeroot", "`npx placeroot")
    text = text.replace("uvx placeroot", "npx placeroot")
    text = text.replace('"command": "uvx"', '"command": "npx"')
    return text


def render(root_markdown: str) -> str:
    intro, sections = _split_sections(root_markdown)

    missing = [name for name in INHERITED_SECTIONS if name not in sections]
    if missing:
        raise SyncError(
            "README.md is missing sections this generator inherits: "
            + ", ".join(f"`## {name}`" for name in missing)
            + ". Update INHERITED_SECTIONS in scripts/sync_npm_readme.py if the "
            "rename is intentional."
        )

    if NPX_ASIDE not in sections["Quick start"]:
        raise SyncError(
            "README.md's Quick start no longer contains the npm-launcher aside "
            "this generator rewrites. Update NPX_ASIDE/UVX_ASIDE in "
            "scripts/sync_npm_readme.py to match the new wording."
        )
    sections["Quick start"] = sections["Quick start"].replace(NPX_ASIDE, UVX_ASIDE)

    intro = intro.rstrip() + "\n\n" + LAUNCHER_NOTE + "\n"

    chunks = [GENERATED_BANNER, intro.strip()]
    for name in INHERITED_SECTIONS:
        chunks.append(sections[name].strip())
        if name == "Quick start":
            chunks.append(REQUIREMENTS_SECTION)
    chunks.append(DOCS_SECTION)
    chunks.append(LICENSE_SECTION)

    rendered = _to_npx("\n\n".join(chunks) + "\n").replace(_KEEP_UVX, "uvx")

    # Relative links resolve against the repo on GitHub but 404 on npmjs.com,
    # which renders the README standalone. Inherited sections carry none today;
    # this catches one being added upstream instead of shipping a dead link.
    relative = re.findall(r"\]\((?!https?://|#)([^)]+)\)", rendered)
    if relative:
        raise SyncError(
            "inherited README sections contain repo-relative links, which break "
            "on npmjs.com: " + ", ".join(sorted(set(relative)))
        )
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify npm/README.md is up to date instead of writing it",
    )
    args = parser.parse_args(argv)

    try:
        rendered = render(ROOT_README.read_text(encoding="utf-8"))
    except SyncError as exc:
        print(f"sync_npm_readme: {exc}", file=sys.stderr)
        return 1

    if args.check:
        current = NPM_README.read_text(encoding="utf-8") if NPM_README.exists() else ""
        if current != rendered:
            print(
                "sync_npm_readme: npm/README.md is out of date with README.md.\n"
                "Run: uv run python scripts/sync_npm_readme.py",
                file=sys.stderr,
            )
            return 1
        print("npm/README.md is up to date.")
        return 0

    NPM_README.write_text(rendered, encoding="utf-8")
    print(f"Wrote {NPM_README.relative_to(ROOT)} ({len(rendered)} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
