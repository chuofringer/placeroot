"""Stateless pagination cursors on find_places/find_near (ROADMAP §4.4)."""

from placeroot import budget, overture, release, server
from placeroot import cursor as cursor_mod

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
            "category": _CATEGORY, "name": None, "categories": None,
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


def test_rewind_cursor_subtracts_offset_keeping_q_and_r():
    original = cursor_mod.encode_cursor({"category": "coffee_shop"}, "2026-08-19.0", 10)
    rewound = cursor_mod.rewind_cursor(original, 3)
    before = cursor_mod.decode_cursor(original)
    after = cursor_mod.decode_cursor(rewound)
    assert after["o"] == 7
    assert after["q"] == before["q"]
    assert after["r"] == before["r"]


def test_rewind_cursor_floors_at_zero():
    original = cursor_mod.encode_cursor({"category": "coffee_shop"}, "2026-08-19.0", 2)
    rewound = cursor_mod.rewind_cursor(original, 10)
    assert cursor_mod.decode_cursor(rewound)["o"] == 0


def test_rewind_cursor_returns_none_for_garbage():
    assert cursor_mod.rewind_cursor("not-a-real-cursor!!", 1) is None


def test_find_near_rewinds_cursor_when_its_own_second_budget_pass_drops_a_row(monkeypatch):
    """Regression: find_places' apply_budget pass computes the cursor's
    offset from the rows *it* delivered, but find_near then re-projects
    those rows to a compact shape and runs budget.apply_budget a SECOND
    time on the result (server.py, end of find_near). If that second pass
    ever drops a row find_places had already counted into the offset, the
    next page would silently skip it — the cursor's whole contract is
    "never skip a row". This forces that second pass to drop exactly one
    row (find_near's own compacted rows are normally smaller than
    find_places', so this practically never trims further today) and
    checks the emitted cursor is rewound to compensate.
    """
    # Ground truth, unpatched: what find_near actually delivers on a normal
    # first page, so we know which row the forced extra drop below removes.
    unpatched_first = server.find_near(
        "coffee_shop", "Blue Bottle Roastery", radius_m=1000, limit=5
    )
    dropped_id = unpatched_first["results"][4]["id"]  # the 5th (last) row of a normal page

    real_apply_budget = budget.apply_budget
    drop_calls = {"n": 0}

    def fake_apply_budget(payload, list_key, budget_tokens=None):
        # Only intercept find_near's own final pass — identifiable by the
        # "near" key its projected envelope carries, which find_places'
        # own apply_budget call never sees.
        if list_key == "results" and isinstance(payload, dict) and "near" in payload:
            drop_calls["n"] += 1
            trimmed = dict(payload)
            trimmed["results"] = list(payload["results"])[:-1]
            trimmed["truncated"] = True
            trimmed["omitted_count"] = payload.get("omitted_count", 0) + 1
            return trimmed
        return real_apply_budget(payload, list_key, budget_tokens)

    # server.py does `from placeroot import budget` and calls
    # budget.apply_budget(...) — same module object as `budget` imported
    # above, so patching it here is enough. Scoped to just this call: the
    # continuation call below must go through the REAL apply_budget, or it
    # would drop its own last row too and this test couldn't tell a
    # correctly-rewound cursor from one that just got lucky.
    with monkeypatch.context() as m:
        m.setattr(budget, "apply_budget", fake_apply_budget)
        first = server.find_near("coffee_shop", "Blue Bottle Roastery", radius_m=1000, limit=5)

    assert drop_calls["n"] >= 1
    assert first["truncated"] is True
    assert "cursor" in first
    # find_places itself would have set offset=5 (5 rows delivered pre-drop);
    # find_near's forced extra drop of 1 row must rewind that to 4, matching
    # the 4 rows the caller actually received.
    assert len(first["results"]) == 4
    decoded = cursor_mod.decode_cursor(first["cursor"])
    assert decoded["o"] == 4
    assert dropped_id not in {r["id"] for r in first["results"]}

    # Continuing from the rewound cursor (real apply_budget now) must not
    # skip the row the forced second pass dropped: it reappears as the
    # first row of the next page.
    second = server.find_near(
        "coffee_shop", "Blue Bottle Roastery", radius_m=1000, limit=5, cursor=first["cursor"]
    )
    assert "error" not in second
    second_ids = [r["id"] for r in second["results"]]
    assert second_ids[0] == dropped_id


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
