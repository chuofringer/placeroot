import duckdb

from placeroot import overture, server

from ._geo import haversine_m
from .conftest import CENTER_LAT, CENTER_LON, FIXTURE_PATH, raw_rows


def test_operating_status_is_relabelled():
    """operating_status is surfaced as business-lifecycle language, never the
    raw Overture "open" (which reads as "open right now" — we have no hours)."""
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    surfaced = {r["operating_status"] for r in results}
    assert "open" not in surfaced
    assert surfaced <= {"in business", "permanently closed", "temporarily closed", None}


def test_label_operating_status_maps_and_passes_through():
    assert overture._label_operating_status("open") == "in business"
    assert overture._label_operating_status("closed") == "permanently closed"
    assert overture._label_operating_status("closed_temporarily") == "temporarily closed"
    assert overture._label_operating_status(None) is None
    # Unrecognised values pass through unchanged — never misrepresent the source.
    assert overture._label_operating_status("some_future_value") == "some_future_value"


def test_limit_fill():
    """Regression for #1: a dense area with more than `limit` matches
    returns exactly `limit` rows, not fewer."""
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    assert len(results) == 10


def test_circle_excludes_bbox_corner():
    """Regression for #3: a place inside the square prefilter but outside
    the true circle must not appear, and a place just inside the circle
    must appear."""
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=500, category="novelty_shop", limit=10
    )
    names = {r["name"] for r in results}
    assert "Corner Test Place" not in names
    assert "Edge Test Place" in names


def test_distances_are_within_radius():
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=500, limit=25)
    assert results
    for r in results:
        assert r["distance_m"] <= 500


def test_results_sorted_nearest_first():
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    distances = [r["distance_m"] for r in results]
    assert distances == sorted(distances)


def test_category_filter_matches_ground_truth():
    radius_m = 1000
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=radius_m, category="coffee_shop", limit=25
    )
    expected = [
        row for row in raw_rows()
        if row["basic_category"] == "coffee_shop"
        and haversine_m(CENTER_LAT, CENTER_LON, row["lat"], row["lon"]) <= radius_m
    ]
    assert results
    assert len(results) == len(expected)
    assert all(r["basic_category"] == "coffee_shop" for r in results)


def test_name_filter():
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, name="Roastery", limit=25)
    assert len(results) == 1
    assert results[0]["name"] == "Blue Bottle Roastery"


def test_empty_result_far_from_any_fixture_data():
    results = overture.find_places(0.0, 0.0, radius_m=1000, limit=10)
    assert results == []


def test_high_latitude_cluster_is_findable():
    results = overture.find_places(78.0, 15.0, radius_m=1000, limit=25)
    assert len(results) == 5
    assert all(r["distance_m"] <= 1000 for r in results)


def test_ids_present_and_stable_across_identical_queries():
    """Regression for #25: every result carries a GERS id, and it's stable
    across two identical queries — an agent can hold onto it across turns."""
    first = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    second = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    assert all(r["id"] for r in first)
    assert [r["id"] for r in first] == [r["id"] for r in second]


def test_ids_match_ground_truth():
    expected = {row["id"] for row in raw_rows()}
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert all(r["id"] in expected for r in results)


def test_bogus_category_gets_hint_note():
    """Regression for #117: a wrong/invalid Overture category slug should
    not look identical to "this area genuinely has none of that category" —
    the server tool adds a non-fatal note pointing at search_categories."""
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="definitely_not_a_category_xyz", limit=10
    )
    assert result["results"] == []
    assert "note" in result
    assert "search_categories" in result["note"]


def test_valid_category_with_matches_has_no_note():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="coffee_shop", limit=25
    )
    assert result["results"]
    assert "note" not in result


def test_no_category_with_matches_has_no_note():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert result["results"]
    assert "note" not in result


def test_no_category_empty_area_has_no_note():
    """The hint is scoped to the category-filter case only — an empty area
    with no category filter at all is a plain empty result, not a hint."""
    result = server.find_places(0.0, 0.0, radius_m=1000, limit=10)
    assert result["results"] == []
    assert "note" not in result


