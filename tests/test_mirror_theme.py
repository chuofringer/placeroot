"""Offline tests for scripts/mirror_theme.py (#20): source enumeration,
copy/resume, and --verify, all against local directories laid out the same
way the real Overture bucket is (release/theme=X/type=Y/*.parquet) — no
network, no live S3 listing. The one live check (--dry-run against the real
bucket) is opt-in — see tests/test_live.py's pattern; this file covers the
same @pytest.mark.live convention for its single network-touching test.
"""

import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mirror_theme  # noqa: E402

RELEASE = "2026-07-22.0"
THEME = "places"
TYPE_ = "place"


def _write_parquet(path: Path, n_rows: int, offset: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT (i + {offset}) AS id, 'row-' || i AS name "
        f"FROM range({n_rows}) t(i)) TO '{path}' (FORMAT PARQUET)"
    )


def _build_source(base: Path, files: dict[str, int]) -> Path:
    """files: {relative_filename: row_count}. Returns the source base dir."""
    root = base / RELEASE / f"theme={THEME}" / f"type={TYPE_}"
    for name, n_rows in files.items():
        _write_parquet(root / name, n_rows)
    return base


@pytest.fixture
def source_dir(tmp_path):
    return _build_source(
        tmp_path / "source",
        {"part-0000.parquet": 10, "part-0001.parquet": 5, "part-0002.parquet": 0},
    )


# --- Source enumeration -----------------------------------------------------


def test_list_source_files_finds_every_parquet_file_with_its_size(source_dir):
    files = mirror_theme.list_source_files(str(source_dir), RELEASE, THEME, TYPE_)
    assert {f.key for f in files} == {
        "part-0000.parquet", "part-0001.parquet", "part-0002.parquet",
    }
    for f in files:
        on_disk = Path(source_dir) / RELEASE / f"theme={THEME}" / f"type={TYPE_}" / f.key
        assert f.size == on_disk.stat().st_size


def test_list_source_files_empty_for_a_root_that_does_not_exist(tmp_path):
    files = mirror_theme.list_source_files(str(tmp_path / "nope"), RELEASE, THEME, TYPE_)
    assert files == []


def test_theme_root_strips_trailing_slash_and_preserves_layout():
    root = mirror_theme.theme_root("s3://bucket/prefix/", RELEASE, THEME, TYPE_)
    assert root == f"s3://bucket/prefix/{RELEASE}/theme=places/type=place"


# --- dry-run -----------------------------------------------------------


def test_dry_run_reports_every_file_and_total_bytes(source_dir, capsys):
    files = mirror_theme.list_source_files(str(source_dir), RELEASE, THEME, TYPE_)
    mirror_theme.cmd_dry_run(files)
    out = capsys.readouterr().out
    for f in files:
        assert f.key in out
    total = sum(f.size for f in files)
    assert f"{len(files)} files" in out
    assert str(total) in out


def test_main_dry_run_needs_no_target(source_dir, capsys):
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source_dir), "--dry-run"]
    )
    assert rc == 0
    assert "part-0000.parquet" in capsys.readouterr().out


def test_main_without_target_and_without_dry_run_fails_cleanly(source_dir):
    rc = mirror_theme.main(["--release", RELEASE, "--source", str(source_dir)])
    assert rc == 2


# --- mirror + resume ---------------------------------------------------


def test_mirror_copies_every_source_file_and_preserves_row_counts(source_dir, tmp_path):
    target = tmp_path / "target"
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source_dir), "--target", str(target)]
    )
    assert rc == 0

    target_root = Path(mirror_theme.theme_root(str(target), RELEASE, THEME, TYPE_))
    con = duckdb.connect()
    for name, expected_rows in [
        ("part-0000.parquet", 10), ("part-0001.parquet", 5), ("part-0002.parquet", 0),
    ]:
        p = target_root / name
        assert p.exists()
        n = con.execute(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0]
        assert n == expected_rows

    manifest = mirror_theme.load_manifest(target / mirror_theme.MANIFEST_NAME)
    assert set(manifest["files"]) == {"part-0000.parquet", "part-0001.parquet", "part-0002.parquet"}
    assert manifest["release"] == RELEASE


