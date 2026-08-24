"""find_places answer diet: detail tiers + multi-category single-scan search
(ROADMAP §4.5 / roadmap feature 5).

detail="compact" is now the DEFAULT — a breaking change to find_places'
answer shape (see CHANGELOG). These tests exercise every tier plus the new
categories/group_by_category params, both in point+radius mode (against the
committed places fixture, via conftest's CENTER_LAT/CENTER_LON) and in
division-polygon mode (via test_find_places_in_area's synthetic fixture,
which has known coffee_shop/bank/bakery rows inside DIV_NOTCH).
"""

from placeroot import honesty, overture, server

from .conftest import CENTER_LAT, CENTER_LON
from .test_find_places_in_area import DIV_NOTCH, polygon_fixtures  # noqa: F401

_COMPACT_KEYS = {"id", "name", "category", "distance_m", "trust"}
_IDS_KEYS = {"id", "distance_m"}


def test_compact_is_the_default_detail():
    default = server.find_places(CENTER_LAT, CENTER_LON, radius_m=500, limit=5)
    explicit = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=500, limit=5, detail="compact"
    )
    assert default["results"] == explicit["results"]
    assert default["results"]
    for row in default["results"]:
        assert set(row) == _COMPACT_KEYS
        assert row["trust"] in honesty.TRUST_TIERS
        assert "trust_note" not in row
    assert default.get("trust_legend") == honesty.TRUST_LEGEND


def test_ids_detail_is_just_id_and_distance():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=500, limit=5, detail="ids")
    assert result["results"]
    for row in result["results"]:
        assert set(row) == _IDS_KEYS
    assert "trust_legend" not in result


def test_full_detail_is_unchanged_from_before_this_pr():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=500, limit=5, detail="full")
    assert result["results"]
    for row in result["results"]:
        assert "trust_note" in row
        assert "lat" in row and "lon" in row
        assert "confidence" in row
        assert "trust" not in row  # tier is a compact-only field
    assert "trust_legend" not in result


def test_unknown_detail_is_bad_request():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=500, detail="verbose")
    assert result["error"] == "bad_request"
    detail = result["detail"]
    assert "ids" in detail and "compact" in detail and "full" in detail


def test_division_mode_compact_rows_omit_distance_m(polygon_fixtures):  # noqa: F811
    result = server.find_places(division_id=DIV_NOTCH)
    assert result["results"]
    for row in result["results"]:
        assert "distance_m" not in row
        assert set(row) == {"id", "name", "category", "trust"}


def test_single_category_full_detail_matches_pre_diet_shape():
    """Regression: category= (singular) under detail="full" is byte-identical
    to what find_places always returned before this PR."""
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=500, category="coffee_shop", limit=5, detail="full"
    )
    raw_rows = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=500, category="coffee_shop", limit=5
    )
    assert result["results"] == raw_rows


# --- categories (multi-category, one scan) ---------------------------------


def test_categories_over_five_is_bad_request():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=500,
        categories=["a", "b", "c", "d", "e", "f"],
    )
    assert result["error"] == "bad_request"


def test_category_and_categories_together_is_bad_request():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=500, category="coffee_shop", categories=["bank"],
    )
    assert result["error"] == "bad_request"


def test_multi_category_merged_equals_union_of_single_category_queries():
    # radius_m=200 keeps the union well under both MAX_ROWS and the token
    # budget (detail="ids" too) so nothing here is truncated by either —
    # this is a pure "is it the same rows, nearest-first" check, not a
    # pagination one (that's covered separately below).
    merged = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=200,
        categories=["coffee_shop", "bank"], limit=25, detail="ids",
    )
    assert "error" not in merged
    assert "truncated" not in merged
    coffee = overture.find_places(
        CENTER_LAT, CENTER_LON, radius_m=200, category="coffee_shop", limit=25
    )
    bank = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=200, category="bank", limit=25)
    by_id = {r["id"]: r["distance_m"] for r in coffee + bank}
    expected_ids = sorted(by_id, key=lambda i: (by_id[i], i))
    got_ids = [r["id"] for r in merged["results"]]
    assert got_ids == expected_ids
    distances = [r["distance_m"] for r in merged["results"]]
    assert distances == sorted(distances)


def test_multi_category_hint_fires_only_when_every_slug_misses():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=50,
        categories=["definitely_not_a_category_xyz", "also_not_one_abc"],
    )
    assert result["results"] == []
    assert "note" in result
    assert "definitely_not_a_category_xyz" in result["note"]
    assert "also_not_one_abc" in result["note"]


def test_multi_category_division_mode(polygon_fixtures):  # noqa: F811
    result = server.find_places(
        division_id=DIV_NOTCH, categories=["coffee_shop", "bank"], detail="full"
    )
    assert "error" not in result
    names = {r["name"] for r in result["results"]}
    assert names == {"Inside Coffee", "Inside Bank"}


# --- group_by_category -------------------------------------------------


def test_group_by_category_requires_categories():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=500, group_by_category=True)
    assert result["error"] == "bad_request"


def test_group_by_category_with_cursor_is_bad_request():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=500,
        categories=["coffee_shop"], group_by_category=True, cursor="anything",
    )
    assert result["error"] == "bad_request"


def test_group_by_category_shapes_and_per_category_limit():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        categories=["coffee_shop", "bank"], group_by_category=True, limit=3, detail="full",
    )
    assert "error" not in result
    assert set(result["results"]) <= {"coffee_shop", "bank"}
    for slug, rows in result["results"].items():
        assert len(rows) <= 3
        assert all(r["category"] == slug for r in rows)
        # nearest-first within each category
        dists = [r["distance_m"] for r in rows]
        assert dists == sorted(dists)
    assert "cursor" not in result


def test_group_by_category_never_emits_a_cursor_even_when_capped():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        categories=["coffee_shop"], group_by_category=True, limit=1,
    )
    assert "error" not in result
    assert "cursor" not in result


def test_group_by_category_compact_detail():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        categories=["coffee_shop", "bank"], group_by_category=True, limit=2,
    )
    assert "error" not in result
    assert result.get("trust_legend") == honesty.TRUST_LEGEND
    for rows in result["results"].values():
        for row in rows:
            assert set(row) == _COMPACT_KEYS


# --- cursors: unaffected by detail, sensitive to categories -----------------


def test_cursor_continues_correctly_across_a_detail_change():
    first = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="coffee_shop", limit=5,
        detail="compact",
    )
    assert first.get("cursor")
    second = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="coffee_shop", limit=5,
        cursor=first["cursor"], detail="full",
    )
    assert "error" not in second
    assert second["results"]
    first_ids = {r["id"] for r in first["results"]}
    second_ids = {r["id"] for r in second["results"]}
    assert first_ids.isdisjoint(second_ids)
    # detail="full" rows on the second page carry the full field set.
    assert all("trust_note" in r for r in second["results"])


def test_cursor_with_changed_categories_is_bad_cursor():
    first = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        categories=["coffee_shop", "bank"], limit=3,
    )
    assert first.get("cursor")
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        categories=["coffee_shop"], limit=3, cursor=first["cursor"],
    )
    assert result["error"] == "bad_cursor"


def test_cursor_with_same_categories_reordered_still_works():
    """categories is sorted before hashing into the cursor's query identity,
    so a re-issued list in a different order is still the same query."""
    first = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        categories=["coffee_shop", "bank"], limit=3,
    )
    assert first.get("cursor")
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000,
        categories=["bank", "coffee_shop"], limit=3, cursor=first["cursor"],
    )
    assert "error" not in result
    assert result["results"]
