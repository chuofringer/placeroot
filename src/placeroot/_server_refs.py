"""LocationRef resolution and the thread fan-outs that resolve many refs at once.

Moved verbatim out of server.py (no behaviour change): named-place and LocationRef
resolution (a place name, lat/lon dict, or GERS id to one resolved place), the
pair/route-end resolvers, the batch fan-outs over db.isolated_reads() worker threads,
and the echo shapes that report what a ref resolved to. server.py re-exports every name.
"""

import contextvars
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from placeroot import (
    db,
    errors,
    gers,
    overture,
)
from placeroot import geocode as geocoding
from placeroot._server_errors import (
    _invalid_coord,
    _schema_error,
    _upstream_error,
)

# Roadmap §4, next tier: not_found from a name-resolution dead end names the
# next move rather than leaving the caller to guess. resolve_place carries
# its own "need"/"retry_with" sketch for the same situation (a plain
# `resolve_place()` call with no rows) — this "try" string is for the
# tools that resolve a name internally and cannot offer that structured
# retry, since they don't expose the intermediate resolve step for the
# caller to redo more specifically. core (the default profile) always
# carries resolve_place and geocode, so naming them here holds for the
# common case; a narrower PLACEROOT_TOOLS selection may not register one.
_NAME_NOT_FOUND_TRY = (
    "resolve_place with near_lat/near_lon or city to disambiguate; or geocode for street addresses"
)


def _server():
    """The placeroot.server module, looked up at call time.

    Tests monkeypatch _resolve_named_place on placeroot.server, and a call made
    from this module must see that patch, so it goes through the server module
    instead of this module's own global.
    """
    return sys.modules["placeroot.server"]


