"""Offline token-efficiency benchmark (issue #178).

Two costs an agent actually pays for a PlaceRoot install, measured
separately because they behave differently:

- **Schema surface**: what every registered tool's name + description +
  JSON schema costs in the model's context, once per conversation, whether
  or not any tool is ever called. Read from the live `MCPServer` registry in
  `placeroot.server` (`mcp.list_tools()`) and serialized exactly the way the
  MCP SDK puts it on the wire, so it can't drift from what a client really
  receives.
- **Response cost**: what one answer costs, per tool call, for a fixed
  suite of scenarios run against the committed test fixtures.

This complements `benchmarks/token_benchmark.py` (issue #26), which is a
*live* accuracy-plus-token-ratio benchmark against real Overture data. This
one is fully offline and deterministic: same fixtures, same numbers, every
run, no network.

    uv run python benchmarks/token_efficiency.py           # print the report
    uv run python benchmarks/token_efficiency.py --write   # + update docs/benchmarks.md

Tokenizer: see `resolve_tokenizer()` — the project has no tokenizer
dependency and this benchmark does not add one, so unless `tiktoken`
happens to be importable in the environment, counts are the
documented chars/4 heuristic `placeroot.budget.estimate_tokens` already
uses to enforce the server's own token budget. Exact character and byte
counts are reported alongside every estimate so a reader with a real
tokenizer can re-derive the numbers without rerunning anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# tests/ carries the committed fixtures and the coordinates every offline
# test is written against; importing them keeps this benchmark's scenarios
# from drifting away from the data they run on.
sys.path.insert(0, str(REPO_ROOT))

from placeroot import buildings, overture, release, routing, server  # noqa: E402
from tests._routing_fixture import build_routing_fixture as fx  # noqa: E402
from tests.conftest import (  # noqa: E402
    ADDRESSES_FIXTURE_PATH,
    BUILDINGS_FIXTURE_PATH,
    CENTER_LAT,
    CENTER_LON,
    DIVISION_AREAS_FIXTURE_PATH,
    DIVISIONS_FIXTURE_PATH,
    FIXTURE_PATH,
    TRANSPORTATION_FIXTURE_PATH,
)

DOC_PATH = REPO_ROOT / "docs" / "benchmarks.md"
GENERATED_BEGIN = "<!-- BEGIN GENERATED: benchmarks/token_efficiency.py -->"
GENERATED_END = "<!-- END GENERATED -->"


# ----------------------------------------------------------------------
# Tokenizer selection.
# ----------------------------------------------------------------------


def resolve_tokenizer() -> tuple[Callable[[str], int], str]:
    """(count_fn, human-readable method label).

    Prefers a real tokenizer *if one is already importable* — this benchmark
    must not add a dependency to a server whose whole point is being small.
    `tiktoken`'s cl100k_base is OpenAI's, not Anthropic's; it is used only
    as a better-than-chars/4 stand-in and is labelled as such. With nothing
    importable, falls back to the same chars/4 heuristic the server itself
    budgets with, which keeps these numbers directly comparable to the
    `truncated: true` behaviour a caller actually observes.
    """
    try:
        import tiktoken
    except ImportError:
        pass
    else:
        enc = tiktoken.get_encoding("cl100k_base")
        return (lambda text: len(enc.encode(text)), "tiktoken cl100k_base (exact, OpenAI BPE)")

    return (
        lambda text: len(text) // 4,
        "chars/4 heuristic (no tokenizer installed; same estimator as "
        "placeroot.budget.estimate_tokens)",
    )


COUNT_TOKENS, TOKENIZER_LABEL = resolve_tokenizer()


@dataclass
class Measurement:
    """One measured JSON payload."""

    label: str
    chars: int
    bytes_: int
    tokens: int

    @classmethod
    def of(cls, label: str, text: str) -> Measurement:
        return cls(
            label=label,
            chars=len(text),
            bytes_=len(text.encode("utf-8")),
            tokens=COUNT_TOKENS(text),
        )


# ----------------------------------------------------------------------
# (a) Schema surface.
# ----------------------------------------------------------------------


def wire_json(obj) -> str:
    """Compact JSON, matching how the MCP SDK serializes protocol messages
    (no whitespace padding). Whitespace would otherwise inflate every schema
    number by a few percent against what a client actually receives."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def tool_definitions() -> list[dict]:
    """Every registered tool, as MCP serializes it in a `tools/list` reply.

    Introspected from the live registry rather than a hand-maintained list,
    so adding a tool changes this benchmark's numbers automatically.
    """
    tools = asyncio.run(server.mcp.list_tools())
    return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]


