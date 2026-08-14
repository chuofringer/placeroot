"""Offline tests for scripts/bump_pin.py — the PINNED_RELEASE bumper (#219).

Mirrors tests/test_bump_version.py's approach: exercise the pure
read/rewrite helpers as strings in, strings out, plus an end-to-end run
against a temp copy of the real release.py — never writing to the tree.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# scripts/ isn't a package; load bump_pin.py by path, same as
# test_bump_version.py does for its sibling script.
_spec = importlib.util.spec_from_file_location(
    "bump_pin", REPO_ROOT / "scripts" / "bump_pin.py"
)
bump_pin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_pin)


RELEASE_PY_SAMPLE = '''"""Discover the current Overture Maps release, with a pinned fallback."""

import re

PINNED_RELEASE = "2026-07-22.0"

LISTING_URL = "https://example.invalid/"
'''


def test_reads_the_current_pin():
    assert bump_pin.read_pinned_release(RELEASE_PY_SAMPLE) == "2026-07-22.0"


def test_read_pinned_release_raises_when_the_line_is_missing():
    with pytest.raises(bump_pin.BumpPinError):
        bump_pin.read_pinned_release("PINNED = 'not the right shape'\n")


def test_bump_rewrites_only_the_pin_line():
    new_text = bump_pin.bump_pinned_release(RELEASE_PY_SAMPLE, "2026-08-19.0")
    assert 'PINNED_RELEASE = "2026-08-19.0"' in new_text
    assert "2026-07-22.0" not in new_text
    # Nothing else in the file moved.
    assert new_text.replace("2026-08-19.0", "2026-07-22.0") == RELEASE_PY_SAMPLE


def test_bump_only_touches_the_pinned_release_assignment():
    """A release string appearing elsewhere in the file (a comment, say) is
    left alone — the regex is anchored to the PINNED_RELEASE assignment."""
    text = RELEASE_PY_SAMPLE + '\n# fallback used since 2026-07-22.0\n'
    new_text = bump_pin.bump_pinned_release(text, "2026-08-19.0")
    assert 'PINNED_RELEASE = "2026-08-19.0"' in new_text
    assert "# fallback used since 2026-07-22.0" in new_text


# --- CLI-level validation ----------------------------------------------------


def test_rejects_a_malformed_release(capsys):
    with pytest.raises(SystemExit):
        bump_pin.main(["not-a-release"])


def test_dry_run_reports_without_writing(tmp_path, monkeypatch, capsys):
    release_py = tmp_path / "release.py"
    release_py.write_text(RELEASE_PY_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(bump_pin, "RELEASE_PY", release_py)
    monkeypatch.setattr(bump_pin, "REPO_ROOT", tmp_path)

    rc = bump_pin.main(["2026-08-19.0", "--dry-run"])

    assert rc == 0
    assert release_py.read_text(encoding="utf-8") == RELEASE_PY_SAMPLE
    out = capsys.readouterr().out
    assert "2026-07-22.0 -> 2026-08-19.0" in out
    assert "Dry run" in out


def test_bump_writes_the_new_pin_and_prints_next_steps(tmp_path, monkeypatch, capsys):
    release_py = tmp_path / "release.py"
    release_py.write_text(RELEASE_PY_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(bump_pin, "RELEASE_PY", release_py)
    monkeypatch.setattr(bump_pin, "REPO_ROOT", tmp_path)

    rc = bump_pin.main(["2026-08-19.0"])

    assert rc == 0
    assert 'PINNED_RELEASE = "2026-08-19.0"' in release_py.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "build_release_manifest.py" in out
    assert "docs/PIN.md" in out


def test_bumping_to_the_same_release_is_an_error(tmp_path, monkeypatch, capsys):
    release_py = tmp_path / "release.py"
    release_py.write_text(RELEASE_PY_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(bump_pin, "RELEASE_PY", release_py)
    monkeypatch.setattr(bump_pin, "REPO_ROOT", tmp_path)

    rc = bump_pin.main(["2026-07-22.0"])

    assert rc == 1
    assert release_py.read_text(encoding="utf-8") == RELEASE_PY_SAMPLE
    assert "nothing to bump" in capsys.readouterr().err


# --- end-to-end against the real repo file (never written) ------------------


def test_current_pin_round_trips_through_the_real_file():
    """Sanity check against the real src/placeroot/release.py: the regex this
    script depends on actually matches the file as it exists in the tree."""
    text = bump_pin.RELEASE_PY.read_text(encoding="utf-8")
    current = bump_pin.read_pinned_release(text)
    assert bump_pin._RELEASE_RE.match(current)
    bumped = bump_pin.bump_pinned_release(text, "2099-01-01.0")
    assert bump_pin.read_pinned_release(bumped) == "2099-01-01.0"
    # Round-tripping back to the original pin reproduces the original text.
    assert bump_pin.bump_pinned_release(bumped, current) == text