def _resolve_named_place(query: str) -> dict:
    """A free-text name -> compact {name, lat, lon, id, type} or an error.

    Shared by from_to and find_near. Ambiguous same-score names return
    candidates instead of silently picking a city. An unresolvable name
    returns {"error": "not_found", "detail", "try"} — "try" names the next
    move (roadmap §4). A comma-qualified name whose qualifier resolved but
    held nothing (#427) says so by naming the qualifier it searched,
    instead of reporting a same-ish name from the other side of the world.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "bad_request", "detail": "place name must be a non-empty string"}
    query = query.strip()
    try:
        resolved = geocoding.resolve_named_place(query)
    except errors.AnchoredNotFound as e:
        return {"error": "not_found", "detail": e.detail, "try": _NAME_NOT_FOUND_TRY}
    except errors.AmbiguousPlace as e:
        return {
            "error": "ambiguous_place",
            "detail": e.detail,
            "query": e.query,
            "candidates": e.candidates,
        }
    except overture.UpstreamUnavailable as e:
        return _upstream_error(e)
    if resolved is None:
        return {
            "error": "not_found",
            "detail": f"no place matched {query!r}",
            "try": _NAME_NOT_FOUND_TRY,
        }
    return resolved


def _resolve_pair(a: str, b: str) -> tuple[dict, dict]:
    """Resolve two names in parallel, each on its own cursor.

    db.isolated_reads gives each worker a private cursor and lock so the
    two resolves genuinely overlap instead of serializing on the shared
    conn lock (#328's parallel-inside-the-compose requirement).

    Workers do not inherit contextvars, so copy the request context into
    each submit — otherwise progress.report from a cold resolve lands in
    a throwaway per-thread log and never reaches attach().
    """

    def _isolated(query: str) -> dict:
        with db.isolated_reads():
            return _server()._resolve_named_place(query)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(contextvars.copy_context().run, _isolated, a)
        fb = pool.submit(contextvars.copy_context().run, _isolated, b)
        return fa.result(), fb.result()


def _resolve_ref_pair(a: str, b: str) -> tuple[tuple, tuple]:
    """Two string LocationRefs (names and/or GERS ids), each on its own cursor.

    perf: the same fan-out as _resolve_pair, for the route ends that
    _resolve_pair's plain-name fast path does not take (a GERS id on one or
    both sides). Returns ((origin, origin_error), (dest, dest_error)) so
    the caller keeps the origin-first error order it had when the ends ran
    in turn.
    """

    def _isolated(ref: str):
        with db.isolated_reads():
            return _resolve_location_ref(ref)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(contextvars.copy_context().run, _isolated, a)
        fb = pool.submit(contextvars.copy_context().run, _isolated, b)
        return fa.result(), fb.result()


# Real GERS ids are 32 lowercase hex characters (gers.py's module docstring
# and _validate_id's own comment). Deliberately stricter than gers.py's own
# ID_CHARSET_RE, which has to admit synthetic fixture ids like
# "gers-div-brooklyn" for gers_lookup's own tests — LocationRef needs the
# opposite bias: a free-text name must never be misread as an id, so this
# only recognizes the one shape a real id actually has. Case-insensitive
# since nothing here depends on it and rejecting a same-shape uppercase id
# would just cost the caller a confusing not_found.
_GERS_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)

_LOCATION_REF_BAD_REQUEST = {
    "error": "bad_request",
    "detail": (
        'location must be one of: {"lat": ..., "lon": ...} with numeric lat/lon in '
        "range, a GERS id string (32 hex characters), or a non-empty free-text place name"
    ),
}


def _resolve_location_ref(ref) -> tuple[dict | None, dict | None]:
    """A LocationRef ({lat,lon} | GERS id string | free-text name) -> (resolved, error).

    Exactly one of the two return values is not None. `resolved` always
    carries numeric lat/lon; a coordinate dict passes through untouched (no
    `id`/`name`/`matched_by` — nothing to echo back, on purpose: raw
    coordinate input must not grow the answer). A string input additionally
    carries `id`, `name`, and `matched_by` ("gers_id" or "name") so callers
    can build the compact `resolved` echo the roadmap calls for.

    `error` is a structured envelope ready to return as-is (bad_request,
    not_found, ambiguous_place, or an upstream/schema failure) — callers
    that resolve several refs prefix its `detail` with the failing index.
    A not_found also carries "try" naming the next move (roadmap §4).
    """
    if isinstance(ref, dict):
        lat, lon = ref.get("lat"), ref.get("lon")
        coord_error = _invalid_coord(lat, lon)
        if coord_error is not None:
            return None, _LOCATION_REF_BAD_REQUEST
        return {"lat": float(lat), "lon": float(lon)}, None
    if isinstance(ref, str):
        text = ref.strip()
        if not text:
            return None, _LOCATION_REF_BAD_REQUEST
        if _GERS_ID_RE.match(text):
            try:
                hit = gers.gers_lookup(text)
            except ValueError:
                hit = None
            except overture.UpstreamUnavailable as e:
                return None, _upstream_error(e)
            except overture.SchemaDegraded as e:
                return None, _schema_error(e)
            if hit is None or hit.get("lat") is None or hit.get("lon") is None:
                return None, {
                    "error": "not_found",
                    "detail": (
                        f"{text!r} looked like a GERS id; no feature has it in this release"
                    ),
                    "try": (
                        "resolve_place or geocode to find the right id; "
                        'or pass a {"lat", "lon"} location instead'
                    ),
                }
            return {
                "id": hit.get("id"),
                "name": hit.get("name"),
                "lat": hit["lat"],
                "lon": hit["lon"],
                "matched_by": "gers_id",
            }, None
        hit = _server()._resolve_named_place(text)
        if "error" in hit:
            return None, hit
        item = {
            "id": hit.get("id"),
            "name": hit.get("name"),
            "lat": hit["lat"],
            "lon": hit["lon"],
            "matched_by": "name",
        }
        if hit.get("note"):
            # #427: the name carried a qualifier that resolved to nothing,
            # so the whole string was searched instead. Non-fatal, but the
            # caller stated something that was not honored and has to hear
            # it — the echo is where it stays visible.
            item["note"] = hit["note"]
        return item, None
    return None, _LOCATION_REF_BAD_REQUEST


def _resolve_location_refs(refs, param_name: str) -> tuple[list[dict] | None, dict | None]:
    """A list of LocationRefs -> (resolved list, error), same contract as above.

    Items that need network resolution (strings: GERS ids or names) resolve
    in parallel, mirroring _resolve_pair's ThreadPoolExecutor + contextvars
    pattern (workers do not inherit contextvars, so the request context is
    copied into each submit or progress.report from a cold resolve would
    never reach attach()). Plain {lat,lon} dicts need no network round-trip
    and are resolved inline.

    On any failure, returns the error for the lowest-indexed failing item
    (deterministic regardless of which worker finishes first), with
    `"index"` set and `detail` prefixed `f"{param_name}[{i}]: "` so the
    agent can retry that one argument instead of the whole call.
    """
    if not isinstance(refs, list):
        return None, {"error": "bad_request", "detail": f"{param_name} must be a list"}

    resolved: list[dict | None] = [None] * len(refs)
    failures: dict[int, dict] = {}
    network_idxs = [i for i, r in enumerate(refs) if isinstance(r, str)]

    def _isolated(i: int, ref):
        with db.isolated_reads():
            return i, _resolve_location_ref(ref)

    if len(network_idxs) > 1:
        with ThreadPoolExecutor(max_workers=min(len(network_idxs), 8)) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, _isolated, i, refs[i])
                for i in network_idxs
            ]
            for future in futures:
                i, (item, err) = future.result()
                if err is not None:
                    failures[i] = err
                else:
                    resolved[i] = item
        pending = [i for i in range(len(refs)) if i not in network_idxs]
    else:
        pending = list(range(len(refs)))

    for i in pending:
        item, err = _resolve_location_ref(refs[i])
        if err is not None:
            failures[i] = err
        else:
            resolved[i] = item

    if failures:
        idx = min(failures)
        err = failures[idx]
        detail = f"{param_name}[{idx}]: {err.get('detail', '')}"
        return None, {**err, "index": idx, "detail": detail}
    return resolved, None


def _location_ref_echo(item: dict) -> dict:
    """The compact {name, id, lat, lon, matched_by} block for a resolved string LocationRef.

    Only called for items that carry `matched_by` — coordinate inputs never
    reach this (see _resolve_location_ref's contract), which is what keeps
    the `resolved` echo absent for pure-coordinate calls. Plus `note` when
    the resolution has something non-fatal to disclose (#427).
    """
    echo = {
        "name": item.get("name"),
        "id": item.get("id"),
        "lat": item["lat"],
        "lon": item["lon"],
        "matched_by": item["matched_by"],
    }
    if item.get("note"):
        echo["note"] = item["note"]
    return echo


def _resolve_matrix_side(points: list, param_name: str) -> tuple[list[dict] | None, dict | None]:
    """origins/destinations LocationRef list -> ([{"lat","lon",...}], error).

    A thin name for _resolve_location_refs at the matrix tools' call sites —
    a missing/non-numeric lat or lon on a plain {"lat", "lon"} dict already
    comes back as an indexed bad_request via _invalid_coord inside
    _resolve_location_ref, so no separate precheck is needed here (and
    adding one would let a malformed dict at a higher index preempt an
    unresolved string at a lower one, breaking "lowest index wins").
    """
    return _resolve_location_refs(points, param_name)


def _matrix_resolved_echo(
    origins: list,
    resolved_origins: list[dict],
    destinations: list,
    resolved_destinations: list[dict],
) -> dict | None:
    """The {"origins": [...], "destinations": [...]} resolved echo, string items only."""
    echo = {}
    o_echo = [
        {"index": i, **_location_ref_echo(r)}
        for i, r in enumerate(resolved_origins)
        if isinstance(origins[i], str)
    ]
    d_echo = [
        {"index": i, **_location_ref_echo(r)}
        for i, r in enumerate(resolved_destinations)
        if isinstance(destinations[i], str)
    ]
    if o_echo:
        echo["origins"] = o_echo
    if d_echo:
        echo["destinations"] = d_echo
    return echo or None


def _resolve_string_origins(
    origins: list, string_idxs: list[int]
) -> tuple[dict[int, dict], dict | None]:
    """Resolve just origins' string entries (by real index), in parallel.

    meeting_point's per-index validation loop mixes string LocationRefs
    with dict {"lat","lon","mode"} origins that need their own mode
    validation, so it can't just hand the whole `origins` list to
    _resolve_location_refs (that function would try to coordinate-validate
    the dict origins itself, with a different error shape than this tool
    documents). This resolves only the string entries — same
    ThreadPoolExecutor + contextvars.copy_context() + db.isolated_reads()
    pattern _resolve_location_refs uses, so 2-5 cold name/GERS resolutions
    run concurrently instead of serially eating into the question's time
    budget — and returns them keyed by their real position in `origins`,
    so the caller's per-index loop and error messages need no index
    remapping. On any failure, returns the lowest-indexed failure with
    `"index"` and `detail` prefixed `f"origins[{i}]: "`, same contract as
    _resolve_location_refs.
    """
    resolved: dict[int, dict] = {}
    failures: dict[int, dict] = {}

    def _isolated(i: int):
        with db.isolated_reads():
            return i, _resolve_location_ref(origins[i])

    if len(string_idxs) > 1:
        with ThreadPoolExecutor(max_workers=min(len(string_idxs), 8)) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, _isolated, i) for i in string_idxs
            ]
            for future in futures:
                i, (item, err) = future.result()
                if err is not None:
                    failures[i] = err
                else:
                    resolved[i] = item
    else:
        for i in string_idxs:
            item, err = _resolve_location_ref(origins[i])
            if err is not None:
                failures[i] = err
            else:
                resolved[i] = item

    if failures:
        idx = min(failures)
        err = failures[idx]
        detail = f"origins[{idx}]: {err.get('detail', '')}"
        return {}, {**err, "index": idx, "detail": detail}
    return resolved, None


def _resolve_route_ends(from_, to) -> tuple[dict | None, dict | None, dict | None]:
    """Resolve a routing call's two LocationRef ends — (origin, dest, error).

    Exactly one of the two shapes comes back: (origin, dest, None) with both
    ends resolved to dicts carrying lat/lon (plus name/id/type/admin_context
    when the input was a name or GERS id), or (None, None, error) where the
    error dict names the offending side in "field": "from" | "to". Shared by
    _route_between_refs (route/from_to) and compare_modes so the end
    semantics — empty-string bad_request, the byte-identical parallel
    _resolve_pair fast path for two plain names, per-side field — are one
    implementation rather than two that can drift.
    """
    if isinstance(from_, str) and not from_.strip():
        return (
            None,
            None,
            {
                "error": "bad_request",
                "detail": "from must be a non-empty place name",
                "field": "from",
            },
        )
    if isinstance(to, str) and not to.strip():
        return (
            None,
            None,
            {
                "error": "bad_request",
                "detail": "to must be a non-empty place name",
                "field": "to",
            },
        )
    # Both ends still plain names (not GERS ids): the original, byte-
    # identical path — same parallel _resolve_pair call, same
    # ambiguous_place/not_found shape as before this feature existed.
    if (
        isinstance(from_, str)
        and isinstance(to, str)
        and not _GERS_ID_RE.match(from_.strip())
        and not _GERS_ID_RE.match(to.strip())
    ):
        origin, dest = _resolve_pair(from_, to)
        if "error" in origin:
            return None, None, {**origin, "field": "from"}
        if "error" in dest:
            return None, None, {**dest, "field": "to"}
        return origin, dest, None
    if isinstance(from_, str) and isinstance(to, str):
        # perf: two network lookups (a name and a GERS id, or two ids) run
        # side by side; the origin's error still wins, as in turn order.
        (origin, origin_error), (dest, dest_error) = _resolve_ref_pair(from_, to)
    else:
        origin, origin_error = _resolve_location_ref(from_)
        dest, dest_error = (None, None) if origin_error is not None else _resolve_location_ref(to)
    if origin_error is not None:
        return None, None, {**origin_error, "field": "from"}
    if dest_error is not None:
        return None, None, {**dest_error, "field": "to"}
    return origin, dest, None
