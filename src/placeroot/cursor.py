"""Stateless pagination cursors for truncated list answers (ROADMAP §4.4).

PlaceRoot's answers come from a pinned, immutable Overture release, so a
continuation cursor doesn't need any server-side session state — it only
needs to name (a) which query it continues, (b) which release it was issued
against, and (c) how many rows of that query were already delivered. That
triple is exactly `{"q": <query-hash>, "r": <release>, "o": <offset>}`,
url-safe-base64 encoded. `encode_cursor`/`decode_cursor` are the primitives;
`resolve_cursor` turns an incoming `cursor` argument into a validated
starting offset (or a `bad_cursor` error); `attach_cursor` turns a tool's
finished payload — after `budget.apply_budget` has already trimmed it for
the token budget — into one that also carries `cursor` when there's more to
see.

query-hash covers everything that affects the result set/order EXCEPT the
cursor itself and `limit` (limit is how many rows one page holds, not which
query it is — a client should be able to page through the same query with a
different page size without invalidating cursors already issued... except
we don't actually promise that here: callers pass the same params_key shape
each time, so in practice limit is simply never included in it).

A cursor is only ever a hint about where to resume, never a trust
boundary: `resolve_cursor` validates the query-hash so a cursor for query A
can't silently be replayed against query B (wrong offset, misleading
answer) — that comes back as `bad_cursor` with a re-issue hint. A cursor
whose release doesn't match the currently active one is NOT an error
(ROADMAP's honesty rule): the query is served against the current release
and the payload gets a one-line note that rows may have shifted, rather
than failing a continuation just because a monthly Overture release landed
in between.

Reusable across every list tool that ranks and truncates rows the same way
`find_places` does — this PR only wires up `find_places`/`find_near`
(ROADMAP feature 4's explicit scope); `geocode`, `within_distance`,
`water_near`, `changes_in_area` and friends can adopt `resolve_cursor`/
`attach_cursor` later without needing new primitives.
"""

import base64
import hashlib
import json


def _params_hash(params_key: dict) -> str:
    """Short, stable hash over the params that determine a query's result set/order."""
    canonical = json.dumps(params_key, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _encode(q: str, r: str, o: int) -> str:
    """Low-level encoder shared by encode_cursor (hashes params_key into q)
    and rewind_cursor (reuses an already-decoded q verbatim, unhashed)."""
    payload = {"q": q, "r": r, "o": o}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def encode_cursor(params_key: dict, release: str, offset: int) -> str:
    """Opaque continuation token: url-safe base64 of `{"q", "r", "o"}`."""
    return _encode(_params_hash(params_key), release, offset)


def decode_cursor(s: str) -> dict | None:
    """Decode a cursor string. Returns None for anything malformed — never raises.

    A hand-crafted or truncated string, wrong JSON shape, or a non-string/
    negative offset all come back as None rather than an exception, so
    callers can turn any of them into the same bad_cursor error.
    """
    if not isinstance(s, str) or not s:
        return None
    try:
        padded = s + "=" * (-len(s) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    q, r, o = payload.get("q"), payload.get("r"), payload.get("o")
    if not isinstance(q, str) or not q or not isinstance(r, str) or not r:
        return None
    if not isinstance(o, int) or isinstance(o, bool) or o < 0:
        return None
    return {"q": q, "r": r, "o": o}


def resolve_cursor(
    cursor: str | None, params_key: dict, current_release: str
) -> tuple[dict | None, int, str | None]:
    """Validate an incoming `cursor` argument against the query about to run.

    Returns (error, offset, note):
    - error is None when the cursor is usable (including cursor=None, the
      first-page case), or a `{"error": "bad_cursor", "detail": ...}`
      envelope the caller should return as-is.
    - offset is the row offset to resume from — always 0 when error is set.
    - note is a one-line honesty note when the cursor's release differs from
      current_release: the query still runs (against the fresher release),
      it just says so, per ROADMAP §4.4's release-mismatch rule.
    """
    if cursor is None:
        return None, 0, None
    decoded = decode_cursor(cursor)
    if decoded is None:
        return {"error": "bad_cursor", "detail": "cursor is malformed or unrecognized"}, 0, None
    if decoded["q"] != _params_hash(params_key):
        return (
            {
                "error": "bad_cursor",
                "detail": "cursor was issued for a different query; re-issue without cursor",
            },
            0,
            None,
        )
    note = None
    if decoded["r"] != current_release:
        note = (
            f"cursor was issued against release {decoded['r']}; recomputed against "
            f"{current_release} — rows may have shifted"
        )
    return None, decoded["o"], note


def rewind_cursor(cursor: str, by: int) -> str | None:
    """Subtract `by` from a cursor's offset, re-encoding with the same q/r.

    For a caller that attaches a cursor via `attach_cursor` and then runs a
    *second* trimming pass over the same rows (find_near projects
    find_places' rows to a compact shape and re-runs budget.apply_budget on
    the result) — if that second pass drops rows find_places already
    counted into the cursor's offset, the offset now overcounts what the
    caller actually delivered, and the next page would silently skip the
    dropped rows. Rewinding by however many rows the second pass dropped
    keeps the cursor's contract ("never skip a row") intact regardless of
    how many trimming passes a payload goes through before it reaches the
    caller.

    Floors at 0 (never a negative offset). Returns None — never raises — on
    an undecodable cursor, so a caller can treat that the same as any other
    unusable cursor rather than propagating an exception from a place that
    is itself a bug-defense measure.
    """
    decoded = decode_cursor(cursor)
    if decoded is None:
        return None
    new_offset = max(0, decoded["o"] - by)
    return _encode(decoded["q"], decoded["r"], new_offset)


def attach_cursor(
    payload: dict,
    list_key: str,
    params_key: dict,
    release: str,
    offset: int,
    has_more: bool,
) -> dict:
    """Add `cursor` to payload when its answer is truncated.

    Call after budget.apply_budget: its `truncated` covers token-budget
    trimming (some fetched rows were dropped or stripped to fit); `has_more`
    covers a second, independent kind of truncation — the scan itself
    stopped at `limit`/MAX_ROWS while more matching rows exist upstream
    (the caller detects this by fetching one extra row beyond `limit` and
    passing whether it came back). Either one earns a cursor.

    offset is how many rows of this query were already delivered before
    this call (0 on a first page). A payload that's truncated by neither
    measure is returned unchanged — no cursor key at all, so the response
    shape stays additive (issue: ROADMAP §4.4).
    """
    truncated = bool(payload.get("truncated")) or has_more
    if not truncated:
        return payload
    result = dict(payload)
    result["truncated"] = True
    new_offset = offset + len(payload.get(list_key) or [])
    result["cursor"] = encode_cursor(params_key, release, new_offset)
    return result
