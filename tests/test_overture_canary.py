"""Offline guards for scripts/overture_canary.py (#219).

The canary itself is network-dependent and runs weekly in CI, so nothing here
probes a bucket. What these cover is the part that can rot silently between
those runs: the requirement table pointing at column lists the runtime no
longer uses, or at a dataset the server reads but the canary doesn't watch.
A canary watching the wrong thing is worse than none — it reports "clean".
"""

import importlib.util
from pathlib import Path

from placeroot import addresses, divisions, land_use, overture

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't a package — load the module by path, the same way
# test_bump_version.py does.
_spec = importlib.util.spec_from_file_location(
    "overture_canary", REPO_ROOT / "scripts" / "overture_canary.py"
)
overture_canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(overture_canary)


def test_requirements_reference_the_runtime_lists_not_copies():
    """Identity, not equality: a restated literal would drift the moment a
    theme module gained or dropped a column."""
    by_dataset = {(t, ty): req for t, ty, req in overture_canary.THEME_REQUIREMENTS}
    assert by_dataset[("places", "place")] is overture.REQUIRED_COLUMNS
    assert by_dataset[("divisions", "division_area")] is divisions.REQUIRED_COLUMNS
    assert by_dataset[("divisions", "division")] is addresses.DIVISION_REQUIRED_COLUMNS
    assert by_dataset[("addresses", "address")] is addresses.REQUIRED_COLUMNS
    assert by_dataset[("base", "land_cover")] is land_use.LAND_COVER_REQUIRED_COLUMNS


def test_every_dataset_the_server_reads_is_watched_exactly_once():
    datasets = [(theme, type_) for theme, type_, _ in overture_canary.THEME_REQUIREMENTS]
    # A duplicate is a second network DESCRIBE of the same dataset for no signal.
    assert len(datasets) == len(set(datasets)), "a dataset is probed twice"
    assert set(datasets) == {
        ("places", "place"),
        ("divisions", "division_area"),
        ("divisions", "division"),
        ("addresses", "address"),
        ("buildings", "building"),
        ("base", "land_use"),
        ("base", "land_cover"),
        ("base", "infrastructure"),
        ("base", "water"),
        ("transportation", "segment"),
    }


def test_required_lists_name_top_level_columns_only():
    """probe_columns compares against DESCRIBE's top-level names, so a nested
    path like "bbox.xmin" in one of these lists would report as missing
    forever. Struct fields are reached through their top-level column."""
    for theme, type_, required in overture_canary.THEME_REQUIREMENTS:
        for column in required:
            assert "." not in column, f"{theme}/{type_}: {column!r} is not a top-level column"


def test_land_cover_is_not_watched_for_columns_it_never_had():
    """land_cover carries neither class nor names upstream; watching it with
    land_use's list would open an issue every week for a by-design absence."""
    assert "class" not in land_use.LAND_COVER_REQUIRED_COLUMNS
    assert "names" not in land_use.LAND_COVER_REQUIRED_COLUMNS
    assert set(land_use.LAND_COVER_REQUIRED_COLUMNS) <= set(land_use.REQUIRED_COLUMNS)