# --- min_confidence / operating_status filters (issue #127) ----------------
# Ground truth computed directly from the fixture: within 100m of CENTER,
# the dense cluster has 13 rows — 10 raw "open" and 3 raw "closed_permanently"
# — and exactly 5 rows with confidence >= 0.8. Small enough that MAX_ROWS
# (25) never truncates these queries, so exact-count assertions are safe.


def _raw_rows_with_confidence_and_status():
    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT id, bbox.ymin AS lat, bbox.xmin AS lon, confidence, operating_status
        FROM read_parquet('{FIXTURE_PATH}')
    """).fetchall()
    cols = ["id", "lat", "lon", "confidence", "operating_status"]
    return [dict(zip(cols, r)) for r in rows]


def _within(radius_m):
    return [
        row for row in _raw_rows_with_confidence_and_status()
        if haversine_m(CENTER_LAT, CENTER_LON, row["lat"], row["lon"]) <= radius_m
    ]


def test_min_confidence_filters_out_low_confidence_rows():
    expected_ids = {r["id"] for r in _within(100) if r["confidence"] >= 0.8}
    assert len(expected_ids) == 5

    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=100, min_confidence=0.8, limit=25
    )
    assert {r["id"] for r in results} == expected_ids
    assert all(r["confidence"] >= 0.8 for r in results)


def test_min_confidence_out_of_range_is_bad_request():
    for bad in (1.5, -0.1):
        result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, min_confidence=bad)
        assert result["error"] == "bad_request"


def test_operating_status_relabeled_input_matches_raw_open():
    expected_ids = {r["id"] for r in _within(100) if r["operating_status"] == "open"}
    assert len(expected_ids) == 10

    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=100, operating_status="in business", limit=25
    )
    assert {r["id"] for r in results} == expected_ids
    assert all(r["operating_status"] == "in business" for r in results)


def test_operating_status_accepts_raw_value_equivalently():
    relabeled = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=100, operating_status="in business", limit=25
    )
    raw = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=100, operating_status="open", limit=25
    )
    assert {r["id"] for r in relabeled} == {r["id"] for r in raw}


def test_operating_status_permanently_closed_matches_raw_closed_permanently():
    expected_ids = {
        r["id"] for r in _within(100) if r["operating_status"] == "closed_permanently"
    }
    assert len(expected_ids) == 3

    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=100, operating_status="permanently closed", limit=25
    )
    assert {r["id"] for r in results} == expected_ids


def test_unknown_operating_status_is_bad_request():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, operating_status="banana")
    assert result["error"] == "bad_request"


def test_min_confidence_and_operating_status_compose_with_category():
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="coffee_shop",
        min_confidence=0.5, operating_status="in business", limit=25,
    )
    assert results
    for r in results:
        assert r["basic_category"] == "coffee_shop"
        assert r["confidence"] >= 0.5
        assert r["operating_status"] == "in business"


def test_min_confidence_and_operating_status_are_noop_when_columns_missing(tmp_path):
    """Filters degrade gracefully (no error, no filtering) when the column
    they'd filter on is absent from the active dataset — consistent with how
    category/name already degrade."""
    out = tmp_path / "missing_confidence_and_status.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * EXCLUDE (confidence, operating_status) "
        f"FROM read_parquet('{FIXTURE_PATH}')) TO '{out}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(out))
    try:
        results = overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=100,
            min_confidence=0.99, operating_status="in business", limit=25,
        )
        assert len(results) == 13  # every row within 100m — filters were no-ops
        assert all(r["confidence"] is None for r in results)
        assert all(r["operating_status"] is None for r in results)
    finally:
        overture.set_data_path(str(FIXTURE_PATH))


def test_rows_carry_brand_and_presence_flags():
    """Issue #128: every row now carries brand (str-or-None) and the
    has_website/has_phone presence booleans, even when they're all absent."""
    results = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=25)
    assert results
    for r in results:
        assert "brand" in r
        assert r["brand"] is None or isinstance(r["brand"], str)
        assert isinstance(r["has_website"], bool)
        assert isinstance(r["has_phone"], bool)
    # The fixture's one fully-populated row (Blue Bottle Roastery) proves the
    # flags/brand aren't just always-empty stand-ins. It's part of a 200-row
    # dense cluster, so a name filter (not a bare top-25 by distance) is
    # needed to guarantee it's the row that comes back.
    roastery = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, name="Roastery", limit=25
    )[0]
    assert roastery["name"] == "Blue Bottle Roastery"
    assert roastery["brand"] == "Blue Bottle Coffee"
    assert roastery["has_website"] is True
    assert roastery["has_phone"] is True


