import json

from placeroot import budget


def _row(i):
    return {
        "name": f"Place {i}",
        "category": "coffee_shop",
        "basic_category": "coffee_shop",
        "operating_status": "open",
        "confidence": 0.9,
        "lat": 40.7,
        "lon": -73.9,
        "distance_m": i,
    }


def test_estimate_tokens_is_chars_over_4():
    obj = {"a": "bcd", "n": 42}
    assert budget.estimate_tokens(obj) == len(json.dumps(obj)) // 4


def test_fit_rows_drops_lowest_ranked_first():
    rows = [_row(i) for i in range(200)]  # best-first: index 0 is nearest
    kept, truncated, omitted = budget.fit_rows(rows, budget_tokens=200)
    assert truncated
    assert omitted == len(rows) - len(kept)
    # survivors must be a prefix of the original ranked list (nearest kept)
    assert [r["distance_m"] for r in kept] == list(range(len(kept)))


def test_fit_rows_under_budget_is_untouched():
    rows = [_row(i) for i in range(3)]
    kept, truncated, omitted = budget.fit_rows(rows, budget_tokens=10_000)
    assert kept == rows
    assert not truncated
    assert omitted == 0


def test_fit_rows_strips_optional_fields_when_single_row_overflows():
    # A worst-case row: even alone, it doesn't fit a tiny budget.
    huge_row = {**_row(0), "name": "X" * 500}
    kept, truncated, omitted = budget.fit_rows([huge_row], budget_tokens=50)
    assert truncated
    assert omitted == 0  # the row itself wasn't dropped, only fields were stripped
    assert len(kept) == 1
    assert "confidence" not in kept[0]
    assert "name" in kept[0]  # required fields are never stripped


def test_apply_budget_adds_metadata_only_when_truncated():
    payload = {"center": {"lat": 0, "lon": 0}, "results": [_row(i) for i in range(200)]}
    result = budget.apply_budget(payload, "results", budget_tokens=200)
    assert result["truncated"] is True
    assert result["omitted_count"] > 0

    small_payload = {"center": {"lat": 0, "lon": 0}, "results": [_row(0)]}
    small_result = budget.apply_budget(small_payload, "results", budget_tokens=10_000)
    assert "truncated" not in small_result
    assert "omitted_count" not in small_result


def test_token_budget_env_override(monkeypatch):
    monkeypatch.setenv("PLACEROOT_TOKEN_BUDGET", "123")
    assert budget.token_budget() == 123
    monkeypatch.delenv("PLACEROOT_TOKEN_BUDGET")
    assert budget.token_budget() == budget.DEFAULT_TOKEN_BUDGET


def test_default_budget_holds_for_realistic_worst_case_row():
    # A single row with maximal but realistic field lengths (long name,
    # long category strings) must fit the default budget after stripping
    # optional fields — the tool boundary should not need to touch required
    # fields (name, lat, lon, distance_m) for ordinary data.
    worst_row = {
        "name": "X" * 200,
        "category": "Y" * 80,
        "basic_category": "Z" * 80,
        "operating_status": "open",
        "confidence": 0.99,
        "lat": 40.7,
        "lon": -73.9,
        "distance_m": 100,
    }
    before = budget.estimate_tokens([worst_row])
    kept, truncated, omitted = budget.fit_rows([worst_row], budget.DEFAULT_TOKEN_BUDGET)
    assert omitted == 0
    assert budget.estimate_tokens(kept) <= before
    assert budget.estimate_tokens(kept) <= budget.DEFAULT_TOKEN_BUDGET


def _fit_rows_reference(rows, budget_tokens, optional_fields=budget.OPTIONAL_FIELD_PRIORITY):
    """The pre-optimisation fit_rows, verbatim: re-serialises the whole list
    on every pop. Kept here as the oracle the O(n) version is held to."""
    kept = list(rows)
    while len(kept) > 1 and budget.estimate_tokens(kept) > budget_tokens:
        kept.pop()
    omitted = len(rows) - len(kept)

    stripped = False
    for field in optional_fields:
        if budget.estimate_tokens(kept) <= budget_tokens:
            break
        if any(field in row for row in kept):
            kept = [{k: v for k, v in row.items() if k != field} for row in kept]
            stripped = True

    return kept, omitted > 0 or stripped, omitted


def test_fit_rows_matches_the_pop_loop_reference_on_random_inputs():
    import random

    rng = random.Random(20261010)
    for _ in range(60):
        n = rng.randint(0, 40)
        rows = []
        for i in range(n):
            row = _row(i)
            row["name"] = "x" * rng.randint(0, 120)
            if rng.random() < 0.3:
                row["website"] = "https://example.com/" + "y" * rng.randint(0, 80)
            if rng.random() < 0.3:
                row["unicode"] = "caf\u00e9 \u2014 " * rng.randint(0, 5)
            if rng.random() < 0.2:
                del row["confidence"]
            rows.append(row)
        # Budgets from "nothing fits" through "everything fits", including
        # exact boundaries around the full-list size.
        full = budget.estimate_tokens(rows)
        for b in (0, 1, 7, 50, 200, full - 1, full, full + 1, 10_000):
            if b < 0:
                continue
            assert budget.fit_rows(rows, b) == _fit_rows_reference(rows, b), (n, b)


def test_fit_rows_empty_input():
    assert budget.fit_rows([], 100) == ([], False, 0)
