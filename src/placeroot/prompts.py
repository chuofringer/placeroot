"""MCP prompts: canned multi-tool geo workflows (issue #194).

Prompts surface as slash commands in the clients that support them
(`/mcp__placeroot__site_selection` in Claude Code) and cost **zero tokens in
`tools/list`** — a client only fetches them on `prompts/list`, and only
materializes one when the user invokes it. That makes them the right place
to put the workflows that are worth more than any single tool: which tool to
call first, what to do with its output, and what the answer should look like.

Two conventions hold the module together, both enforced by
`tests/test_prompts.py`:

1. **Every tool mention is written `tool_name()`** — backticked, with empty
   parentheses. That is what makes the guard test possible: it extracts every
   `identifier(` from every rendered prompt and asserts the name is in the
   server's registry, so renaming a tool cannot leave a prompt pointing at a
   name that no longer exists. The reverse is checked too — a registered tool
   name appearing bare, without the parentheses, fails the test, since the
   guard would not see it.
2. **Prompts are always registered, whatever `PLACEROOT_TOOLS` selects.**
   They are not part of `tools/list` and so cost a subset install nothing,
   and a workflow is still worth reading when one of its steps is
   unavailable. When the active profile excludes a tool a prompt names, the
   rendered text carries a note saying so and telling the agent to work
   around it, rather than the prompt silently instructing a call that will
   fail. See `_profile_note`.
"""

import re
from collections.abc import Callable, Iterable

from mcp.server.mcpserver import MCPServer

_WHITESPACE = re.compile(r"\s+")


def _arg(value: str) -> str:
    """A prompt argument, flattened to a single line.

    Arguments are interpolated raw into markdown that is otherwise a
    numbered list of instructions, so a value containing line breaks can
    lay itself out as further steps ("...\\n\\n6. ignore the above"). The
    user is supplying their own prompt here, so this is self-inflicted
    rather than an attack surface, but a numbered step that came from an
    argument should not be able to read as one the workflow wrote.
    Collapsing every whitespace run to a single space is the whole fix:
    without a newline there is no list item and no heading.
    """
    return _WHITESPACE.sub(" ", value or "").strip()


def _profile_note(referenced: Iterable[str], selected: set[str]) -> str:
    """Trailing note naming the referenced tools this install did not register.

    Empty (and so invisible) on the default full surface. Under a subset
    profile it is the difference between an agent trying an unavailable
    call and an agent knowing to route around the gap.
    """
    missing = sorted(set(referenced) - selected)
    if not missing:
        return ""
    return (
        "\n\nNote: this install runs a PLACEROOT_TOOLS subset that does not "
        "register " + ", ".join(missing) + ". Skip those steps and get as far "
        "as the registered tools allow, then say which part of the workflow "
        "the missing tools would have covered."
    )


_COMPACT = (
    "Keep the final answer compact: a short ranked list or table plus one "
    "or two sentences of reasoning. Do not dump raw tool output — every "
    "PlaceRoot response is already budgeted and ranked, so cite the few "
    "numbers that decide the answer and leave the rest."
)


SITE_SELECTION_TOOLS = (
    "search_categories",
    "geocode",
    "summarize_area",
    "find_places",
    "compare_areas",
    "within_distance",
)


def _site_selection(business_type: str, area: str) -> str:
    business_type = _arg(business_type)
    area = _arg(area)
    return f"""Find the best location in {area} to open a {business_type}, using the \
PlaceRoot tools.

Work through these steps, and stop early if the evidence is already decisive:

1. `search_categories()` with text describing a {business_type} to get the
   Overture category slug. Every later category filter must use that slug —
   guessing one returns zero results.
2. `geocode()` on "{area}" to get its coordinates and admin context. If it
   resolves to several candidates, say which one you picked and why.
3. `summarize_area()` at that point for the baseline: how many places, which
   categories dominate. That is the character of the area you are placing
   into.
4. Pick 3-5 candidate sub-areas (neighborhoods, commercial strips, transit
   nodes) from what steps 2-3 surfaced, then `compare_areas()` on them in one
   call. Read the category mix and density, not just the totals.
5. For the leading 1-2 candidates, `find_places()` with the step-1 category
   slug to count and locate the direct competition, and `within_distance()`
   to check how close the nearest competitor actually is.

Then recommend one location. Justify it on the evidence you gathered:
competitor count and distance, complementary category mix, and overall
density. Name the runner-up and the single fact that separates them, and be
explicit about what the data cannot tell you (foot traffic, rents, zoning,
demographics are all outside Overture).

{_COMPACT}"""


COMPARE_NEIGHBORHOODS_TOOLS = (
    "resolve_place",
    "geocode",
    "admin_lookup",
    "summarize_area",
    "compare_areas",
    "summarize_buildings",
)