def test_second_mirror_run_skips_every_already_mirrored_file(source_dir, tmp_path, caplog):
    target = tmp_path / "target"
    mirror_theme.main(["--release", RELEASE, "--source", str(source_dir), "--target", str(target)])

    import logging

    caplog.set_level(logging.INFO, logger="mirror_theme")
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source_dir), "--target", str(target)]
    )
    assert rc == 0
    assert "3 copied, 0 skipped" not in caplog.text
    assert "0 copied, 3 skipped" in caplog.text


def test_resume_only_copies_the_file_added_since_the_first_run(tmp_path, caplog):
    import logging

    source = _build_source(tmp_path / "source", {"part-0000.parquet": 10})
    target = tmp_path / "target"
    mirror_theme.main(["--release", RELEASE, "--source", str(source), "--target", str(target)])

    # Add a second source file after the first mirror run completed.
    _write_parquet(
        source / RELEASE / f"theme={THEME}" / f"type={TYPE_}" / "part-0001.parquet", 5
    )
    caplog.set_level(logging.INFO, logger="mirror_theme")
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source), "--target", str(target)]
    )
    assert rc == 0
    assert "1 copied, 1 skipped" in caplog.text


# --- verify --------------------------------------------------------------


def test_verify_passes_immediately_after_a_clean_mirror(source_dir, tmp_path):
    target = tmp_path / "target"
    mirror_theme.main(["--release", RELEASE, "--source", str(source_dir), "--target", str(target)])
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source_dir), "--target", str(target), "--verify"]
    )
    assert rc == 0


def test_verify_catches_a_truncated_target_file(source_dir, tmp_path, caplog):
    import logging

    target = tmp_path / "target"
    mirror_theme.main(["--release", RELEASE, "--source", str(source_dir), "--target", str(target)])

    target_root = Path(mirror_theme.theme_root(str(target), RELEASE, THEME, TYPE_))
    victim = target_root / "part-0000.parquet"
    original = victim.read_bytes()
    victim.write_bytes(original[: len(original) // 2])  # truncate

    caplog.set_level(logging.ERROR, logger="mirror_theme")
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source_dir), "--target", str(target), "--verify"]
    )
    assert rc == 1
    assert "part-0000.parquet" in caplog.text


def test_verify_catches_a_corrupted_target_file_with_valid_size(source_dir, tmp_path, caplog):
    """Same size as the real file (so the cheap size check alone wouldn't
    catch it) but garbage content — verify's row-count re-read must still
    flag it."""
    import logging

    target = tmp_path / "target"
    mirror_theme.main(["--release", RELEASE, "--source", str(source_dir), "--target", str(target)])

    target_root = Path(mirror_theme.theme_root(str(target), RELEASE, THEME, TYPE_))
    victim = target_root / "part-0000.parquet"
    size = victim.stat().st_size
    victim.write_bytes(b"x" * size)

    caplog.set_level(logging.ERROR, logger="mirror_theme")
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source_dir), "--target", str(target), "--verify"]
    )
    assert rc == 1
    assert "part-0000.parquet" in caplog.text


def test_verify_catches_a_file_missing_from_target(source_dir, tmp_path, caplog):
    import logging

    target = tmp_path / "target"
    mirror_theme.main(["--release", RELEASE, "--source", str(source_dir), "--target", str(target)])

    target_root = Path(mirror_theme.theme_root(str(target), RELEASE, THEME, TYPE_))
    (target_root / "part-0001.parquet").unlink()

    caplog.set_level(logging.ERROR, logger="mirror_theme")
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source_dir), "--target", str(target), "--verify"]
    )
    assert rc == 1
    assert "missing from target" in caplog.text


