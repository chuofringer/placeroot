#!/usr/bin/env python3
"""Site-selection demo (issue #17): "Where should I open a coffee shop in Austin?"

Runs the whole scenario through PlaceRoot's MCP tools as a real MCP client
talking to the server over stdio (the same transport a real agent uses) —
not by importing placeroot's Python internals. Every number in the printed
report comes from an actual tool response; nothing is invented.

Tool sequence:
    1. summarize_area   on each of 3 candidate centers
    2. compare_areas     across all 3, for differentiators
    3. find_places       (category=coffee_shop) in each, for competition
    4. within_distance   nearest grocery_store anchor, per area
    5. within_distance   nearest gym anchor (second daily-need proxy), per area
    6. place_details     on the closest competitor found in step 3, by GERS id
    7. admin_lookup       on each candidate center, to name the neighborhood

Usage:
    uv run python examples/site_selection/run_demo.py            # live Overture S3
    uv run python examples/site_selection/run_demo.py --offline  # committed fixtures

--offline points the query layer at the same committed fixtures
tests/conftest.py uses, so this can run in CI with no network. It exercises
the same tool-call pipeline as the live path; the fixture is a synthetic
data cluster, not real Austin, so the offline "recommendation" only smoke
tests the pipeline's shape, not real-world advice.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PLACES = REPO_ROOT / "tests" / "fixtures" / "places.parquet"
FIXTURE_DIVISIONS = REPO_ROOT / "tests" / "fixtures" / "divisions.parquet"

RADIUS_M = 800
COMPETITOR_CATEGORY = "coffee_shop"
GROCERY_ANCHOR_CATEGORY = "grocery_store"
# Overture's places theme doesn't carry transit stops, so a second daily-need
# anchor (gym) stands in as the "other reason people already come here"
# signal alongside grocery — documented here rather than dressed up as transit.
SECOND_ANCHOR_CATEGORY = "gym"
ANCHOR_MAX_DISTANCE_M = 600

# Live: three real, distinct Austin commercial districts, chosen to contrast
# a walkable retail strip, a nightlife/dining corridor, and a suburban
# office-park retail hub.
LIVE_CANDIDATES = [
    {"label": "South Congress", "lat": 30.2478, "lon": -97.7500},
    {"label": "East Austin (E 6th / Cesar Chavez)", "lat": 30.2629, "lon": -97.7223},
    {"label": "The Domain", "lat": 30.4021, "lon": -97.7241},
]

# Offline: three centers inside the committed synthetic fixture cluster
# (tests/fixtures/places.parquet, built around lat=40.70/lon=-73.90) so the
# whole pipeline runs deterministically without network. Not real Austin.
OFFLINE_CANDIDATES = [
    {"label": "Fixture Area A (southwest)", "lat": 40.6980, "lon": -73.9020},
    {"label": "Fixture Area B (northeast)", "lat": 40.7020, "lon": -73.8980},
    {"label": "Fixture Area C (center)", "lat": 40.7000, "lon": -73.9000},
]


class DemoError(RuntimeError):
    """Raised when a tool call errors or returns a structured {"error": ...}.

    The demo stops here rather than fabricating a result for a failed step.
    """


class StepLog:
    """Records each tool call's raw serialized response and its estimated token cost.

    Token estimate matches placeroot.budget.estimate_tokens's own heuristic
    (len(json)/4) so the numbers quoted in the report are directly
    comparable to the server's own budget accounting.
    """

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def record(self, label: str, tool: str, args: dict, raw_json: str, parsed: dict) -> dict:
        tokens = len(raw_json) // 4
        self.steps.append(
            {
                "label": label,
                "tool": tool,
                "args": args,
                "raw_json": raw_json,
                "parsed": parsed,
                "tokens": tokens,
            }
        )
        return parsed

    @property
    def total_tokens(self) -> int:
        return sum(s["tokens"] for s in self.steps)


def _offline_env() -> dict:
    env = os.environ.copy()
    # Mirrors tests/conftest.py's offline_data fixture: pin the release (no
    # discovery network call), disable the tile cache, and point both themes
    # at their committed fixtures.
    from placeroot import release as release_mod

    env["PLACEROOT_OVERTURE_RELEASE"] = release_mod.PINNED_RELEASE
    env["PLACEROOT_CACHE"] = "off"
    env["PLACEROOT_DATA_PATH"] = str(FIXTURE_PLACES)
    env["PLACEROOT_DATA_PATH_DIVISIONS"] = str(FIXTURE_DIVISIONS)
    return env


async def _call(
    session: ClientSession, log: StepLog, label: str, tool: str, args: dict
) -> dict:
    result = await session.call_tool(tool, args)
    if result.is_error:
        text = "".join(c.text for c in result.content if hasattr(c, "text"))
        raise DemoError(f"[{label}] {tool}{args} returned an MCP error: {text}")
    raw_json = "".join(c.text for c in result.content if hasattr(c, "text"))
    if not raw_json:
        raise DemoError(f"[{label}] {tool}{args} returned no text content")
    parsed = json.loads(raw_json)
    if isinstance(parsed, dict) and "error" in parsed:
        raise DemoError(f"[{label}] {tool}{args} -> {parsed}")
    return log.record(label, tool, args, raw_json, parsed)


def _fmt_tokens(n: int) -> str:
    return f"~{n} tokens"


async def run(offline: bool) -> tuple[StepLog, list[dict], list[dict], str]:
    candidates = OFFLINE_CANDIDATES if offline else LIVE_CANDIDATES
    log = StepLog()

    env = _offline_env() if offline else os.environ.copy()
    params = StdioServerParameters(command="uv", args=["run", "placeroot"], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_name = init.server_info.name
            server_version = init.server_info.version
            print(f"# Connected to MCP server: {server_name} {server_version}\n")

            # --- Step 1: summarize_area on each candidate -----------------
            summaries = []
            for c in candidates:
                res = await _call(
                    session,
                    log,
                    f"summarize_area({c['label']})",
                    "summarize_area",
                    {"lat": c["lat"], "lon": c["lon"], "radius_m": RADIUS_M},
                )
                summaries.append(res)

            # --- Step 2: compare_areas across all three --------------------
            areas_arg = [{"lat": c["lat"], "lon": c["lon"]} for c in candidates]
            await _call(
                session,
                log,
                "compare_areas(all 3)",
                "compare_areas",
                {"areas": areas_arg, "radius_m": RADIUS_M},
            )

            # --- Step 3: find_places coffee_shop competition per candidate --
            competition = []
            for c in candidates:
                res = await _call(
                    session,
                    log,
                    f"find_places(coffee_shop near {c['label']})",
                    "find_places",
                    {
                        "lat": c["lat"],
                        "lon": c["lon"],
                        "radius_m": RADIUS_M,
                        "category": COMPETITOR_CATEGORY,
                        "limit": 10,
                    },
                )
                competition.append(res)

            # --- Step 4 & 5: within_distance anchor checks per candidate ----
            anchors = []
            for c in candidates:
                grocery = await _call(
                    session,
                    log,
                    f"within_distance(grocery near {c['label']})",
                    "within_distance",
                    {
                        "lat": c["lat"],
                        "lon": c["lon"],
                        "max_distance_m": ANCHOR_MAX_DISTANCE_M,
                        "category": GROCERY_ANCHOR_CATEGORY,
                    },
                )
                second = await _call(
                    session,
                    log,
                    f"within_distance(gym near {c['label']})",
                    "within_distance",
                    {
                        "lat": c["lat"],
                        "lon": c["lon"],
                        "max_distance_m": ANCHOR_MAX_DISTANCE_M,
                        "category": SECOND_ANCHOR_CATEGORY,
                    },
                )
                anchors.append({"grocery": grocery, "gym": second})

            # --- Step 6: place_details on the closest competitor found -----
            all_competitors = [row for c in competition for row in c["results"]]
            if all_competitors:
                closest = min(all_competitors, key=lambda r: r["distance_m"])
                await _call(
                    session,
                    log,
                    f"place_details(closest competitor: {closest['name']})",
                    "place_details",
                    {"id": closest["id"]},
                )

            # --- Step 7: admin_lookup to name each neighborhood -------------
            admin_names = []
            for c in candidates:
                res = await _call(
                    session,
                    log,
                    f"admin_lookup({c['label']})",
                    "admin_lookup",
                    {"lat": c["lat"], "lon": c["lon"]},
                )
                admin_names.append(res)

    return log, summaries, competition, _recommend(
        candidates, summaries, competition, anchors, admin_names
    )


def _recommend(candidates, summaries, competition, anchors, admin_names) -> str:
    """Pick a candidate from real tool output: fewest coffee_shop competitors
    within radius, among areas whose grocery anchor is within reach; ties
    broken by higher total_places (more overall foot traffic).
    """
    scored = []
    for i, c in enumerate(candidates):
        n_competitors = len(competition[i]["results"])
        grocery_ok = anchors[i]["grocery"]["within"]
        total_places = summaries[i]["total_places"]
        chain = admin_names[i]["chain"]
        # chain[0] is the smallest *division* containing the point — often the
        # locality (e.g. "Austin" for every candidate), not the neighborhood.
        # The candidate label is the meaningful name; the chain is context.
        locality = chain[0]["name"] if chain else "unknown"
        scored.append(
            {
                "label": c["label"],
                "locality": locality,
                "n_competitors": n_competitors,
                "grocery_ok": grocery_ok,
                "total_places": total_places,
            }
        )

    def key(s):
        # Prefer areas with a grocery anchor in reach, then fewest
        # competitors, then more overall activity.
        return (0 if s["grocery_ok"] else 1, s["n_competitors"], -s["total_places"])

    best = min(scored, key=key)
    lines = ["## Recommendation\n"]
    for s in scored:
        lines.append(
            f"- **{s['label']}** ({s['locality']}): {s['n_competitors']} existing "
            f"coffee_shop competitor(s) within {RADIUS_M}m, grocery anchor "
            f"{'within' if s['grocery_ok'] else 'NOT within'} {ANCHOR_MAX_DISTANCE_M}m, "
            f"{s['total_places']} total places nearby."
        )
    tied = sum(1 for s in scored if s["n_competitors"] == best["n_competitors"]) > 1
    competitors_clause = (
        f"{best['n_competitors']} coffee_shop competitors within {RADIUS_M}m"
        + (" (tied with other candidates)" if tied else " (fewest among candidates)")
    )
    lines.append(
        f"\n**Recommended: {best['label']}** — grocery anchor in reach, "
        f"{competitors_clause}, and the most nearby activity "
        f"({best['total_places']} total places) to draw walk-in traffic from."
    )
    return "\n".join(lines)


def print_report(log: StepLog, recommendation: str, offline: bool) -> None:
    mode = "OFFLINE (committed fixtures)" if offline else "LIVE (Overture S3)"
    print("=" * 72)
    print(f"PlaceRoot site-selection demo — mode: {mode}")
    print("Question: Where should I open a coffee shop in Austin?")
    print("=" * 72)
    print()
    print("## Tool calls\n")
    for i, s in enumerate(log.steps, 1):
        print(f"{i}. {s['tool']} — {s['label']}")
        print(f"   args: {json.dumps(s['args'])}")
        excerpt = s["raw_json"]
        if len(excerpt) > 400:
            excerpt = excerpt[:400] + " ...(truncated for display)"
        print(f"   response ({_fmt_tokens(s['tokens'])}): {excerpt}")
        print()
    print("## Token accounting\n")
    for s in log.steps:
        print(f"  {s['label']:<55} {_fmt_tokens(s['tokens'])}")
    print(f"\n  TOTAL across {len(log.steps)} tool calls: {_fmt_tokens(log.total_tokens)}")
    print()
    print(recommendation)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run against the committed fixtures instead of live Overture S3.",
    )
    args = parser.parse_args()

    try:
        log, summaries, competition, recommendation = asyncio.run(run(args.offline))
    except DemoError as e:
        print(f"Demo stopped: {e}", file=sys.stderr)
        return 1

    print_report(log, recommendation, args.offline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
