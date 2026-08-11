"""Bundled release manifests (manifest.py): file pruning for bbox scans.

The manifest is strictly an optimization — every test here pins one side
of that contract: either that pruning applies where it should, or that it
gets out of the way (returns None) everywhere correctness could be at
stake.
"""

import pytest

from placeroot import cache, manifest

GLOB = "s3://overturemaps-us-west-2/release/2026-07-22.0/theme=places/type=place/*"
BOX = (139.6, 35.6, 139.8, 35.8)  # Tokyo


@pytest.fixture
def synthetic(monkeypatch):
    files = {
        "part-japan.parquet": [128.0, 30.0, 146.0, 46.0],
        "part-europe.parquet": [-11.0, 35.0, 32.0, 61.0],
        "part-nostats.parquet": [-180.0, -90.0, 180.0, 90.0],  # world extent: never pruned
    }
    monkeypatch.setattr(
        manifest, "_load",
        lambda r, t, ty: {"release": r, "theme": t, "type": ty, "files": dict(files)},
    )
    return files


def test_prunes_to_intersecting_files_plus_conservative_ones(synthetic):
    sql = manifest.pruned_source_sql(GLOB, BOX)
    assert sql is not None
    assert "part-japan.parquet" in sql
    assert "part-nostats.parquet" in sql  # world extent stays in every list
    assert "part-europe.parquet" not in sql
    assert sql.startswith("read_parquet([") and "hive_partitioning=1" in sql
    # Full s3 paths, not basenames: the list replaces the glob verbatim.
    assert GLOB[:-1] + "part-japan.parquet" in sql


def test_gets_out_of_the_way_when_it_cannot_help(synthetic):
    assert manifest.pruned_source_sql(GLOB, None) is None            # no bbox
    assert manifest.pruned_source_sql(GLOB, (179.9, 0, 180.4, 1)) is None  # seam
    assert manifest.pruned_source_sql("/local/fixture.parquet", BOX) is None  # pinned
    # Every file intersects -> the plain glob is simpler.
    assert manifest.pruned_source_sql(GLOB, (-180, -90, 180, 90)) is None


def test_zero_intersections_fall_back_to_the_glob(monkeypatch):
    """With no conservatively-kept world-extent file in play, a box that no
    file's extent touches falls back to the glob — the real scan gets to be
    the one that says "no rows"."""
    monkeypatch.setattr(
        manifest, "_load",
        lambda r, t, ty: {"files": {
            "part-japan.parquet": [128.0, 30.0, 146.0, 46.0],
            "part-europe.parquet": [-11.0, 35.0, 32.0, 61.0],
        }},
    )
    assert manifest.pruned_source_sql(GLOB, (60.0, -50.0, 61.0, -49.0)) is None


def test_unknown_release_falls_back(monkeypatch):
    monkeypatch.setattr(manifest, "_load", lambda r, t, ty: None)
    assert manifest.pruned_source_sql(GLOB, BOX) is None


def test_bundled_manifests_load_and_prune_for_real():
    """The wheel-bundled data for the pinned release is present and useful:
    a city-sized box over the biggest theme keeps only a few files."""
    m = manifest._load("2026-07-22.0", "buildings", "building")
    assert m is not None and len(m["files"]) == 512
    glob = "s3://overturemaps-us-west-2/release/2026-07-22.0/theme=buildings/type=building/*"
    sql = manifest.pruned_source_sql(glob, BOX)
    assert sql is not None
    kept = sql.count(".parquet")
    assert kept <= 16, f"expected a handful of files for a city box, got {kept}"


def test_cache_source_sql_uses_the_manifest_on_the_fallback(monkeypatch, synthetic):
    monkeypatch.setenv("PLACEROOT_CACHE", "off")
    sql = cache.source_sql("places", GLOB, BOX)
    assert "part-japan.parquet" in sql and "part-europe.parquet" not in sql


def test_ensure_tile_copy_reads_only_intersecting_files(tmp_path, monkeypatch, synthetic):
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path))
    captured = {}

    class Con:
        def execute(self, sql):
            captured["sql"] = sql
            raise RuntimeError("stop before writing")

    with pytest.raises(RuntimeError):
        cache.ensure_tile(Con(), "2026-07-22.0", "places", (139, 35), GLOB, "fp")
    assert "part-japan.parquet" in captured["sql"]
    assert "part-europe.parquet" not in captured["sql"]