def test_verify_fails_when_the_mirror_never_ran(source_dir, tmp_path):
    target = tmp_path / "target"
    rc = mirror_theme.main(
        ["--release", RELEASE, "--source", str(source_dir), "--target", str(target), "--verify"]
    )
    assert rc == 1


# --- human_bytes -----------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [(0, "0B"), (512, "512B"), (2048, "2.0KB"), (5 * 1024 * 1024, "5.0MB")],
)
def test_human_bytes(n, expected):
    assert mirror_theme.human_bytes(n) == expected


# --- mirror freshness (#219): --check-current / --prune-releases -------
#
# Decision logic only — list_mirror_releases (the S3/local listing call) is
# mocked or driven via local directories, matching this file's existing
# offline-only convention. No network, no real S3 credentials needed.


def test_newest_release_picks_the_numerically_greatest_patch():
    # Plain string/max() would put ".9" above ".10" — this must not.
    assert (
        mirror_theme.newest_release(["2026-07-22.0", "2026-07-22.10", "2026-07-22.9"])
        == "2026-07-22.10"
    )


def test_newest_release_of_empty_list_is_none():
    assert mirror_theme.newest_release([]) is None


def test_releases_to_prune_keeps_only_the_newest():
    assert mirror_theme.releases_to_prune(
        ["2026-05-21.0", "2026-06-22.0", "2026-07-22.0"]
    ) == ["2026-05-21.0", "2026-06-22.0"]


def test_releases_to_prune_of_a_single_release_is_empty():
    assert mirror_theme.releases_to_prune(["2026-07-22.0"]) == []


def test_releases_to_prune_of_no_releases_is_empty():
    assert mirror_theme.releases_to_prune([]) == []


def test_check_current_ok_when_mirror_holds_the_newest_release():
    rc = mirror_theme.cmd_check_current(["2026-06-22.0", "2026-07-22.0"], "2026-07-22.0")
    assert rc == 0


def test_check_current_ok_when_mirror_is_ahead_of_upstream():
    rc = mirror_theme.cmd_check_current(["2026-07-22.0"], "2026-06-22.0")
    assert rc == 0


def test_check_current_fails_when_mirror_is_behind(caplog):
    import logging

    caplog.set_level(logging.ERROR, logger="mirror_theme")
    rc = mirror_theme.cmd_check_current(["2026-06-22.0"], "2026-07-22.0")
    assert rc == 1
    assert "mirror holds 2026-06-22.0, upstream is at 2026-07-22.0" in caplog.text


def test_check_current_fails_when_mirror_holds_nothing(caplog):
    import logging

    caplog.set_level(logging.ERROR, logger="mirror_theme")
    rc = mirror_theme.cmd_check_current([], "2026-07-22.0")
    assert rc == 1
    assert "no releases" in caplog.text


def test_main_check_current_reports_behind_against_a_local_mirror(tmp_path, caplog, monkeypatch):
    import logging

    target = tmp_path / "target"
    (target / "2026-06-22.0" / "theme=places" / "type=place").mkdir(parents=True)
    monkeypatch.setattr(mirror_theme.release_mod, "resolve_release", lambda: "2026-07-22.0")

    caplog.set_level(logging.ERROR, logger="mirror_theme")
    rc = mirror_theme.main(["--target", str(target), "--check-current"])
    assert rc == 1
    assert "mirror holds 2026-06-22.0, upstream is at 2026-07-22.0" in caplog.text


def test_main_check_current_reports_ok_against_a_current_local_mirror(tmp_path, monkeypatch):
    target = tmp_path / "target"
    (target / "2026-07-22.0" / "theme=places" / "type=place").mkdir(parents=True)
    monkeypatch.setattr(mirror_theme.release_mod, "resolve_release", lambda: "2026-07-22.0")

    rc = mirror_theme.main(["--target", str(target), "--check-current"])
    assert rc == 0


def test_main_check_current_requires_target():
    rc = mirror_theme.main(["--check-current"])
    assert rc == 2


