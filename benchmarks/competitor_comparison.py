"""Head-to-head token benchmark: PlaceRoot vs Mapbox MCP vs Google Maps MCP (issue #208).

`benchmarks/token_efficiency.py` measures PlaceRoot against itself. This one
puts the same two numbers — schema surface and answer size — next to the two
other MCP servers an agent might install for spatial questions:

- **Mapbox MCP** (github.com/mapbox/mcp-server), the category leader.
- **Google Maps MCP**, the archived Anthropic reference server
  (github.com/modelcontextprotocol/servers-archived, `src/google-maps`).

Everything about the competitors comes from snapshots vendored under
`benchmarks/competitors/`, with repo, commit and capture date recorded in
`benchmarks/competitors/provenance.json`. Nothing here touches the network:
their tool lists and their answers were captured once, by the scripts in
`benchmarks/competitors/capture/`, and committed. PlaceRoot's own numbers are
measured live — schema surface from the registry, answers from the committed
test fixtures — reusing `token_efficiency`'s machinery so the two benchmarks
cannot drift apart.

    uv run python benchmarks/competitor_comparison.py           # print the report
    uv run python benchmarks/competitor_comparison.py --write   # + update docs/benchmarks-vs.md

Token counting is the **chars/4 heuristic**, `placeroot.budget.estimate_tokens`,
applied identically to all three servers. Unlike `token_efficiency.py` this
script never opportunistically upgrades to `tiktoken`: a comparison has to use
one ruler, and it has to give the same answer on every machine so the committed
document can be drift-guarded byte for byte.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

COMPETITORS = Path(__file__).resolve().parent / "competitors"
DOC_PATH = REPO_ROOT / "docs" / "benchmarks-vs.md"
GENERATED_BEGIN = "<!-- BEGIN GENERATED: benchmarks/competitor_comparison.py -->"
GENERATED_END = "<!-- END GENERATED -->"

# benchmarks/ is not part of the installed package, so its sibling is loaded by
# file path — the same approach tests/test_benchmark_script.py uses.
_SPEC = importlib.util.spec_from_file_location(
    "token_efficiency", Path(__file__).resolve().parent / "token_efficiency.py"
)
token_efficiency = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("token_efficiency", token_efficiency)
_SPEC.loader.exec_module(token_efficiency)

from placeroot import server  # noqa: E402
from placeroot.budget import CHARS_PER_TOKEN  # noqa: E402
from tests._routing_fixture import build_routing_fixture as fx  # noqa: E402
from tests.conftest import CENTER_LAT, CENTER_LON  # noqa: E402

wire_json = token_efficiency.wire_json
fixture_data = token_efficiency.fixture_data

PLACEROOT = "placeroot"
MAPBOX = "mapbox-mcp"
GOOGLE = "google-maps-archived"
SERVER_ORDER = [PLACEROOT, MAPBOX, GOOGLE]

TOKENIZER_LABEL = (
    f"chars/{CHARS_PER_TOKEN} heuristic — `placeroot.budget.estimate_tokens`, "
    "applied identically to all three servers"
)


def count_tokens(text: str) -> int:
    """The estimator `placeroot.budget.estimate_tokens` uses, on raw text.

    `estimate_tokens` takes an object and JSON-encodes it first; here the text
    already *is* the payload as its server put it on the wire, competitor
    pretty-printing and all, so it is divided directly.
    """
    return len(text) // CHARS_PER_TOKEN


# ----------------------------------------------------------------------
# Vendored snapshots.
# ----------------------------------------------------------------------


def load_provenance() -> dict:
    return json.loads((COMPETITORS / "provenance.json").read_text())


def competitor_tools(name: str) -> list[dict]:
    path = COMPETITORS / name / "tools_list.json"
    return json.loads(path.read_text())["tools"]


def competitor_answers() -> list[dict]:
    return json.loads((COMPETITORS / "answers.json").read_text())


# ----------------------------------------------------------------------
# (a) Schema surface.
# ----------------------------------------------------------------------

# The tools each server would actually register to answer the six scenarios
# below — the apples-to-apples surface, next to the whole-install surface.
# Names are checked against the snapshots, so a rename upstream fails loudly.
SCENARIO_TOOLS: dict[str, list[str]] = {
    PLACEROOT: [
        "geocode",
        "reverse_geocode",
        "find_places",
        "route",
        "isochrone",
        "distance_matrix",
    ],
    MAPBOX: [
        "search_and_geocode_tool",
        "reverse_geocode_tool",
        "category_search_tool",
        "directions_tool",
        "isochrone_tool",
        "matrix_tool",
    ],
    GOOGLE: [
        "maps_geocode",
        "maps_reverse_geocode",
        "maps_search_places",
        "maps_directions",
        "maps_distance_matrix",
    ],
}


@dataclass
class SchemaSurface:
    server: str
    tool_count: int
    tokens: int
    chars: int
    subset_names: list[str]
    subset_tool_count: int
    subset_tokens: int


def measure_surface(name: str, definitions: list[dict]) -> SchemaSurface:
    by_name = {d["name"]: d for d in definitions}
    wanted = SCENARIO_TOOLS[name]
    missing = [t for t in wanted if t not in by_name]
    if missing:
        raise KeyError(f"{name}: scenario tools missing from the tool list: {missing}")
    texts = [wire_json(d) for d in definitions]
    subset = [wire_json(by_name[t]) for t in wanted]
    return SchemaSurface(
        server=name,
        tool_count=len(definitions),
        tokens=sum(count_tokens(t) for t in texts),
        chars=sum(len(t) for t in texts),
        subset_names=wanted,
        subset_tool_count=len(wanted),
        subset_tokens=sum(count_tokens(t) for t in subset),
    )


def measure_all_surfaces() -> dict[str, SchemaSurface]:
    return {
        PLACEROOT: measure_surface(PLACEROOT, token_efficiency.tool_definitions()),
        MAPBOX: measure_surface(MAPBOX, competitor_tools(MAPBOX)),
        GOOGLE: measure_surface(GOOGLE, competitor_tools(GOOGLE)),
    }


# ----------------------------------------------------------------------
# (b) Answer size, on six identical questions.
# ----------------------------------------------------------------------

ROUTE_FROM_LAT, ROUTE_FROM_LON = fx.node_latlon(2, 2)
ROUTE_TO_LAT, ROUTE_TO_LON = fx.node_latlon(2, 5)
ISO_LAT, ISO_LON = fx.node_latlon(10, 10)
_MATRIX_POINTS = [
    {"lat": lat, "lon": lon}
    for lat, lon in (fx.node_latlon(2, 2), fx.node_latlon(2, 5), fx.node_latlon(5, 2))
]

# Scenario key -> (question asked of every server, how PlaceRoot answers it).
# The keys are the ones `benchmarks/competitors/answers.json` was captured
# under; a mismatch is a hard error rather than a silently empty cell.
SCENARIOS: list[tuple[str, str, object]] = [
    (
        "geocode_address",
        "Geocode one address / place name.",
        lambda: server.geocode("Brooklyn", limit=5),
    ),
    (
        "reverse_geocode",
        "What address is at this coordinate?",
        lambda: server.reverse_geocode(CENTER_LAT, CENTER_LON),
    ),
    (
        "nearest_coffee",
        "Coffee shops near this point.",
        lambda: server.find_places(
            CENTER_LAT, CENTER_LON, radius_m=1000, category="coffee_shop", limit=10
        ),
    ),
    (
        "route_a_to_b",
        "Route from A to B.",
        lambda: server.route(
            ROUTE_FROM_LAT, ROUTE_FROM_LON, ROUTE_TO_LAT, ROUTE_TO_LON, mode="walk"
        ),
    ),
    (
        "isochrone_15min",
        "How far can I get in 15 minutes?",
        lambda: server.isochrone(ISO_LAT, ISO_LON, minutes=15, mode="walk"),
    ),
    (
        "matrix_3x3",
        "3x3 distance matrix between six points.",
        lambda: server.distance_matrix(_MATRIX_POINTS, _MATRIX_POINTS),
    ),
]


@dataclass
class Answer:
    server: str
    scenario: str
    tokens: int
    chars: int
    minified_tokens: int | None  # None when the payload is not JSON
    note: str = ""


def minified_tokens(text: str) -> int | None:
    """Tokens after collapsing pretty-print whitespace, or None if not JSON.

    Both competitors `JSON.stringify(..., null, 2)` their payloads while
    PlaceRoot serializes compact. Charging them for indentation they really do
    send is fair, but it is not a design difference, so the whitespace-free
    number is reported next to it.
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return count_tokens(wire_json(parsed))


