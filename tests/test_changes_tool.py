"""Offline tests for the `changes_in_area` MCP tool (issue #377).

Two families:

1. Bbox-mode tests run the real changes.diff_places() query layer against
   the same two-parquet-fixture pattern tests/test_changes.py uses (a
   release-keyed override of the places dataset for RELEASE_A/RELEASE_B) --
   these exercise the actual diff, not a stub of it.

2. Named-area and error-path tests monkeypatch geocode.resolve_area,
   overture._resolve_division_geometry, and release.available_releases at
   the boundary server.changes_in_area calls them through -- building a
   real division polygon fixture (as tests/test_find_places_by_area.py
   does) for every one of these would be a lot of DuckDB setup to test
   control flow that doesn't touch the polygon shape itself.

Every test monkeypatches release.available_releases explicitly: the
offline_data autouse fixture (conftest.py) pins PLACEROOT_OVERTURE_RELEASE
for the *query* path, but available_releases() does its own live S3-listing
HTTP call independent of that env var -- leaving it unpatched here would
turn every test into a live-network test.
"""

import duckdb
import pytest

from placeroot import changes, geocode, overture, release, server
from placeroot.errors import AmbiguousArea

RELEASE_A = "2020-01-01.0"
RELEASE_B = "2020-02-01.0"
RELEASE_MID = "2020-01-15.0"

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
    """RELEASE_A/RELEASE_B places fixtures, same shape as test_changes.py's."""
    con = duckdb.connect()
    rows_a = [
        _row("u1", 0.01, 0.01, "Unchanged Cafe", "cafe", 0.9),
        _row("d1", 0.02, 0.02, "Disappeared Shop", "shop", 0.8),
    ]
    rows_b = [
        _row("u1", 0.01, 0.01, "Unchanged Cafe", "cafe", 0.9),
        _row("a1", 0.05, 0.05, "Appeared Bakery", "bakery", 0.85),
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


def _stub_releases(monkeypatch, releases):
    monkeypatch.setattr(release, "available_releases", lambda: list(releases))


# --- explicit bbox path ------------------------------------------------


def test_explicit_bbox_diffs_between_given_releases(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert "error" not in result
    assert result["counts"] == {"appeared": 1, "disappeared": 1, "changed": 0, "unchanged": 1}
    assert {r["id"] for r in result["appeared"]} == {"a1"}
    assert {r["id"] for r in result["disappeared"]} == {"d1"}
    assert result["releases"] == {"from": RELEASE_A, "to": RELEASE_B}
    assert "area" not in result


def test_partial_bbox_is_bad_request(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    result = server.changes_in_area(min_lon=0.0, min_lat=0.0)
    assert result["error"] == "bad_request"


def test_place_and_bbox_together_is_bad_request(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    result = server.changes_in_area(
        place="Palo Alto",
        min_lon=0.0,
        min_lat=0.0,
        max_lon=0.1,
        max_lat=0.1,
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert result["error"] == "bad_request"


def test_neither_place_nor_bbox_is_bad_request(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    assert server.changes_in_area()["error"] == "bad_request"


def test_oversized_explicit_bbox_is_bad_request(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    result = server.changes_in_area(
        min_lon=0.0,
        min_lat=0.0,
        max_lon=10.0,
        max_lat=10.0,
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert result["error"] == "bad_request"


# --- release defaulting and validation ----------------------------------


def _spy_diff(monkeypatch, seen):
    """Replace changes.diff_places with a canned-empty-diff spy that records
    which releases the tool defaulted to -- these tests are about the
    window choice, not the diff itself."""

    def spy(bbox, release_a, release_b, category=None, limit=50):
        seen["releases"] = (release_a, release_b)
        return {
            "appeared": [],
            "disappeared": [],
            "changed": [],
            "counts": {"appeared": 0, "disappeared": 0, "changed": 0, "unchanged": 0},
            "truncated": False,
            "releases": {"from": release_a, "to": release_b},
        }

    monkeypatch.setattr(changes, "diff_places", spy)


def test_omitted_releases_default_to_previous_vs_active(diff_fixtures, monkeypatch):
    """to_release defaults to the ACTIVE release (resolve_release, honoring
    pins -- what every other tool queries), and from_release to the newest
    listed release older than it: adjacent and recent, never the years-old
    oldest release Overture still serves."""
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", RELEASE_B)
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_MID, RELEASE_B])
    seen = {}
    _spy_diff(monkeypatch, seen)
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
    )
    assert "error" not in result
    assert seen["releases"] == (RELEASE_MID, RELEASE_B)
    assert result["releases"] == {"from": RELEASE_MID, "to": RELEASE_B}


def test_active_release_missing_from_listing_still_defaults_sanely(diff_fixtures, monkeypatch):
    """A pinned/mirrored install's active release may not appear in the
    public listing at all -- the default window is then newest-older-listed
    release -> active, not an error."""
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", RELEASE_B)
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_MID])
    seen = {}
    _spy_diff(monkeypatch, seen)
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
    )
    assert "error" not in result
    assert seen["releases"] == (RELEASE_MID, RELEASE_B)


def test_no_release_older_than_active_is_a_structured_error(diff_fixtures, monkeypatch):
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", RELEASE_A)
    _stub_releases(monkeypatch, [RELEASE_B])  # newer than active, no older one
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
    )
    assert result["error"] == "upstream_unavailable"
    assert RELEASE_B in result["detail"]
    assert "appeared" not in result  # never an empty diff standing in for the error


def test_zero_available_releases_is_a_structured_error(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [])
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
    )
    assert result["error"] == "upstream_unavailable"


