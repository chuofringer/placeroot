"""MCP resources: the two purely-static lookups, readable without a tool call (issue #195).

A resource is context a *user* attaches, not something the model invokes:
Claude Code auto-completes them as `@placeroot:placeroot://…` mentions and
pastes the payload into the conversation; Claude Desktop and Cursor list
them in their own attachment pickers. That makes them the right home for
the two answers that never depend on arguments — which Overture release
backs everything, and the shape of the category taxonomy — because pinning
either one up front removes a whole round-trip from the conversations that
need it.

Two conventions, both test-guarded in `tests/test_resources.py`:

1. **Resources share the tool's code path, they don't mirror it.**
   `data_version_payload()` below is the single source of the release
   answer; `server.data_version()` returns it verbatim. A resource that
   drifted from its tool would be worse than no resource at all, so the
   test asserts the two are equal rather than merely similar.
2. **Resources are always registered, whatever `PLACEROOT_TOOLS` selects.**
   They are not part of `tools/list` (a test asserts it is byte-identical
   with and without them), so they cost a subset install exactly nothing,
   and `placeroot://data-version` is worth reading precisely when the
   `data_version` tool was left out of the selection. Same call made for
   prompts in #194.

Both payloads stay compact — the category resource is a *summary* of the
2,117-slug taxonomy, not the CSV. The full taxonomy is the
`search_categories` tool's job; a resource that dumped it would cost more
context than the lookups it saves. `tests/test_resources.py` asserts the
budget-module estimate stays under 1.5k tokens.
"""

import json

from mcp.server.mcpserver import MCPServer

from placeroot import budget, categories, recreation, release

DATA_VERSION_URI = "placeroot://data-version"
CATEGORIES_URI = "placeroot://categories"

DATA_VERSION_DESCRIPTION = (
    "Which Overture Maps release backs every PlaceRoot answer, and how it "
    "was resolved (live S3 discovery, an operator env override, or the "
    "pinned fallback). Same value the data_version tool returns."
)

CATEGORIES_DESCRIPTION = (
    "Summary of the Overture places category taxonomy: every top-level "
    "category with how many slugs sit under it, plus how to look up the "
    "exact slug for a query. Compact by design — use the "
    "search_categories tool for individual slugs."
)

# Soft ceiling for the categories payload, in the chars/4 token units
# budget.py counts in. Not enforced at runtime (the payload is derived from
# a pinned CSV, so it cannot grow behind our backs); asserted in tests so a
# future edit that turns the summary back into a dump fails loudly.
CATEGORIES_TOKEN_BUDGET = 1500


def data_version_payload() -> dict:
    """The resolved-release answer. The `data_version` tool returns this too.

    Reads the TTL cache in release.py — no DB dependency, safe to read from
    either surface at any time. It can trigger the one discovery call that
    an expired TTL owes, the same as any query path would.
    """
    info = release.resolve_release_info()
    release_str = info["release"]
    payload = {
        "release": release_str,
        "release_date": release_str.rsplit(".", 1)[0],
        "source": info["source"],
        # #219: the vintage's age, so an agent or operator can see the data
        # is old without knowing Overture's release cadence. `stale` flips
        # past PLACEROOT_STALE_RELEASE_DAYS (default 60 — two missed
        # ~monthly releases).
        "age_days": release.age_days(release_str),
        "note": (
            "Overture ships ~monthly; re-checked on a TTL "
            "(PLACEROOT_RELEASE_TTL_HOURS, default 6h)."
        ),
    }
    if release.is_stale(release_str):
        payload["stale"] = True
        payload["note"] += (
            " This release is older than the staleness threshold — "
            "discovery may be failing in this deployment."
        )
    layer = recreation_payload()
    if layer is not None:
        payload["recreation_layer"] = layer
    return payload