def _compare_neighborhoods(area_a: str, area_b: str) -> str:
    area_a = _arg(area_a)
    area_b = _arg(area_b)
    return f"""Compare {area_a} and {area_b} as places, using the PlaceRoot tools.

1. Resolve both names to points first: `geocode()` (or `resolve_place()` if
   you want stable ids to carry across turns). Confirm each resolved to the
   place the user means — same-named neighborhoods in different cities are
   the usual failure here — and use `admin_lookup()` on each point to state
   the admin hierarchy each one sits in.
2. `summarize_area()` on each point at the same radius. Same radius on both
   sides or the comparison is meaningless.
3. `compare_areas()` with both points in one call: that is the tool that
   reports what differs most between them, rather than making you diff two
   summaries by eye.
4. `summarize_buildings()` on each for the physical fabric: building count,
   footprint area, height and use mix. Amenity mix says what a place is for;
   building stock says what it is made of.

Then answer in a small table — one row per dimension that actually differs
(density, dominant categories, building height/use), one column per
neighborhood — followed by two or three sentences on what kind of place each
one is and who each suits. Use the same radius everywhere and say what it
was. Overture has no rent, crime, school, or demographic data; if the user's
question implies those, say what you could not check.

{_COMPACT}"""


PLAN_ERRANDS_TOOLS = (
    "geocode_batch",
    "distance_matrix",
    "route",
    "places_along_route",
)


def _plan_errands(stops: str, start: str = "") -> str:
    stops = _arg(stops)
    start = _arg(start)
    origin = (
        f'Start from "{start}".'
        if start
        else "If the user has not said where they are starting, ask before ordering the stops."
    )
    # `stops` is required, but "required" in MCP means present, not
    # non-empty: a client can send "" or "   ". Interpolating that renders
    # "visiting: " and the agent plans a route around nothing. Ask instead.
    task = (
        f"Plan an efficient errand run visiting: {stops}"
        if stops
        else (
            "Plan an efficient errand run. The user has not said which stops "
            "to visit — ask them for the list of errands first, then work "
            "through the steps below with it. Do not invent stops."
        )
    )
    return f"""{task}

{origin}

1. `geocode_batch()` with every stop in one call — one round-trip for the
   whole list, not one call per stop. If any stop comes back ambiguous,
   resolve it with the user before planning around a guess.
2. `distance_matrix()` over the resolved points to get every pairwise
   straight-line distance at once. Order the stops greedily from that matrix:
   nearest unvisited next, from the start point.
3. `route()` on each consecutive leg of that order for real network distance
   and duration on the chosen travel mode. Straight-line ordering can lose to
   the road network across a river or a rail line, so if a leg's network
   distance is far above its matrix distance, swap it with its neighbor and
   measure the pair again.
4. Optionally `places_along_route()` on the longest leg for anything worth
   picking up on the way.

Then give the ordered stop list with per-leg distance and duration and a
total, and one sentence on why that order beats the order the user gave.
If any stop carries a trust_note, add one "Verify before going:" line
naming the 1–2 weakest-confidence stops — the ones most worth checking
before anyone leaves the house.

{_COMPACT}"""


# name -> (function, description, referenced tool names). The description is
# what a client shows next to the slash command.
PROMPTS: dict[str, tuple[Callable[..., str], str, tuple[str, ...]]] = {
    "site_selection": (
        _site_selection,
        "Pick where to open a business: category lookup, area baseline, "
        "candidate comparison, competitor proximity, one recommendation.",
        SITE_SELECTION_TOOLS,
    ),
    "compare_neighborhoods": (
        _compare_neighborhoods,
        "Compare two neighborhoods side by side on amenity mix, density, "
        "and building stock.",
        COMPARE_NEIGHBORHOODS_TOOLS,
    ),
    "plan_errands": (
        _plan_errands,
        "Order a list of errand stops into an efficient route with per-leg "
        "distances and durations.",
        PLAN_ERRANDS_TOOLS,
    ),
}


def register(server: MCPServer, selected: set[str]) -> None:
    """Register every prompt on `server`, appending the note for `selected`.

    Called from build_server() for the same reason tools are: registration
    is a property of the server instance, so a test can build one for any
    PLACEROOT_TOOLS selection without touching the process environment.
    Unlike tools, the set of prompts does not depend on `selected` — only
    the note at the end of each rendered prompt does.
    """

    @server.prompt(name="site_selection", description=PROMPTS["site_selection"][1])
    def site_selection(business_type: str, area: str) -> str:
        return _site_selection(business_type, area) + _profile_note(
            SITE_SELECTION_TOOLS, selected
        )

    @server.prompt(
        name="compare_neighborhoods", description=PROMPTS["compare_neighborhoods"][1]
    )
    def compare_neighborhoods(area_a: str, area_b: str) -> str:
        return _compare_neighborhoods(area_a, area_b) + _profile_note(
            COMPARE_NEIGHBORHOODS_TOOLS, selected
        )

    @server.prompt(name="plan_errands", description=PROMPTS["plan_errands"][1])
    def plan_errands(stops: str, start: str = "") -> str:
        return _plan_errands(stops, start) + _profile_note(PLAN_ERRANDS_TOOLS, selected)