def placeroot_answers() -> list[Answer]:
    answers = []
    with fixture_data():
        for key, _question, call in SCENARIOS:
            payload = call()
            if "error" in payload:
                raise RuntimeError(f"{key}: PlaceRoot answered with an error: {payload}")
            text = wire_json(payload)
            answers.append(
                Answer(
                    server=PLACEROOT,
                    scenario=key,
                    tokens=count_tokens(text),
                    chars=len(text),
                    minified_tokens=count_tokens(text),
                )
            )
    return answers


def snapshot_answers() -> list[Answer]:
    known = {key for key, _q, _c in SCENARIOS}
    answers = []
    for row in competitor_answers():
        if row["scenario"] not in known:
            raise KeyError(f"answers.json holds an unknown scenario: {row['scenario']}")
        if row["is_error"]:
            raise RuntimeError(f"{row['server']}/{row['scenario']} captured an error response")
        text = row["response_text"]
        answers.append(
            Answer(
                server=row["server"],
                scenario=row["scenario"],
                tokens=count_tokens(text),
                chars=len(text),
                minified_tokens=minified_tokens(text),
            )
        )
    return answers


def answer_index() -> dict[tuple[str, str], Answer]:
    return {(a.server, a.scenario): a for a in placeroot_answers() + snapshot_answers()}