def test_only_one_of_from_to_release_given_is_bad_request(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
        from_release=RELEASE_A,
    )
    assert result["error"] == "bad_request"


def test_explicit_release_not_in_available_list_is_bad_request(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
        from_release=RELEASE_A,
        to_release="2099-01-01.0",
    )
    assert result["error"] == "bad_request"
    assert "2099-01-01.0" in result["detail"]


def test_explicit_releases_tried_directly_when_listing_is_unreachable(diff_fixtures, monkeypatch):
    """available_releases() == [] means the listing failed, not that no
    release exists -- explicit releases should still be attempted."""
    _stub_releases(monkeypatch, [])
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert "error" not in result
    assert result["releases"] == {"from": RELEASE_A, "to": RELEASE_B}


# --- named-area path (monkeypatched resolution) -------------------------


def _stub_area_resolution(monkeypatch, division_id, name, bbox):
    xmin, ymin, xmax, ymax = bbox
    monkeypatch.setattr(
        geocode,
        "resolve_area",
        lambda area: {"division_id": division_id, "name": name, "admin_context": []},
    )
    monkeypatch.setattr(
        overture,
        "_resolve_division_geometry",
        lambda division_id_: (b"", xmin, xmax, ymin, ymax),
    )


def test_named_area_resolves_and_diffs(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    _stub_area_resolution(monkeypatch, "gers-div-x", "Notchville", BBOX)
    result = server.changes_in_area(
        place="Notchville",
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert "error" not in result
    assert result["area"] == {
        "division_id": "gers-div-x",
        "name": "Notchville",
        "admin_context": [],
    }
    assert result["counts"]["appeared"] == 1


def test_named_area_too_large_is_bad_request(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    too_big = (0.0, 0.0, 10.0, 10.0)
    _stub_area_resolution(monkeypatch, "gers-div-huge", "Hugeville", too_big)
    result = server.changes_in_area(
        place="Hugeville",
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert result["error"] == "bad_request"
    assert "Hugeville" in result["detail"]


def test_named_area_ambiguous_returns_candidates(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    candidates = [{"division_id": "a"}, {"division_id": "b"}]

    def raise_ambiguous(area):
        raise AmbiguousArea(area, candidates)

    monkeypatch.setattr(geocode, "resolve_area", raise_ambiguous)
    result = server.changes_in_area(place="London", from_release=RELEASE_A, to_release=RELEASE_B)
    assert result["error"] == "ambiguous_area"
    assert result["candidates"] == candidates


def test_named_area_not_found(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    monkeypatch.setattr(geocode, "resolve_area", lambda area: None)
    result = server.changes_in_area(
        place="Definitely Not A Real Place XYZ",
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert result["error"] == "not_found"


def test_blank_place_is_bad_request(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    assert server.changes_in_area(place="   ")["error"] == "bad_request"


# --- digest / honest-framing / truncation propagation on the tool path --


def test_disappeared_note_present_via_the_tool(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert result["counts"]["disappeared"] > 0
    assert any("delist" in n.lower() or "closed" in n.lower() for n in result["notes"])


def test_appeared_note_present_via_the_tool(diff_fixtures, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    result = server.changes_in_area(
        min_lon=BBOX[0],
        min_lat=BBOX[1],
        max_lon=BBOX[2],
        max_lat=BBOX[3],
        from_release=RELEASE_A,
        to_release=RELEASE_B,
    )
    assert result["counts"]["appeared"] > 0
    assert any("newly" in n.lower() or "mapped" in n.lower() for n in result["notes"])


def test_truncated_flag_set_when_more_appeared_than_the_digest_shows(tmp_path, monkeypatch):
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    con = duckdb.connect()
    rows_a = [_row("keep", 0.01, 0.01, "Keep", "cafe", 0.9)]
    rows_b = [_row("keep", 0.01, 0.01, "Keep", "cafe", 0.9)] + [
        _row(f"new{i}", 0.02 + i * 0.001, 0.02, f"New Place {i}", "cafe", 0.5 + i * 0.01)
        for i in range(12)
    ]
    path_a = tmp_path / "a.parquet"
    path_b = tmp_path / "b.parquet"
    _write_places_parquet(con, path_a, rows_a)
    _write_places_parquet(con, path_b, rows_b)
    overture.set_data_path(str(path_a), "places", "place", release=RELEASE_A)
    overture.set_data_path(str(path_b), "places", "place", release=RELEASE_B)
    try:
        result = server.changes_in_area(
            min_lon=BBOX[0],
            min_lat=BBOX[1],
            max_lon=BBOX[2],
            max_lat=BBOX[3],
            from_release=RELEASE_A,
            to_release=RELEASE_B,
        )
    finally:
        overture.set_data_path(None, "places", "place", release=RELEASE_A)
        overture.set_data_path(None, "places", "place", release=RELEASE_B)

    assert result["counts"]["appeared"] == 12
    assert len(result["appeared"]) <= 8  # DEFAULT_DIGEST_TOP_N
    assert result["truncated"] is True


def test_limit_above_the_default_actually_widens_the_lists(tmp_path, monkeypatch):
    """limit > DEFAULT_DIGEST_TOP_N must flow through to the digest's
    top_n (capped at overture.MAX_ROWS) -- 'widen the limit' used to be a
    no-op above 8."""
    _stub_releases(monkeypatch, [RELEASE_A, RELEASE_B])
    con = duckdb.connect()
    rows_a = [_row("keep", 0.01, 0.01, "Keep", "cafe", 0.9)]
    rows_b = [_row("keep", 0.01, 0.01, "Keep", "cafe", 0.9)] + [
        _row(f"new{i}", 0.02 + i * 0.001, 0.02, f"New Place {i}", "cafe", 0.5 + i * 0.01)
        for i in range(12)
    ]
    path_a = tmp_path / "a.parquet"
    path_b = tmp_path / "b.parquet"
    _write_places_parquet(con, path_a, rows_a)
    _write_places_parquet(con, path_b, rows_b)
    overture.set_data_path(str(path_a), "places", "place", release=RELEASE_A)
    overture.set_data_path(str(path_b), "places", "place", release=RELEASE_B)
    try:
        result = server.changes_in_area(
            min_lon=BBOX[0],
            min_lat=BBOX[1],
            max_lon=BBOX[2],
            max_lat=BBOX[3],
            from_release=RELEASE_A,
            to_release=RELEASE_B,
            limit=12,
        )
    finally:
        overture.set_data_path(None, "places", "place", release=RELEASE_A)
        overture.set_data_path(None, "places", "place", release=RELEASE_B)

    assert result["counts"]["appeared"] == 12
    assert len(result["appeared"]) == 12
