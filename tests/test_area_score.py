"""Issue #349: score_locality — candidate localities scored against
free-text amenity requirements, with an honest measurable: false for
anything Overture cannot count (issue #348's divisions_in_polygon supplies
the candidates; this module scores each one).

Ground truth (mirrors test_compare_areas_priorities.py's NYC-cluster-vs-
Arctic fixture centers): the shared places fixture has 29 bank rows split
24 (NYC cluster, around CENTER_LAT/LON) / 5 (Arctic cluster, around
(78.0, 15.0)) and no "park" rows anywhere. Tests that need a park-adjacent
locality mutate a copy of the fixture, turning the NYC cluster's bank rows
into park rows — leaving the Arctic cluster's 5 banks untouched as the
"commercial, no parks" locality.
"""

import duckdb
import pytest

from placeroot import area_score, categories, gers, overture

from .conftest import CENTER_LAT, CENTER_LON, FIXTURE_PATH

ARCTIC_LAT, ARCTIC_LON = 78.0, 15.0

# Downtown's division_id (division_area's id == the divisions/type=division
# fixture's id for the same row — see tests/fixtures/divisions.parquet),
# located exactly at (CENTER_LAT, CENTER_LON).
DOWNTOWN_DIVISION_ID = "2835be088c8011a4aee3dff5cabbcf13"


def _park_adjacent_fixture(tmp_path) -> str:
    """Copy of the places fixture with the NYC cluster's banks turned into
    parks — a locality genuinely rich in "park" category rows, so a
    "parks" requirement has real signal to score against."""
    mutated = tmp_path / "park_adjacent.parquet"
    con = duckdb.connect()
    con.execute(
        f"""COPY (
            SELECT * REPLACE (
                CASE WHEN basic_category = 'bank' AND bbox.ymin BETWEEN 40 AND 41
                     THEN 'park' ELSE basic_category END AS basic_category,
                CASE WHEN taxonomy."primary" = 'bank' AND bbox.ymin BETWEEN 40 AND 41
                     THEN struct_pack("primary" := 'park', alternates := taxonomy.alternates)
                     ELSE taxonomy END AS taxonomy
            ) FROM read_parquet('{FIXTURE_PATH}')
        ) TO '{mutated}' (FORMAT PARQUET)"""
    )
    return str(mutated)


@pytest.fixture
def park_adjacent(tmp_path):
    overture.set_data_path(_park_adjacent_fixture(tmp_path))
    try:
        yield
    finally:
        overture.set_data_path(None)


def test_park_adjacent_scores_higher_than_commercial_on_parks(park_adjacent):
    park_side = area_score.score_locality(
        ["parks"], lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
    )
    commercial_side = area_score.score_locality(
        ["parks"], lat=ARCTIC_LAT, lon=ARCTIC_LON, radius_m=1000
    )
    park_req = park_side["requirements"][0]
    commercial_req = commercial_side["requirements"][0]

    assert park_req["measurable"] is True
    assert park_req["category"] == "park"
    assert park_req["count"] > 0
    assert commercial_req["measurable"] is True
    assert commercial_req["count"] == 0
    assert park_req["score"] > commercial_req["score"]
    assert commercial_req["score"] == 0.0
    assert park_side["overall_score"] > commercial_side["overall_score"]


def test_commercial_locality_scores_higher_on_gyms(park_adjacent):
    # The NYC cluster has 22 gyms untouched by the park mutation (only its
    # bank rows were turned into parks); the Arctic cluster has none at
    # all — the inverse comparison to the parks test above.
    gym_side = area_score.score_locality(
        ["gyms"], lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
    )
    no_gym_side = area_score.score_locality(
        ["gyms"], lat=ARCTIC_LAT, lon=ARCTIC_LON, radius_m=1000
    )
    assert gym_side["requirements"][0]["count"] == 22
    assert no_gym_side["requirements"][0]["count"] == 0
    assert gym_side["requirements"][0]["score"] > no_gym_side["requirements"][0]["score"]


def test_park_never_counts_parking(park_adjacent, tmp_path):
    """Same honesty rule as #354's compare_areas priorities: "park" must
    never count parking garages via substring matching."""
    mutated = tmp_path / "parking_not_park.parquet"
    con = duckdb.connect()
    con.execute(
        f"""COPY (
            SELECT * REPLACE (
                CASE WHEN basic_category = 'park' THEN 'parking' ELSE basic_category END
                    AS basic_category,
                CASE WHEN taxonomy."primary" = 'park'
                     THEN struct_pack("primary" := 'parking', alternates := taxonomy.alternates)
                     ELSE taxonomy END AS taxonomy
            ) FROM read_parquet('{overture._upstream_glob()}')
        ) TO '{mutated}' (FORMAT PARQUET)"""
    )
    overture.set_data_path(str(mutated))
    try:
        result = area_score.score_locality(
            ["parks"], lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
        )
        assert result["requirements"][0]["count"] == 0
    finally:
        overture.set_data_path(_park_adjacent_fixture(tmp_path))


def test_subjective_requirement_is_flagged_not_scored(park_adjacent):
    result = area_score.score_locality(
        ["quiet streets", "safe neighborhood", "good schools"],
        lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000,
    )
    for row in result["requirements"]:
        assert row["measurable"] is False
        assert "score" not in row
        assert row["note"]
    assert result["overall_score"] is None