def recreation_payload() -> dict | None:
    """The active recreation layer, or None when it isn't enabled.

    An answer drawn partly from a second theme has to say so: a
    `find_places` result whose provenance the caller can't see is exactly
    the silently-partial answer CONTRIBUTING.md's honesty rule exists to
    prevent. Everything reported here is derivable from the code and the
    pinned release — there is no local build whose vintage could drift, so
    there is nothing to read off disk.

    `degraded_types` is the one field worth an agent's attention: a base
    type listed there is one whose schema drifted far enough that the layer
    dropped it, so results are places-theme-only for its categories.
    """
    if not recreation.enabled():
        return None
    payload = {
        "source": "Overture base theme (types: " + ", ".join(recreation.SOURCES) + ")",
        "categories": recreation.CATEGORIES,
        "note": (
            "Opt-in via " + recreation.ENV_VAR + ". Adds OSM-derived recreation "
            "areas the listings-derived places theme under-counts. These rows carry "
            "no confidence or operating_status, and may have no name."
        ),
    }
    degraded = recreation.degraded_types()
    if degraded:
        payload["degraded_types"] = degraded
    return payload


def categories_payload() -> dict:
    """Compact taxonomy summary derived from the bundled CSV at call time.

    Deterministic: same pinned snapshot in, same payload out (top-level
    categories sorted by descending slug count, ties broken by name, so
    dict iteration order cannot leak in).
    """
    summary = categories.taxonomy_summary()
    return {
        "taxonomy": "Overture Maps places categories",
        "schema_version": categories.SCHEMA_VERSION,
        "total_categories": summary["total"],
        "top_level_categories": [
            {"category": name, "slugs": count} for name, count in summary["top_level"]
        ],
        # Only tools that actually take a `category` argument may be named
        # here. MCP argument validation silently drops unknown kwargs, so an
        # agent that believed summarize_area took a category would get a
        # 200 OK *unfiltered* answer and never know. test_resources.py
        # checks every name below against the live tool signature.
        "how_to_use": (
            "Category slugs are the leaves of this tree (e.g. coffee_shop under "
            "eat_and_drink > cafe). Pass one to the `category` filter of "
            "`find_places()` or `places_along_route()`. "
            "Call the `search_categories()` tool with free text to get the exact "
            "slug and its path — guessing a slug returns zero results."
        ),
        "note": (
            f"{summary['total']} slugs total; only the {len(summary['top_level'])} "
            "top-level branches are listed here. This is a summary, not the full "
            "taxonomy."
        ),
    }


def render(payload: dict) -> str:
    """The exact text a resources/read returns for `payload`.

    Indented JSON: a resource is pasted into a human-visible conversation,
    where the extra whitespace is worth more than the bytes it costs. Both
    resources go through here so the token estimate below measures what is
    actually served, not a denser form of it.
    """
    return json.dumps(payload, indent=2)


def categories_token_estimate() -> int:
    """chars/4 estimate for the served categories text, in budget.py's units.

    budget.estimate_tokens() counts the compact JSON form; the wire form is
    indented, so measure the string we actually send with the same
    heuristic rather than under-reporting by the whitespace.
    """
    return len(render(categories_payload())) // budget.CHARS_PER_TOKEN


def register(server: MCPServer) -> None:
    """Register both resources on `server`.

    Called from build_server() so a test can build a server for any
    PLACEROOT_TOOLS selection without touching the process environment.
    Unlike tools, the set of resources does not depend on the selection —
    resources never reach tools/list, so they cost a subset nothing.
    """

    @server.resource(
        DATA_VERSION_URI,
        name="data_version",
        title="Overture data version",
        description=DATA_VERSION_DESCRIPTION,
        mime_type="application/json",
    )
    def data_version_resource() -> str:
        return render(data_version_payload())

    @server.resource(
        CATEGORIES_URI,
        name="categories",
        title="Overture place categories",
        description=CATEGORIES_DESCRIPTION,
        mime_type="application/json",
    )
    def categories_resource() -> str:
        return render(categories_payload())
