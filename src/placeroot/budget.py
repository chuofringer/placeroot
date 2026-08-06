"""Token-budget enforcement for tool responses.

Every tool answer should fit an agent's working context — see the "answers,
not data" rule in ROADMAP.md. We estimate size with a chars/4 heuristic:
not a real tokenizer, but a dependency-free approximation close enough for
a soft budget, and cheap enough to run on every response.

Truncation is deterministic: drop the lowest-ranked rows first (callers
pass rows best-first, e.g. nearest-first or highest-count-first), then, if
a single row still doesn't fit, strip optional fields by priority.
"""

import json
import os

DEFAULT_TOKEN_BUDGET = 2000

# Stripped from rows, in this order, if dropping rows alone isn't enough
# to fit a single remaining row within budget.
OPTIONAL_FIELD_PRIORITY = ["confidence", "operating_status", "category", "basic_category"]


def token_budget() -> int:
    """Budget in estimated tokens, overridable via PLACEROOT_TOKEN_BUDGET."""
    return int(os.environ.get("PLACEROOT_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET))


def estimate_tokens(obj) -> int:
    """chars/4 heuristic for estimated token count of obj's JSON form."""
    return len(json.dumps(obj, default=str)) // 4


def fit_rows(
    rows: list[dict],
    budget_tokens: int,
    optional_fields: list[str] = OPTIONAL_FIELD_PRIORITY,
) -> tuple[list[dict], bool, int]:
    """Trim a best-first ranked list of rows to fit budget_tokens.

    Returns (kept_rows, truncated, omitted_count). omitted_count counts only
    dropped rows; stripping optional fields (when even one row overflows the
    budget) also sets truncated but isn't counted as an omission.
    """
    kept = list(rows)
    while len(kept) > 1 and estimate_tokens(kept) > budget_tokens:
        kept.pop()
    omitted = len(rows) - len(kept)

    stripped = False
    for field in optional_fields:
        if estimate_tokens(kept) <= budget_tokens:
            break
        if any(field in row for row in kept):
            kept = [{k: v for k, v in row.items() if k != field} for row in kept]
            stripped = True

    return kept, omitted > 0 or stripped, omitted


def truncate_list(items: list | None, max_items: int = 5) -> tuple[list | None, int]:
    """Truncate a single field's array value to max_items, never silently.

    Returns (kept, omitted_count). None/empty pass through unchanged with
    omitted_count 0. Used for place_details' array fields (addresses,
    websites, phones, socials, sources, issue #9) — those can be long
    enough on their own to blow the token budget, and unlike find_places'
    row lists, apply_budget's row-dropping doesn't apply to a single
    place's fields, so this is the array-level equivalent: callers surface
    omitted_count > 0 as "<field>_omitted_count" on the response.
    """
    if not items or len(items) <= max_items:
        return items, 0
    return items[:max_items], len(items) - max_items


def apply_budget(payload: dict, list_key: str, budget_tokens: int | None = None) -> dict:
    """Fit payload[list_key] — a ranked list — within the token budget.

    Returns a new dict. Adds truncated=True and omitted_count only when
    something was actually dropped or stripped; an untruncated response
    carries neither key.
    """
    budget = token_budget() if budget_tokens is None else budget_tokens
    envelope_tokens = estimate_tokens({**payload, list_key: []})
    kept, truncated, omitted = fit_rows(payload[list_key], max(budget - envelope_tokens, 0))
    result = {**payload, list_key: kept}
    if truncated:
        result["truncated"] = True
        result["omitted_count"] = omitted
    return result