def measure_schema_surface() -> list[tuple[str, Measurement, Measurement, Measurement]]:
    """Per tool: (name, whole definition, description only, inputSchema only).

    The description/schema split matters because they are tuned by different
    means — prose can be shortened by editing, schema size is driven by the
    parameter list.
    """
    rows = []
    for definition in tool_definitions():
        name = definition["name"]
        rows.append(
            (
                name,
                Measurement.of(name, wire_json(definition)),
                Measurement.of("description", definition.get("description", "")),
                Measurement.of("inputSchema", wire_json(definition.get("inputSchema", {}))),
            )
        )
    return rows


# ----------------------------------------------------------------------
# (b) Response cost, on committed fixtures.
# ----------------------------------------------------------------------


@contextmanager
def fixture_data():
    """Point every query layer at the committed fixtures.

    Mirrors the autouse `offline_data` fixture in tests/conftest.py (pinned
    release, tile cache off, one fixture per theme) so the benchmark answers
    from exactly the data the offline test suite asserts against — without
    needing pytest to be driving.
    """
    import os

    previous = {
        "PLACEROOT_OVERTURE_RELEASE": os.environ.get("PLACEROOT_OVERTURE_RELEASE"),
        "PLACEROOT_CACHE": os.environ.get("PLACEROOT_CACHE"),
    }
    os.environ["PLACEROOT_OVERTURE_RELEASE"] = release.PINNED_RELEASE
    os.environ["PLACEROOT_CACHE"] = "off"
    overture.set_data_path(str(FIXTURE_PATH))
    overture.set_data_path(str(DIVISION_AREAS_FIXTURE_PATH), theme="divisions")
    overture.set_data_path(str(DIVISIONS_FIXTURE_PATH), theme="divisions", type_="division")
    overture.set_data_path(str(ADDRESSES_FIXTURE_PATH), theme="addresses", type_="address")
    routing.set_data_path(str(TRANSPORTATION_FIXTURE_PATH))
    buildings.set_data_path(str(BUILDINGS_FIXTURE_PATH))
    routing.clear_graph_cache()
    try:
        yield
    finally:
        overture.set_data_path(None)
        overture.clear_division_geometry_cache()
        overture.set_data_path(None, theme="divisions")
        overture.set_data_path(None, theme="divisions", type_="division")
        overture.set_data_path(None, theme="addresses", type_="address")
        routing.set_data_path(None)
        buildings.set_data_path(None)
        routing.clear_graph_cache()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# Grid nodes from the routing fixture (scripts/build_routing_fixture.py):
# a 20x20 street grid at 100m spacing. (10, 10) is the interior node
# tests/test_routing.py's isochrone tests use; (2, 2) -> (2, 5) is the
# known-connected pair tests/test_route.py routes between.
ISO_LAT, ISO_LON = fx.node_latlon(10, 10)
ROUTE_FROM_LAT, ROUTE_FROM_LON = fx.node_latlon(2, 2)
ROUTE_TO_LAT, ROUTE_TO_LON = fx.node_latlon(2, 5)


@dataclass
class Scenario:
    name: str
    question: str
    call: Callable[[], dict]


SCENARIOS: list[Scenario] = [
    Scenario(
        "find_places (point, 1km)",
        "What named places are within 1km of the fixture center?",
        lambda: server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10),
    ),
    Scenario(
        "find_places (point, category filter)",
        "Which coffee shops are within 1km?",
        lambda: server.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000, category="coffee_shop", limit=10
        ),
    ),
    Scenario(
        "geocode",
        "Where is 'Brooklyn'?",
        lambda: server.geocode("Brooklyn", limit=5),
    ),
    Scenario(
        "place_details",
        "Tell me about the place named 'Roastery' near the fixture center.",
        lambda: server.place_details(
            name="Roastery", lat=CENTER_LAT, lon=CENTER_LON, radius_m=1000
        ),
    ),
    Scenario(
        "summarize_area",
        "What's in this 1km area?",
        lambda: server.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000),
    ),
    Scenario(
        "route (walk)",
        "How do I walk from one grid corner to another?",
        lambda: server.route(
            ROUTE_FROM_LAT, ROUTE_FROM_LON, ROUTE_TO_LAT, ROUTE_TO_LON, mode="walk",
            confirm=True,
        ),
    ),
    Scenario(
        "isochrone (15min walk)",
        "How far can I walk in 15 minutes from here?",
        lambda: server.isochrone(ISO_LAT, ISO_LON, minutes=15, mode="walk"),
    ),
]


