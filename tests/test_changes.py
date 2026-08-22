"""Offline tests for changes.diff_places (issue #376).

Two tiny synthetic parquet fixtures stand in for "release A" and "release
B" of the same theme=places/type=place dataset, registered via
overture.set_data_path's release-keyed override (#375's per-release fixture
plumbing) rather than pointed at real S3. Both fixtures share one small
bbox so the diff has something to compare; ids present in only one fixture
or the other, or present in both with a different name/category, are what
exercise appeared/disappeared/changed classification.

See tests/test_upstream_mirror.py's test_per_release_fixture_override_wins_*
for the release-keyed override pattern this copies, and
tests/test_find_places_in_area.py for the inline-duckdb fixture-building
idiom.
"""

import duckdb
import pytest

from placeroot import changes, overture

# Deliberately not real Overture release names (conftest's offline_data
# fixture pins PLACEROOT_OVERTURE_RELEASE to release.PINNED_RELEASE, and the
# live-data stability test in test_live.py uses the two real releases) --
# any two strings matching release._RELEASE_RE work, since the release-keyed
# override bypasses live-glob construction entirely.
RELEASE_A = "2020-01-01.0"
RELEASE_B = "2020-02-01.0"

# Every fixture row sits inside this box; diff_places is called with it
# unless a test deliberately narrows or breaks it.
BBOX = (0.0, 0.0, 0.1, 0.1)


def _row(id_, lon, lat, name, category, confidence):
    return (
        id_,
        {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat},
        {"primary": name},
        {"primary": category, "alternates": []},
        category,
        confidence,
    )


def _write_places_parquet(con, path, rows):
    con.execute("""
        CREATE OR REPLACE TABLE places (
            id VARCHAR,
            bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE),
            names STRUCT("primary" VARCHAR),
            taxonomy STRUCT("primary" VARCHAR, alternates VARCHAR[]),
            basic_category VARCHAR,
            confidence DOUBLE
        )
    """)
    con.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute(f"COPY places TO '{path}' (FORMAT PARQUET)")


@pytest.fixture
def diff_fixtures(tmp_path):
    """Registers RELEASE_A/RELEASE_B fixtures and yields nothing -- tests
    call diff_places directly. Cleans up the release-keyed overrides in
    finally so a failure here can never leak into another test's release
    resolution (release-keyed overrides are NOT covered by conftest's
    autouse offline_data teardown, which only clears the bare-theme one)."""
    con = duckdb.connect()

    rows_a = [
        _row("u1", 0.01, 0.01, "Unchanged Cafe", "cafe", 0.9),
        _row("d1", 0.02, 0.02, "Disappeared Shop", "shop", 0.8),
        _row("c1", 0.03, 0.03, "Same Name", "shop", 0.7),
        _row("c2", 0.04, 0.04, "Old Name", "cafe", 0.6),
        # Outside BBOX -- proves the bbox filter, not just the id diff,
        # keeps out-of-area rows out of the comparison entirely.
        _row("far", 5.0, 5.0, "Far Away Place", "cafe", 0.99),
    ]
    rows_b = [
        _row("u1", 0.01, 0.01, "Unchanged Cafe", "cafe", 0.9),
        _row("a1", 0.05, 0.05, "Appeared Bakery", "bakery", 0.85),
        _row("c1", 0.03, 0.03, "Same Name", "bakery", 0.7),  # category changed
        _row("c2", 0.04, 0.04, "New Name", "cafe", 0.6),  # name changed
        _row("far", 5.0, 5.0, "Far Away Place", "cafe", 0.99),
    ]
    path_a = tmp_path / "places_a.parquet"
    path_b = tmp_path / "places_b.parquet"
    _write_places_parquet(con, path_a, rows_a)
    _write_places_parquet(con, path_b, rows_b)

    overture.set_data_path(str(path_a), "places", "place", release=RELEASE_A)
    overture.set_data_path(str(path_b), "places", "place", release=RELEASE_B)
    try:
        yield
    finally:
        overture.set_data_path(None, "places", "place", release=RELEASE_A)
        overture.set_data_path(None, "places", "place", release=RELEASE_B)


def test_appeared_disappeared_and_changed_classification(diff_fixtures):
    result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B)

    appeared_ids = {r["id"] for r in result["appeared"]}
    disappeared_ids = {r["id"] for r in result["disappeared"]}
    changed_ids = {r["id"] for r in result["changed"]}

    assert appeared_ids == {"a1"}
    assert disappeared_ids == {"d1"}
    assert changed_ids == {"c1", "c2"}
    # "far" and "u1" are neither: far is outside bbox, u1 is identical.
    assert "far" not in appeared_ids | disappeared_ids | changed_ids
    assert "u1" not in appeared_ids | disappeared_ids | changed_ids


def test_appeared_row_shape_comes_from_release_b(diff_fixtures):
    result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B)
    (row,) = [r for r in result["appeared"] if r["id"] == "a1"]
    assert row["name"] == "Appeared Bakery"
    assert row["category"] == "bakery"
    assert row["confidence"] == pytest.approx(0.85)
    assert row["lat"] == pytest.approx(0.05)
    assert row["lon"] == pytest.approx(0.05)


