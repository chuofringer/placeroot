"""Stateless pagination cursors on find_places/find_near (ROADMAP §4.4)."""

from placeroot import cursor as cursor_mod
from placeroot import overture, release, server

from .conftest import CENTER_LAT, CENTER_LON
from .test_find_places_in_area import DIV_NOTCH, polygon_fixtures  # noqa: F401

# coffee_shop within 1000m of the fixture center: 23 matches (< MAX_ROWS=25),
# so a query-layer call with limit=25 returns the whole set — the ground
# truth every pagination test below walks toward. Going through
# overture.find_places directly (rather than the find_places tool) skips
# budget.apply_budget's token trimming, which would otherwise drop a row or
# two of this same set under the default token budget and make it a moving
# target.
_CATEGORY = "coffee_shop"


def _all_coffee_shop_ids() -> list[str]:
    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=25)
    assert len(rows) == 23
    return [r["id"] for r in rows]


def test_truncated_answer_carries_a_decodable_cursor():
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5)
    assert result["truncated"] is True
    assert "cursor" in result
    decoded = cursor_mod.decode_cursor(result["cursor"])
    assert decoded is not None
    assert decoded["r"] == release.PINNED_RELEASE
    assert decoded["o"] == 5


def test_replaying_cursor_returns_next_rows_no_overlap_stable_order():
    first = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5)
    second = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5,
        cursor=first["cursor"],
    )
    assert "error" not in second
    first_ids = [r["id"] for r in first["results"]]
    second_ids = [r["id"] for r in second["results"]]
    assert len(second_ids) == 5
    assert set(first_ids).isdisjoint(second_ids)

    # Walking every page to exhaustion reconstructs exactly the single
    # big-limit call's result set, in the same order.
    all_ids = list(first_ids)
    page = second
    while True:
        all_ids.extend(r["id"] for r in page["results"])
        if "cursor" not in page:
            break
        page = server.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5,
            cursor=page["cursor"],
        )
        assert "error" not in page

    assert all_ids == _all_coffee_shop_ids()
    assert len(all_ids) == len(set(all_ids))  # no duplicate anywhere across pages


def test_final_page_carries_no_cursor():
    # 23 matches, page size 5 -> pages of 5,5,5,5,3; the last page (offset
    # 20) is the final one and must not carry a cursor.
    page = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5)
    for _ in range(3):
        page = server.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5,
            cursor=page["cursor"],
        )
    # This is the 5th and final page.
    last = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5,
        cursor=page["cursor"],
    )
    assert len(last["results"]) == 3
    assert "truncated" not in last
    assert "cursor" not in last


def test_garbage_cursor_is_bad_cursor():
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5,
        cursor="not-a-valid-cursor!!",
    )
    assert result["error"] == "bad_cursor"


def test_garbage_cursor_variants():
    for garbage in ["", "===", "eyJub3QiOiJhIGN1cnNvciJ9", "12345", "{}"]:
        result = server.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5,
            cursor=garbage,
        )
        assert result.get("error") == "bad_cursor", garbage


def test_cursor_with_changed_params_is_bad_cursor_with_reissue_hint():
    first = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5)
    # Different category -> different query -> the cursor must not apply.
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category="bank", limit=5,
        cursor=first["cursor"],
    )
    assert result["error"] == "bad_cursor"
    assert "different query" in result["detail"]
    assert "re-issue" in result["detail"]

    # Different radius_m -> also a different query.
    result2 = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=2000, category=_CATEGORY, limit=5,
        cursor=first["cursor"],
    )
    assert result2["error"] == "bad_cursor"


def test_cursor_from_a_different_release_is_served_with_honesty_note():
    first = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5)
    decoded = cursor_mod.decode_cursor(first["cursor"])
    stale = cursor_mod.encode_cursor(
        {
            "lat": float(CENTER_LAT), "lon": float(CENTER_LON), "radius_m": 1000.0,
            "category": _CATEGORY, "name": None,
            "min_confidence": None, "operating_status": None, "brand": None,
            "has_website": None, "has_phone": None,
            "division_id": None, "area": None,
        },
        "2020-01-01.0",
        decoded["o"],
    )
    result = server.find_places(
        CENTER_LAT, CENTER_LON, radius_m=1000, category=_CATEGORY, limit=5,
        cursor=stale,
    )
    assert "error" not in result
    assert len(result["results"]) == 5
    assert "note" in result
    assert "2020-01-01.0" in result["note"]
    assert release.PINNED_RELEASE in result["note"]


def test_division_path_pagination_walks_to_the_same_set(polygon_fixtures):  # noqa: F811
    # DIV_NOTCH's polygon body (test_find_places_in_area.py) contains 5
    # places: inside/coffee/bank/chain/faint. name/id order is already
    # deterministic (ORDER BY name, id) — paging with limit=2 must walk
    # through all 5 with no overlap or gap, same as the point path.
    expected = {r["id"] for r in overture.find_places_in_division(DIV_NOTCH, limit=25)}
    assert len(expected) == 5

    seen: set[str] = set()
    page = server.find_places(division_id=DIV_NOTCH, limit=2)
    while True:
        ids = {r["id"] for r in page["results"]}
        assert seen.isdisjoint(ids)
        seen |= ids
        if "cursor" not in page:
            break
        page = server.find_places(division_id=DIV_NOTCH, limit=2, cursor=page["cursor"])
        assert "error" not in page
    assert seen == expected


def test_find_near_end_to_end_continuation():
    # "Blue Bottle Roastery" is a fixture place close enough to the coffee
    # cluster around CENTER that a 1km search from it also truncates at
    # limit=5 — the same overfetch-by-one detection as the point path,
    # exercised through find_near's delegation to find_places.
    first = server.find_near("coffee_shop", "Blue Bottle Roastery", radius_m=1000, limit=5)
    assert "error" not in first
    assert first["truncated"] is True
    assert "cursor" in first
    second = server.find_near(
        "coffee_shop", "Blue Bottle Roastery", radius_m=1000, limit=5, cursor=first["cursor"]
    )
    assert "error" not in second
    first_ids = {r["id"] for r in first["results"]}
    second_ids = {r["id"] for r in second["results"]}
    assert len(second_ids) == 5
    assert first_ids.isdisjoint(second_ids)