# ----------------------------------------------------------------------
# Reporting.
# ----------------------------------------------------------------------

LABELS = {
    PLACEROOT: "PlaceRoot",
    MAPBOX: "Mapbox MCP",
    GOOGLE: "Google Maps MCP (archived)",
}


def render_schema_table(surfaces: dict[str, SchemaSurface]) -> str:
    lines = [
        "| server | tools registered | schema surface (tokens) | schema surface (chars) "
        "| the 6-scenario subset | subset tokens |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in SERVER_ORDER:
        s = surfaces[name]
        lines.append(
            f"| {LABELS[name]} | {s.tool_count} | **{s.tokens}** | {s.chars} "
            f"| {s.subset_tool_count} | **{s.subset_tokens}** |"
        )
    return "\n".join(lines)


def render_answer_table(index: dict[tuple[str, str], Answer]) -> str:
    lines = [
        "| scenario | PlaceRoot | Mapbox MCP | Google Maps MCP (archived) |",
        "|---|---:|---:|---:|",
    ]
    for key, question, _call in SCENARIOS:
        cells = []
        for name in SERVER_ORDER:
            answer = index.get((name, key))
            if answer is None:
                cells.append("not measured")
            elif answer.minified_tokens is None:
                cells.append(f"**{answer.tokens}** (text)")
            elif answer.minified_tokens == answer.tokens:
                cells.append(f"**{answer.tokens}**")
            else:
                cells.append(f"**{answer.tokens}** ({answer.minified_tokens} minified)")
        lines.append(f"| `{key}` — {question} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_provenance_table(provenance: dict) -> str:
    lines = [
        "| server | source | commit | version | captured |",
        "|---|---|---|---|---|",
    ]
    for name in (MAPBOX, GOOGLE):
        entry = provenance["servers"][name]
        path = f"/{entry['path']}" if entry.get("path") else ""
        lines.append(
            f"| {LABELS[name]} | [{entry['repo'].removeprefix('https://github.com/')}{path}]"
            f"({entry['repo']}) | `{entry['commit'][:12]}` ({entry['commit_date']}) "
            f"| {entry['package_version']} | {provenance['captured']} |"
        )
    return "\n".join(lines)


def render_generated_section() -> str:
    provenance = load_provenance()
    surfaces = measure_all_surfaces()
    index = answer_index()

    ours = surfaces[PLACEROOT]
    mapbox = surfaces[MAPBOX]
    google = surfaces[GOOGLE]
    subset_vs_mapbox = mapbox.subset_tokens / ours.subset_tokens
    whole_vs_mapbox = mapbox.tokens / ours.tokens

    measured = [key for key, _q, _c in SCENARIOS if (MAPBOX, key) in index]
    ours_total = sum(index[(PLACEROOT, k)].tokens for k in measured)
    theirs_total = sum(index[(MAPBOX, k)].tokens for k in measured)
    # A plain-text answer has no pretty-print whitespace to strip, so it
    # counts as itself rather than as a hole in the total.
    theirs_min_total = sum(
        index[(MAPBOX, k)].minified_tokens
        if index[(MAPBOX, k)].minified_tokens is not None
        else index[(MAPBOX, k)].tokens
        for k in measured
    )

    not_measured = provenance["answers"]["not_measured"]
    understatements = provenance["answers"]["known_understatements"]

    return "\n".join(
        [
            GENERATED_BEGIN,
            "",
            "Regenerate with `uv run python benchmarks/competitor_comparison.py --write`. "
            "Competitor figures come from the snapshots in `benchmarks/competitors/`; "
            "PlaceRoot's are measured live against the committed test fixtures. No network, "
            "either way.",
            "",
            f"- Token counting method: **{TOKENIZER_LABEL}**",
            f"- Snapshots captured: **{provenance['captured']}**",
            f"- Schema surface, whole install: PlaceRoot **{ours.tokens}** tokens "
            f"({ours.tool_count} tools) · Mapbox **{mapbox.tokens}** ({mapbox.tool_count} tools) "
            f"· Google Maps **{google.tokens}** ({google.tool_count} tools)",
            f"- Schema surface, the six tools each server needs for the scenarios below: "
            f"PlaceRoot **{ours.subset_tokens}** · Mapbox **{mapbox.subset_tokens}** "
            f"({subset_vs_mapbox:.1f}x ours) · Google Maps **{google.subset_tokens}** "
            f"({google.subset_tool_count} tools — no isochrone tool exists)",
            f"- Whole-install surface: Mapbox is **{whole_vs_mapbox:.1f}x** PlaceRoot's, "
            f"on {mapbox.tool_count} tools against {ours.tool_count}",
            f"- Answers, over the {len(measured)} scenarios both PlaceRoot and Mapbox answer: "
            f"PlaceRoot **{ours_total}** tokens total, Mapbox **{theirs_total}** "
            f"({theirs_min_total} with pretty-print whitespace removed)",
            "",
            "### Where the competitor numbers come from",
            "",
            render_provenance_table(provenance),
            "",
            "### Schema surface (paid once per conversation, whether or not a tool is called)",
            "",
            render_schema_table(surfaces),
            "",
            "### Answer size (paid per tool call)",
            "",
            render_answer_table(index),
            "",
            "Competitor answers are their real servers' output: each server was run over "
            "stdio with its upstream HTTP calls pointed at a local stub replying with the "
            "vendor's own documented example response for that endpoint "
            "(`benchmarks/competitors/upstream_examples/`). Both pretty-print their JSON with "
            "two-space indentation, so the whitespace-free count is shown alongside. "
            "PlaceRoot serializes compact, which is why its two numbers are equal.",
            "",
            "### What these numbers are not",
            "",
            *[f"- {line}" for line in understatements],
            *[
                f"- **{LABELS[name]}, not measured**: {reason}"
                for name, reason in not_measured.items()
            ],
            "",
            GENERATED_END,
        ]
    )


def write_doc(section: str, doc_path: Path = DOC_PATH) -> None:
    """Replace the generated region of docs/benchmarks-vs.md in place."""
    text = doc_path.read_text()
    start = text.index(GENERATED_BEGIN)
    end = text.index(GENERATED_END) + len(GENERATED_END)
    doc_path.write_text(text[:start] + section + text[end:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the generated numbers section of docs/benchmarks-vs.md",
    )
    args = parser.parse_args(argv)

    section = render_generated_section()
    print(section)
    if args.write:
        write_doc(section)
        print(f"\nWrote {DOC_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