def test_disappeared_row_shape_comes_from_release_a(diff_fixtures):
    result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B)
    (row,) = [r for r in result["disappeared"] if r["id"] == "d1"]
    assert row["name"] == "Disappeared Shop"
    assert row["category"] == "shop"
    assert row["confidence"] == pytest.approx(0.8)


def test_changed_category_carries_old_and_new_values(diff_fixtures):
    result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B)
    (row,) = [r for r in result["changed"] if r["id"] == "c1"]
    assert row["old_category"] == "shop"
    assert row["new_category"] == "bakery"
    assert row["old_name"] == row["new_name"] == "Same Name"


def test_changed_name_carries_old_and_new_values(diff_fixtures):
    result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B)
    (row,) = [r for r in result["changed"] if r["id"] == "c2"]
    assert row["old_name"] == "Old Name"
    assert row["new_name"] == "New Name"
    assert row["old_category"] == row["new_category"] == "cafe"


def test_unchanged_is_counted_but_not_listed(diff_fixtures):
    result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B)
    assert result["counts"]["unchanged"] == 1  # only u1; far is outside bbox
    for bucket in ("appeared", "disappeared", "changed"):
        assert "u1" not in {r["id"] for r in result[bucket]}


def test_counts_match_full_lists_and_releases_echoed(diff_fixtures):
    result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B)
    assert result["counts"] == {
        "appeared": 1,
        "disappeared": 1,
        "changed": 2,
        "unchanged": 1,
    }
    assert result["truncated"] is False
    assert result["releases"] == {"from": RELEASE_A, "to": RELEASE_B}


def test_category_filter_narrows_both_scans(diff_fixtures):
    """category="cafe" must drop c1 (shop->bakery, never cafe in either
    release) entirely, while still catching c2 (cafe in both) as changed
    and u1 (cafe in both) as unchanged."""
    result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B, category="cafe")
    changed_ids = {r["id"] for r in result["changed"]}
    assert changed_ids == {"c2"}
    assert result["counts"]["unchanged"] == 1
    assert result["appeared"] == []  # a1 is a bakery
    assert result["disappeared"] == []  # d1 is a shop


def test_limit_truncates_lists_but_counts_stay_exact(tmp_path):
    """Five appeared ids, limit=2: the appeared list is capped to 2 (highest
    confidence first) but counts.appeared reports the true total, and
    truncated is True."""
    con = duckdb.connect()
    rows_a = [_row("keep", 0.01, 0.01, "Keep", "cafe", 0.9)]
    rows_b = [_row("keep", 0.01, 0.01, "Keep", "cafe", 0.9)] + [
        _row(f"new{i}", 0.02 + i * 0.001, 0.02, f"New Place {i}", "cafe", 0.5 + i * 0.01)
        for i in range(5)
    ]
    path_a = tmp_path / "a.parquet"
    path_b = tmp_path / "b.parquet"
    _write_places_parquet(con, path_a, rows_a)
    _write_places_parquet(con, path_b, rows_b)
    overture.set_data_path(str(path_a), "places", "place", release=RELEASE_A)
    overture.set_data_path(str(path_b), "places", "place", release=RELEASE_B)
    try:
        result = changes.diff_places(BBOX, RELEASE_A, RELEASE_B, limit=2)
    finally:
        overture.set_data_path(None, "places", "place", release=RELEASE_A)
        overture.set_data_path(None, "places", "place", release=RELEASE_B)

    assert result["counts"]["appeared"] == 5
    assert len(result["appeared"]) == 2
    assert result["truncated"] is True
    # Highest confidence first: new4 (0.54) then new3 (0.53).
    assert [r["id"] for r in result["appeared"]] == ["new4", "new3"]


def test_invalid_bbox_min_not_less_than_max_rejected(diff_fixtures):
    with pytest.raises(ValueError):
        changes.diff_places((1.0, 1.0, 0.0, 0.0), RELEASE_A, RELEASE_B)


def test_invalid_bbox_out_of_range_coordinates_rejected(diff_fixtures):
    with pytest.raises(ValueError):
        changes.diff_places((-200.0, 0.0, -190.0, 1.0), RELEASE_A, RELEASE_B)


def test_oversized_bbox_rejected(diff_fixtures):
    with pytest.raises(ValueError):
        changes.diff_places((0.0, 0.0, 10.0, 10.0), RELEASE_A, RELEASE_B)


def test_same_release_on_both_sides_rejected(diff_fixtures):
    with pytest.raises(ValueError):
        changes.diff_places(BBOX, RELEASE_A, RELEASE_A)


def test_invalid_release_string_rejected(diff_fixtures):
    with pytest.raises(ValueError):
        changes.diff_places(BBOX, "not-a-release", RELEASE_B)


# --- changes_digest (issue #377) --------------------------------------------
#
# Pure-function tests: changes_digest() takes a diff_places()-shaped dict and
# reshapes it, so these build that dict by hand rather than paying for a
# fixture scan -- diff_places' own tests above already cover how that dict
# gets produced.