def test_brand_filter_matches_only_that_brand():
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, brand="Blue Bottle Coffee", limit=25
    )
    assert len(results) == 1
    assert results[0]["name"] == "Blue Bottle Roastery"
    assert results[0]["brand"] == "Blue Bottle Coffee"


def test_brand_filter_no_match_returns_empty():
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, brand="Nonexistent Chain Inc", limit=25
    )
    assert results == []


def test_has_website_true_returns_only_places_with_a_website():
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, has_website=True, limit=25
    )
    names = {r["name"] for r in results}
    assert names == {"Blue Bottle Roastery", "Cluster Place 010"}
    assert all(r["has_website"] is True for r in results)


def test_has_website_false_excludes_places_with_a_website():
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, has_website=False, limit=25
    )
    names = {r["name"] for r in results}
    assert "Blue Bottle Roastery" not in names
    assert "Cluster Place 010" not in names
    assert all(r["has_website"] is False for r in results)


def test_has_phone_true_returns_only_places_with_a_phone():
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, has_phone=True, limit=25
    )
    assert len(results) == 1
    assert results[0]["name"] == "Blue Bottle Roastery"
    assert results[0]["has_phone"] is True


def test_has_phone_false_excludes_places_with_a_phone():
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, has_phone=False, limit=25
    )
    assert all(r["name"] != "Blue Bottle Roastery" for r in results)
    # Cluster Place 010 has a website but no phone, so has_phone=False
    # (narrowed with a name filter — it's one of 200 in a dense cluster,
    # not guaranteed to land in a bare top-25-by-distance) still includes it.
    narrowed = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        name="Cluster Place 010", has_phone=False, limit=25,
    )
    assert len(narrowed) == 1
    assert narrowed[0]["name"] == "Cluster Place 010"


def test_brand_composes_with_category():
    """brand + category both apply — Blue Bottle Roastery's basic_category
    is 'bar' in the fixture (synthetic data), so pairing brand with the
    right category narrows to it, and the wrong category excludes it."""
    matching = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        brand="Blue Bottle Coffee", category="bar", limit=25,
    )
    assert len(matching) == 1
    assert matching[0]["name"] == "Blue Bottle Roastery"

    non_matching = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        brand="Blue Bottle Coffee", category="coffee_shop", limit=25,
    )
    assert non_matching == []


def test_has_website_composes_with_category():
    """has_website + category both apply — only Cluster Place 010
    (grocery_store, has a website) should match; Blue Bottle Roastery is
    excluded here because its category is 'bar', not 'grocery_store'."""
    results = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        has_website=True, category="grocery_store", limit=25,
    )
    assert len(results) == 1
    assert results[0]["name"] == "Cluster Place 010"


def test_brand_and_presence_filters_noop_when_columns_missing(tmp_path):
    """Issue #128: with brand/websites/phones missing from the dataset, the
    filters no-op (never raise/exclude everything) and rows come back with
    brand=None / has_website=False / has_phone=False rather than erroring —
    mirrors the degraded-column pattern in test_resilience.py."""
    con = duckdb.connect()
    degraded = tmp_path / "missing_brand_websites_phones.parquet"
    con.execute(
        "COPY (SELECT * EXCLUDE (brand, websites, phones) "
        f"FROM read_parquet('{FIXTURE_PATH}')) TO '{degraded}' (FORMAT PARQUET)"
    )
    overture.set_data_path(str(degraded))
    try:
        results = overture.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000,
            brand="Blue Bottle Coffee", has_website=True, has_phone=True, limit=25,
        )
        # The filters no-op on missing columns, so the radius search still
        # returns its normal full set of matches rather than an empty list.
        assert results
        assert all(r["brand"] is None for r in results)
        assert all(r["has_website"] is False for r in results)
        assert all(r["has_phone"] is False for r in results)
        assert overture.degraded_fields() == ["websites", "phones", "brand"] or set(
            overture.degraded_fields()
        ) == {"brand", "websites", "phones"}
    finally:
        overture.set_data_path(None)