def test_unmatched_category_text_is_flagged_not_scored(park_adjacent):
    result = area_score.score_locality(
        ["asdkjfhalskdjfh nonsense phrase"], lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
    )
    row = result["requirements"][0]
    assert row["measurable"] is False
    assert "taxonomy" in row["note"]


def test_mixed_measurable_and_unmeasurable_requirements(park_adjacent):
    result = area_score.score_locality(
        ["parks", "quiet streets"], lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
    )
    by_label = {r["label"]: r for r in result["requirements"]}
    assert by_label["parks"]["measurable"] is True
    assert by_label["quiet streets"]["measurable"] is False
    # overall_score only averages the measurable ones.
    assert result["overall_score"] == by_label["parks"]["score"]


def test_plural_requirement_resolves_to_the_correct_singular_slug(park_adjacent):
    # "parks" alone (whole-query tiers) matches an unrelated sibling slug
    # (mountain_bike_parks) before it matches plain "park" — the module
    # must prefer the higher-confidence singular candidate.
    assert categories.search_categories("parks", limit=1)[0]["slug"] != "park"
    result = area_score.score_locality(
        ["parks"], lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
    )
    assert result["requirements"][0]["category"] == "park"


def test_nearest_distance_reported_when_matches_exist(park_adjacent):
    result = area_score.score_locality(
        ["parks"], lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
    )
    row = result["requirements"][0]
    assert row["nearest_distance_m"] is not None
    assert row["nearest_distance_m"] >= 0


def test_division_id_resolves_locality_centroid(park_adjacent):
    result = area_score.score_locality(
        ["parks"], division_id=DOWNTOWN_DIVISION_ID, radius_m=1000
    )
    assert result["locality"]["division_id"] == DOWNTOWN_DIVISION_ID
    assert result["locality"]["lat"] == pytest.approx(CENTER_LAT, abs=0.01)
    assert result["locality"]["lon"] == pytest.approx(CENTER_LON, abs=0.01)
    assert result["requirements"][0]["count"] > 0


def test_division_id_not_found_raises(park_adjacent):
    with pytest.raises(area_score.LocalityNotFound):
        area_score.score_locality(
            ["parks"], division_id="0" * 32,
        )


def test_both_division_id_and_latlon_is_bad_request(park_adjacent):
    with pytest.raises(ValueError):
        area_score.score_locality(
            ["parks"], division_id=DOWNTOWN_DIVISION_ID, lat=CENTER_LAT, lon=CENTER_LON
        )


def test_neither_division_id_nor_latlon_is_bad_request(park_adjacent):
    with pytest.raises(ValueError):
        area_score.score_locality(["parks"])


def test_lat_without_lon_is_bad_request(park_adjacent):
    with pytest.raises(ValueError):
        area_score.score_locality(["parks"], lat=CENTER_LAT)


def test_out_of_range_coord_is_bad_request(park_adjacent):
    with pytest.raises(ValueError):
        area_score.score_locality(["parks"], lat=200.0, lon=CENTER_LON)


def test_empty_requirements_is_bad_request(park_adjacent):
    with pytest.raises(ValueError):
        area_score.score_locality([], lat=CENTER_LAT, lon=CENTER_LON)


def test_too_many_requirements_is_bad_request(park_adjacent):
    with pytest.raises(ValueError):
        area_score.score_locality(
            ["a"] * (area_score.MAX_REQUIREMENTS + 1), lat=CENTER_LAT, lon=CENTER_LON
        )


def test_blank_requirement_is_bad_request(park_adjacent):
    with pytest.raises(ValueError):
        area_score.score_locality(["parks", "   "], lat=CENTER_LAT, lon=CENTER_LON)


def test_degraded_category_columns_flag_measurable_false_not_zero(tmp_path):
    """Same honesty rule as #354's compare_areas verdict: a dataset with
    no category columns at all must not silently score every requirement
    as zero matches — it must say it could not measure them."""
    out = tmp_path / "no_category_columns.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * EXCLUDE (basic_category, taxonomy) FROM read_parquet("
        f"'{FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out))
    try:
        result = area_score.score_locality(
            ["parks"], lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
        )
        row = result["requirements"][0]
        assert row["measurable"] is False
        assert "degraded" in row["note"]
        assert result["overall_score"] is None
    finally:
        overture.set_data_path(None)


def test_honesty_note_present():
    result = area_score.score_locality(["parks"], lat=CENTER_LAT, lon=CENTER_LON)
    assert "honesty" in result
    assert result["honesty"]


def test_gers_lookup_hint_narrows_division_lookup(park_adjacent, monkeypatch):
    """near_lat/near_lon must reach gers.gers_lookup as a hint (issue #349:
    reuse the same bounded-lookup machinery every other tool uses)."""
    seen = {}
    original = gers.gers_lookup

    def spy(id, near_lat=None, near_lon=None):
        seen["near_lat"] = near_lat
        seen["near_lon"] = near_lon
        return original(id, near_lat=near_lat, near_lon=near_lon)

    monkeypatch.setattr(gers, "gers_lookup", spy)
    area_score.score_locality(
        ["parks"], division_id=DOWNTOWN_DIVISION_ID,
        near_lat=CENTER_LAT, near_lon=CENTER_LON,
    )
    assert seen["near_lat"] == CENTER_LAT
    assert seen["near_lon"] == CENTER_LON