def _fake_row(id_, name, category, confidence=0.5):
    return {
        "id": id_,
        "name": name,
        "category": category,
        "confidence": confidence,
        "lat": 0.0,
        "lon": 0.0,
    }


def _fake_changed_row(id_, old_category, new_category):
    return {
        "id": id_,
        "old_name": "Old",
        "new_name": "New",
        "old_category": old_category,
        "new_category": new_category,
        "confidence": 0.5,
        "lat": 0.0,
        "lon": 0.0,
    }


def _fake_diff(appeared=(), disappeared=(), changed=(), unchanged=0, truncated=False):
    return {
        "appeared": list(appeared),
        "disappeared": list(disappeared),
        "changed": list(changed),
        "counts": {
            "appeared": len(appeared),
            "disappeared": len(disappeared),
            "changed": len(changed),
            "unchanged": unchanged,
        },
        "truncated": truncated,
        "releases": {"from": RELEASE_A, "to": RELEASE_B},
    }


def test_digest_carries_exact_counts_and_releases():
    diff = _fake_diff(appeared=[_fake_row("a1", "A", "cafe")], unchanged=3)
    digest = changes.changes_digest(diff)
    assert digest["counts"] == {"appeared": 1, "disappeared": 0, "changed": 0, "unchanged": 3}
    assert digest["releases"] == {"from": RELEASE_A, "to": RELEASE_B}


def test_digest_caps_each_bucket_at_top_n():
    appeared = [_fake_row(f"a{i}", f"Place {i}", "cafe") for i in range(20)]
    digest = changes.changes_digest(_fake_diff(appeared=appeared), top_n=8)
    assert len(digest["appeared"]) == 8
    assert digest["counts"]["appeared"] == 20  # exact count, never capped by top_n


def test_digest_truncated_when_top_n_cuts_a_bucket_diff_did_not_truncate():
    appeared = [_fake_row(f"a{i}", f"Place {i}", "cafe") for i in range(20)]
    diff = _fake_diff(appeared=appeared, truncated=False)
    digest = changes.changes_digest(diff, top_n=8)
    assert digest["truncated"] is True


def test_digest_truncated_propagates_from_diff():
    diff = _fake_diff(appeared=[_fake_row("a1", "A", "cafe")], truncated=True)
    digest = changes.changes_digest(diff)
    assert digest["truncated"] is True


def test_digest_not_truncated_when_nothing_was_cut():
    diff = _fake_diff(appeared=[_fake_row("a1", "A", "cafe")], truncated=False)
    digest = changes.changes_digest(diff, top_n=8)
    assert digest["truncated"] is False


def test_digest_delisting_note_present_whenever_disappeared_gt_zero():
    diff = _fake_diff(disappeared=[_fake_row("d1", "D", "shop")])
    digest = changes.changes_digest(diff)
    assert any("delist" in n.lower() or "closed" in n.lower() for n in digest["notes"])


def test_digest_newly_mapped_note_present_whenever_appeared_gt_zero():
    diff = _fake_diff(appeared=[_fake_row("a1", "A", "cafe")])
    digest = changes.changes_digest(diff)
    assert any("newly" in n.lower() or "mapped" in n.lower() for n in digest["notes"])


def test_digest_both_notes_present_when_both_buckets_nonempty():
    diff = _fake_diff(
        appeared=[_fake_row("a1", "A", "cafe")],
        disappeared=[_fake_row("d1", "D", "shop")],
    )
    digest = changes.changes_digest(diff)
    assert len(digest["notes"]) == 2


def test_digest_no_notes_when_nothing_appeared_or_disappeared():
    diff = _fake_diff(changed=[_fake_changed_row("c1", "shop", "bakery")], unchanged=5)
    digest = changes.changes_digest(diff)
    assert "notes" not in digest


def test_digest_by_category_ranks_and_caps():
    appeared = [_fake_row(f"cafe{i}", f"Cafe {i}", "cafe") for i in range(3)] + [
        _fake_row(f"shop{i}", f"Shop {i}", "shop") for i in range(1)
    ]
    digest = changes.changes_digest(_fake_diff(appeared=appeared))
    assert digest["appeared_by_category"]["cafe"] == 3
    assert digest["appeared_by_category"]["shop"] == 1
    # cafe (3) ranks ahead of shop (1).
    assert list(digest["appeared_by_category"]) == ["cafe", "shop"]


def test_digest_changed_by_category_uses_new_category():
    changed = [_fake_changed_row("c1", "shop", "bakery")]
    digest = changes.changes_digest(_fake_diff(changed=changed))
    assert digest["changed_by_category"] == {"bakery": 1}


def test_digest_empty_diff_has_no_notes_and_empty_buckets():
    digest = changes.changes_digest(_fake_diff())
    assert digest["appeared"] == digest["disappeared"] == digest["changed"] == []
    assert "notes" not in digest
    assert digest["truncated"] is False