def test_list_mirror_releases_from_a_local_directory(tmp_path):
    target = tmp_path / "target"
    for release in ("2026-05-21.0", "2026-06-22.0", "2026-07-22.0"):
        (target / release / "theme=places" / "type=place").mkdir(parents=True)
    (target / "not-a-release").mkdir(parents=True)
    (target / mirror_theme.MANIFEST_NAME).write_text("{}")

    con = duckdb.connect()
    releases = mirror_theme.list_mirror_releases(con, str(target))
    assert releases == ["2026-05-21.0", "2026-06-22.0", "2026-07-22.0"]


def test_list_mirror_releases_of_a_directory_that_does_not_exist(tmp_path):
    con = duckdb.connect()
    releases = mirror_theme.list_mirror_releases(con, str(tmp_path / "nope"))
    assert releases == []


def test_prune_dry_run_deletes_nothing(tmp_path, caplog):
    import logging

    target = tmp_path / "target"
    old_dir = target / "2026-06-22.0" / "theme=places" / "type=place"
    old_dir.mkdir(parents=True)
    _write_parquet(old_dir / "part-0000.parquet", 3)
    new_dir = target / "2026-07-22.0" / "theme=places" / "type=place"
    new_dir.mkdir(parents=True)
    _write_parquet(new_dir / "part-0000.parquet", 3)

    caplog.set_level(logging.INFO, logger="mirror_theme")
    rc = mirror_theme.main(["--target", str(target), "--prune-releases"])
    assert rc == 0
    assert (target / "2026-06-22.0").exists()
    assert (target / "2026-07-22.0").exists()
    assert "would delete" in caplog.text


def test_prune_with_yes_deletes_only_older_releases(tmp_path):
    target = tmp_path / "target"
    old_dir = target / "2026-06-22.0" / "theme=places" / "type=place"
    old_dir.mkdir(parents=True)
    _write_parquet(old_dir / "part-0000.parquet", 3)
    new_dir = target / "2026-07-22.0" / "theme=places" / "type=place"
    new_dir.mkdir(parents=True)
    _write_parquet(new_dir / "part-0000.parquet", 3)

    rc = mirror_theme.main(["--target", str(target), "--prune-releases", "--yes"])
    assert rc == 0
    assert not (target / "2026-06-22.0").exists()
    assert (target / "2026-07-22.0").exists()


def test_prune_with_nothing_to_prune_is_a_noop(tmp_path, caplog):
    import logging

    target = tmp_path / "target"
    (target / "2026-07-22.0" / "theme=places" / "type=place").mkdir(parents=True)

    caplog.set_level(logging.INFO, logger="mirror_theme")
    rc = mirror_theme.main(["--target", str(target), "--prune-releases", "--yes"])
    assert rc == 0
    assert (target / "2026-07-22.0").exists()
    assert "nothing to prune" in caplog.text


def test_prune_of_an_empty_mirror_is_a_noop(tmp_path):
    target = tmp_path / "target"
    rc = mirror_theme.main(["--target", str(target), "--prune-releases", "--yes"])
    assert rc == 0


def test_main_prune_releases_requires_target():
    rc = mirror_theme.main(["--prune-releases"])
    assert rc == 2


# --- live: run once against the real Overture bucket listing ------------


@pytest.mark.live
def test_dry_run_against_real_overture_places_listing(capsys):
    """The one network-touching check for this module — lists the real
    places/place theme for the resolved release and reports file
    count/total size, exactly what an operator's first `--dry-run` run
    would see. Never mirrors real data (that's 10s of GB); listing only.
    """
    from placeroot import release as release_mod

    files = mirror_theme.list_source_files(
        mirror_theme.DEFAULT_SOURCE_BASE, release_mod.resolve_release(), "places", "place"
    )
    assert len(files) > 0
    assert all(f.size > 0 for f in files)
    total = sum(f.size for f in files)
    size_str = mirror_theme.human_bytes(total)
    print(f"LIVE places/place listing: {len(files)} files, {total} bytes ({size_str})")
