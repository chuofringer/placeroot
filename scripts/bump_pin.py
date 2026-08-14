#!/usr/bin/env python3
"""Bump `release.PINNED_RELEASE`, and print the checklist for what else moves.

#219's canary tells you *when* the pin should move (upstream shipped a newer
release, or a required column vanished); this script and docs/PIN.md are the
*how*. See docs/PIN.md for the full rationale — the short version:

  - `PINNED_RELEASE` (src/placeroot/release.py) is the one line every pin
    bump must change, and the only one this script touches. It's a plain
    textual substitution, safe to automate the same way bump_version.py
    bumps the package version.
  - Everything else that mentions the old release string is either a
    generated artifact keyed by release name (regenerate with a script, not
    a text edit — see the printed checklist) or a historical "measured live
    on <release>" comment that must NOT move with the pin (it documents a
    specific past measurement, not the current one). Sweeping those with a
    blind find-and-replace would corrupt the historical record, so this
    script deliberately does not attempt it — it only prints where they are.

Usage:
    uv run python scripts/bump_pin.py 2026-08-19.0
    uv run python scripts/bump_pin.py 2026-08-19.0 --dry-run
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

RELEASE_PY = REPO_ROOT / "src" / "placeroot" / "release.py"

# Same shape release.py's own _RELEASE_RE enforces (YYYY-MM-DD.N). Duplicated
# rather than imported: importing release.py pulls in duckdb-adjacent
# package init for a one-line regex, and this script must stay a plain text
# rewriter that runs (and is testable) with nothing installed.
_RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")

_PIN_RE = re.compile(r'(?m)^(PINNED_RELEASE\s*=\s*")(\d{4}-\d{2}-\d{2}\.\d+)(")')


class BumpPinError(RuntimeError):
    """A precondition failed; the tree is left untouched."""


def read_pinned_release(text: str) -> str:
    match = _PIN_RE.search(text)
    if not match:
        raise BumpPinError(
            f"{RELEASE_PY.relative_to(REPO_ROOT)}: no `PINNED_RELEASE = \"...\"` line found"
        )
    return match.group(2)


def bump_pinned_release(text: str, new_release: str) -> str:
    """Rewrite the `PINNED_RELEASE = "..."` line to `new_release`.

    Only the first match is touched (there is exactly one such assignment in
    release.py); any other occurrence of the old string elsewhere in the file
    — there is none today, but this stays narrow on purpose — is left alone.
    """
    return _PIN_RE.sub(rf"\g<1>{new_release}\g<3>", text, count=1)


# Printed after a successful bump. Each line names what still has to move and
# how — nothing here is rewritten automatically (see the module docstring for
# why). Kept in one place so docs/PIN.md and this script can't drift from
# each other; docs/PIN.md is the longer version with the full rationale per
# category.
NEXT_STEPS = """\
Next — none of this is automatic, see docs/PIN.md for the full rationale:

1. Regenerate the three bundled artifact sets for the new release (skipping
   this leaves `data_version` reporting `artifacts: unmatched` and cold
   queries lose their acceleration until it's done):
     uv run python scripts/build_release_manifest.py
     uv run python scripts/build_geocode_index.py
     uv run python scripts/build_land_cover_grid.py

2. Update the one test that asserts against the real bundled data by its old
   release name and file count:
     tests/test_manifest.py (test_bundled_manifests_load_and_prune_for_real)

3. Re-run the live suite and skim for anything that degraded on the new
   release (schema drift the canary didn't already catch, coverage changes):
     uv run pytest -m live

4. Regenerate the benchmark/measurement snapshots that cite a release number
   (each documents when it was captured — leave any that only describe a
   past measurement, e.g. "measured live on <old release>" comments in
   src/placeroot/*.py and tests/*.py; those are historical record, not
   pointers to the current pin):
     uv run python benchmarks/token_efficiency.py --write
     uv run python benchmarks/run_query_corpus.py   (see benchmarks/results.md)
     uv run python benchmarks/competitor_comparison.py

5. Sweep for any other doc reporting a live sample tied to the old release
   (docs/MIRROR.md's dry-run sample, docs/RECREATION.md's coverage numbers,
   README.md's recreation-layer stat) and re-capture by hand if it's due —
   `git grep <old-release>` finds every remaining mention.

6. uv run ruff check . && uv run pytest -q -m "not live"
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("release", help="the new pinned release, e.g. 2026-08-19.0")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change without writing"
    )
    args = parser.parse_args(argv)

    if not _RELEASE_RE.match(args.release):
        parser.error(f"release must look like YYYY-MM-DD.N, got {args.release!r}")

    try:
        text = RELEASE_PY.read_text(encoding="utf-8")
        old_release = read_pinned_release(text)
        if args.release == old_release:
            raise BumpPinError(f"already pinned to {args.release} — nothing to bump.")
        new_text = bump_pinned_release(text, args.release)
    except BumpPinError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"{old_release} -> {args.release}")
    rel = RELEASE_PY.relative_to(REPO_ROOT)
    if args.dry_run:
        print(f"  would edit {rel}")
        print("\nDry run — nothing written.")
        return 0

    RELEASE_PY.write_text(new_text, encoding="utf-8")
    print(f"  updated    {rel}")
    print()
    print(NEXT_STEPS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