@dataclass
class ScenarioResult:
    scenario: Scenario
    measurement: Measurement
    payload: dict


def run_scenarios(scenarios: list[Scenario] = SCENARIOS) -> list[ScenarioResult]:
    """Every scenario's real tool response, measured.

    An `{"error": ...}` response is measured and reported like any other —
    a tool that answers cheaply by failing is not an efficiency win, and
    hiding it would make the aggregate a lie.
    """
    results = []
    with fixture_data():
        for scenario in scenarios:
            payload = scenario.call()
            results.append(
                ScenarioResult(
                    scenario=scenario,
                    measurement=Measurement.of(scenario.name, wire_json(payload)),
                    payload=payload,
                )
            )
    return results


# ----------------------------------------------------------------------
# Reporting.
# ----------------------------------------------------------------------


def render_schema_table(rows) -> str:
    lines = [
        "| tool | description tokens | inputSchema tokens | total tokens | total chars |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, whole, desc, schema in sorted(rows, key=lambda r: -r[1].tokens):
        lines.append(
            f"| `{name}` | {desc.tokens} | {schema.tokens} | **{whole.tokens}** | {whole.chars} |"
        )
    total_tokens = sum(r[1].tokens for r in rows)
    total_chars = sum(r[1].chars for r in rows)
    desc_tokens = sum(r[2].tokens for r in rows)
    schema_tokens = sum(r[3].tokens for r in rows)
    lines.append(
        f"| **all {len(rows)} tools** | {desc_tokens} | {schema_tokens} | "
        f"**{total_tokens}** | {total_chars} |"
    )
    return "\n".join(lines)


def render_scenario_table(results: list[ScenarioResult]) -> str:
    lines = [
        "| scenario | question | response tokens | response chars |",
        "|---|---|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| `{r.scenario.name}` | {r.scenario.question} | "
            f"**{r.measurement.tokens}** | {r.measurement.chars} |"
        )
    return "\n".join(lines)


def render_generated_section(rows, results: list[ScenarioResult]) -> str:
    schema_tokens = [r[1].tokens for r in rows]
    total_schema = sum(schema_tokens)
    response_tokens = sorted(r.measurement.tokens for r in results)
    median_response = response_tokens[len(response_tokens) // 2]
    breakeven = total_schema / median_response if median_response else float("nan")

    return "\n".join(
        [
            GENERATED_BEGIN,
            "",
            f"Generated {time.strftime('%Y-%m-%d')} by "
            "`uv run python benchmarks/token_efficiency.py --write`.",
            "",
            f"- Token counting method: **{TOKENIZER_LABEL}**",
            f"- Overture release pinned for the fixture run: `{release.PINNED_RELEASE}`",
            f"- Tools registered: **{len(rows)}**",
            f"- Total schema surface: **{total_schema} tokens** "
            f"({sum(r[1].chars for r in rows)} chars, "
            f"{sum(r[1].bytes_ for r in rows)} bytes)",
            f"- Schema cost per tool: min {min(schema_tokens)}, "
            f"median {sorted(schema_tokens)[len(schema_tokens) // 2]}, "
            f"max {max(schema_tokens)} tokens",
            f"- Median scenario response: **{median_response} tokens** "
            f"(range {response_tokens[0]}-{response_tokens[-1]})",
            f"- Break-even: the schema surface costs about as much as "
            f"**{breakeven:.0f} median answers**",
            "",
            "### Schema surface (paid once per conversation)",
            "",
            render_schema_table(rows),
            "",
            "### Response cost (paid per tool call, measured on committed fixtures)",
            "",
            render_scenario_table(results),
            "",
            GENERATED_END,
        ]
    )


def write_doc(section: str, doc_path: Path = DOC_PATH) -> None:
    """Replace the generated region of docs/benchmarks.md in place.

    The prose around the markers is hand-written and must survive a rerun;
    the numbers between them are never hand-edited.
    """
    text = doc_path.read_text()
    start = text.index(GENERATED_BEGIN)
    end = text.index(GENERATED_END) + len(GENERATED_END)
    doc_path.write_text(text[:start] + section + text[end:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the generated numbers section of docs/benchmarks.md",
    )
    args = parser.parse_args(argv)

    rows = measure_schema_surface()
    results = run_scenarios()
    section = render_generated_section(rows, results)
    print(section)

    if args.write:
        write_doc(section)
        print(f"\nWrote {DOC_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
