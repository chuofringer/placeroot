"""Head-to-head token benchmark: PlaceRoot vs Mapbox MCP vs Google Maps MCP (issue #208).

`benchmarks/token_efficiency.py` measures PlaceRoot against itself. This one
puts the same two numbers — schema surface and answer size — next to the two
other MCP servers an agent might install for spatial questions:

- **Mapbox MCP** (github.com/mapbox/mcp-server), the category leader.
- **Google Maps MCP**, the archived Anthropic reference server
  (github.com/modelcontextprotocol/servers-archived, `src/google-maps`).

Every published figure is a pure function of committed files. Competitor tool
lists and answers are snapshots under `benchmarks/competitors/`, captured once
by the scripts in `benchmarks/competitors/capture/`, with repo, commit and
capture date in `benchmarks/competitors/provenance.json`. *Our* answers are
snapshotted the same way, in `benchmarks/competitors/placeroot_answers.json` —
held to the rule we hold them to, and for a concrete reason: routing and
geometry do floating-point work whose last digits differ between platforms, so
a live rerun of the scenarios costs a few tokens more or less on Linux than on
macOS, and a page rebuilt from live numbers could never be drift-guarded byte
for byte. The live run still happens — in the tolerance test in
tests/test_competitor_comparison.py, which fails if the snapshot rots.

PlaceRoot's *schema surface* is still read live from the registry: it is exact
integer-free text, identical on every platform, and reading it live is what
makes a newly added tool move this page.

    uv run python benchmarks/competitor_comparison.py           # print the report
    uv run python benchmarks/competitor_comparison.py --write   # + update docs/benchmarks-vs.md
    uv run python benchmarks/competitor_comparison.py --capture-answers  # re-snapshot ours

Scenario machinery is reused from `token_efficiency` so the two benchmarks
cannot drift apart.

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
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

COMPETITORS = Path(__file__).resolve().parent / "competitors"
PLACEROOT_ANSWERS_PATH = COMPETITORS / "placeroot_answers.json"
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

from placeroot import release, server  # noqa: E402
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


# A `tools/list` entry may carry fields one server publishes and another does
# not, and the difference is not a verbosity difference. Mapbox declares an
# `outputSchema` on 28 of its 29 tools; PlaceRoot declares none, so the field is
# structurally absent from our side rather than smaller. Counting it would make
# the ratio mostly a report of that one choice.
#
# So every surface is measured twice: `tokens` is the verbatim wire cost an
# agent really pays, and `common_tokens` restricts both sides to the fields all
# three servers actually publish. The published ratios are the common-field
# ones; the verbatim numbers sit next to them, with the gap named.
COMMON_FIELDS = ("name", "title", "description", "inputSchema", "annotations")


def common_fields_only(definition: dict) -> dict:
    return {k: v for k, v in definition.items() if k in COMMON_FIELDS}


@dataclass
class SchemaSurface:
    server: str
    tool_count: int
    tokens: int
    chars: int
    common_tokens: int
    subset_names: list[str]
    subset_tool_count: int
    subset_tokens: int
    subset_common_tokens: int
    # Fields this server publishes that are outside COMMON_FIELDS, and what
    # they cost across the whole install — the gap between the two counts.
    extra_fields: list[str]

    @property
    def extra_tokens(self) -> int:
        return self.tokens - self.common_tokens


def measure_surface(name: str, definitions: list[dict]) -> SchemaSurface:
    by_name = {d["name"]: d for d in definitions}
    wanted = SCENARIO_TOOLS[name]
    missing = [t for t in wanted if t not in by_name]
    if missing:
        raise KeyError(f"{name}: scenario tools missing from the tool list: {missing}")
    texts = [wire_json(d) for d in definitions]
    subset = [wire_json(by_name[t]) for t in wanted]
    common = [wire_json(common_fields_only(d)) for d in definitions]
    subset_common = [wire_json(common_fields_only(by_name[t])) for t in wanted]
    extra = sorted({k for d in definitions for k in d if k not in COMMON_FIELDS})
    return SchemaSurface(
        server=name,
        tool_count=len(definitions),
        tokens=sum(count_tokens(t) for t in texts),
        chars=sum(len(t) for t in texts),
        common_tokens=sum(count_tokens(t) for t in common),
        subset_names=wanted,
        subset_tool_count=len(wanted),
        subset_tokens=sum(count_tokens(t) for t in subset),
        subset_common_tokens=sum(count_tokens(t) for t in subset_common),
        extra_fields=extra,
    )


def placeroot_tool_definitions() -> list[dict]:
    """Our live registry, refusing to measure a narrowed one.

    `PLACEROOT_TOOLS` can register a subset, and a subset that drops a scenario
    tool would otherwise produce a KeyError deep inside `measure_surface` or,
    worse, a smaller published surface than an unconfigured install pays. The
    page documents the default install, so say so.
    """
    definitions = token_efficiency.tool_definitions()
    missing = [t for t in SCENARIO_TOOLS[PLACEROOT] if t not in {d["name"] for d in definitions}]
    if missing:
        raise RuntimeError(
            "the tool registry is narrowed (PLACEROOT_TOOLS="
            f"{os.environ.get('PLACEROOT_TOOLS', '<unset>')}), so it is missing scenario "
            f"tools {missing}. This benchmark reports the default install; rerun with "
            "PLACEROOT_TOOLS unset."
        )
    return definitions


def measure_all_surfaces() -> dict[str, SchemaSurface]:
    return {
        PLACEROOT: measure_surface(PLACEROOT, placeroot_tool_definitions()),
        MAPBOX: measure_surface(MAPBOX, competitor_tools(MAPBOX)),
        GOOGLE: measure_surface(GOOGLE, competitor_tools(GOOGLE)),
    }


# ----------------------------------------------------------------------
# (b) Answer size, on six matched questions.
#
# "Matched", not "identical": each competitor server really is asked the
# scenario's question, but the stub behind it replies with the vendor's
# published example for that endpoint whatever the arguments say. So their cell
# is the size of their code's rendering of a payload they publish, not of an
# answer to our exact question. Where that changes what is being compared —
# the isochrone example carries three contours to our one — the difference is
# recorded in provenance.json and printed next to the table.
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
        "3x3 distance matrix over three points.",
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


def live_placeroot_answers() -> list[Answer]:
    """Run the six scenarios for real, against the committed fixtures.

    Deterministic on any one machine, but *not* across machines: routing and
    geometry do floating-point work whose last digits differ between
    platforms, and a digit is a character, and characters are what chars/4
    counts. Observed on this benchmark: `route_a_to_b` 41 tokens on macOS
    against 42 on Linux, `isochrone_15min` 208 against 205.

    So this is not what the published table is built from — see
    `vendored_placeroot_answers()`. It is what
    `--capture-answers` records, and what the tolerance test in
    tests/test_competitor_comparison.py checks the recording against.
    """
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
                    # Measured, not assumed: the claim on the page is that we
                    # serialize compact, so run our text through the same
                    # whitespace-stripping the competitors' text gets and let
                    # the two numbers agree on their own.
                    minified_tokens=minified_tokens(text),
                )
            )
    return answers


def vendored_placeroot_answers() -> list[Answer]:
    """Our own answers, snapshotted the same way the competitors' are.

    Holding all three servers to one rule — the published numbers come from
    committed files — is what makes `render_generated_section()` a pure
    function of the repo and lets the drift guard be byte-exact everywhere
    instead of only on the machine that last regenerated the page.
    """
    known = {key for key, _q, _c in SCENARIOS}
    answers = []
    for row in json.loads(PLACEROOT_ANSWERS_PATH.read_text())["answers"]:
        if row["scenario"] not in known:
            raise KeyError(f"placeroot_answers.json holds an unknown scenario: {row['scenario']}")
        text = row["response_text"]
        answers.append(
            Answer(
                server=PLACEROOT,
                scenario=row["scenario"],
                tokens=count_tokens(text),
                chars=len(text),
                minified_tokens=minified_tokens(text),
            )
        )
    return answers


def capture_placeroot_answers() -> None:
    """Rewrite the vendored snapshot of our own answers from a live run.

    Run by hand, on the machine whose platform the snapshot then records —
    exactly like the competitors' capture scripts, and for the same reason:
    a published number has to name where it came from.
    """
    with fixture_data():
        rows = []
        for key, question, call in SCENARIOS:
            payload = call()
            if "error" in payload:
                raise RuntimeError(f"{key}: PlaceRoot answered with an error: {payload}")
            rows.append(
                {"scenario": key, "question": question, "response_text": wire_json(payload)}
            )
    snapshot = {
        "captured_on": platform.platform(),
        "python": platform.python_version(),
        "overture_release": release.PINNED_RELEASE,
        "method": (
            "benchmarks/competitor_comparison.py --capture-answers: the six scenarios run "
            "through the same placeroot.server functions the MCP server exposes, answered "
            "from the committed fixtures in tests/fixtures/ with the Overture release pinned "
            "and the tile cache off. Snapshotted rather than recomputed at publish time "
            "because floating-point differences in routing and geometry change digit counts "
            "between platforms, which would make the generated page platform-dependent."
        ),
        "answers": rows,
    }
    PLACEROOT_ANSWERS_PATH.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"Wrote {PLACEROOT_ANSWERS_PATH}", file=sys.stderr)


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
    """One answer per (server, scenario) — a duplicate is a capture bug.

    Silently letting the last row win would let a stale re-capture sit in the
    file and quietly set a published number.
    """
    index: dict[tuple[str, str], Answer] = {}
    for answer in vendored_placeroot_answers() + snapshot_answers():
        key = (answer.server, answer.scenario)
        if key in index:
            raise KeyError(f"duplicate captured answer for {key[0]}/{key[1]}")
        index[key] = answer
    return index


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
        "| server | tools registered | whole install, verbatim | whole install, common fields "
        "| the 6-scenario subset | subset verbatim | subset common fields |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in SERVER_ORDER:
        s = surfaces[name]
        lines.append(
            f"| {LABELS[name]} | {s.tool_count} | {s.tokens} | **{s.common_tokens}** "
            f"| {s.subset_tool_count} | {s.subset_tokens} | **{s.subset_common_tokens}** |"
        )
    return "\n".join(lines)


def render_field_note(surfaces: dict[str, SchemaSurface]) -> str:
    """Name the fields each server publishes, and what the extras cost.

    Without this the two columns above look like a rounding choice rather than
    the difference between "they are more verbose" and "they publish a field we
    don't publish at all".
    """
    common = ", ".join(f"`{f}`" for f in COMMON_FIELDS)
    lines = [
        f"**Common fields** are {common} — the ones every server here puts in a "
        "`tools/list` entry. The verbatim column additionally counts whatever else "
        "a server sends, and that is not the same kind of difference:",
        "",
    ]
    for name in SERVER_ORDER:
        s = surfaces[name]
        if s.extra_fields:
            extra = ", ".join(f"`{f}`" for f in s.extra_fields)
            lines.append(
                f"- **{LABELS[name]}** also sends {extra} — **{s.extra_tokens}** tokens "
                f"across the install, **{s.subset_tokens - s.subset_common_tokens}** across "
                "the six-tool subset."
            )
        else:
            lines.append(f"- **{LABELS[name]}** sends the common fields and nothing else.")
    lines += [
        "",
        "Mapbox declares an `outputSchema` on almost every tool; PlaceRoot declares none, "
        "so on our side the field is *absent*, not smaller. An agent really does pay the "
        "verbatim number, so it is in the table — but a ratio built on it would mostly be "
        "reporting that one choice, which is why the headline ratios above use the common "
        "fields. If PlaceRoot adds output schemas the two columns will converge, and this "
        "page will say so on its own.",
    ]
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
    # Headline ratios are the common-field ones; see render_field_note().
    subset_vs_mapbox = mapbox.subset_common_tokens / ours.subset_common_tokens
    whole_vs_mapbox = mapbox.common_tokens / ours.common_tokens
    subset_vs_mapbox_verbatim = mapbox.subset_tokens / ours.subset_tokens
    whole_vs_mapbox_verbatim = mapbox.tokens / ours.tokens

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

    ours_snapshot = json.loads(PLACEROOT_ANSWERS_PATH.read_text())
    not_measured = provenance["answers"]["not_measured"]
    understatements = provenance["answers"]["known_understatements"]

    return "\n".join(
        [
            GENERATED_BEGIN,
            "",
            "Regenerate with `uv run python benchmarks/competitor_comparison.py --write`. "
            "Every answer figure below — theirs and ours alike — comes from a snapshot "
            "committed under `benchmarks/competitors/`; PlaceRoot's schema surface is read "
            "live from the tool registry. No network, either way.",
            "",
            f"- Token counting method: **{TOKENIZER_LABEL}**",
            f"- Snapshots captured: **{provenance['captured']}**",
            f"- PlaceRoot's own answers were captured on **{ours_snapshot['captured_on']}** "
            f"(Python {ours_snapshot['python']}, Overture "
            f"`{ours_snapshot['overture_release']}`) and are snapshotted rather than "
            "recomputed here: floating-point differences in routing and geometry change "
            "digit counts between platforms, so a live rerun costs a few tokens more or "
            "less on Linux than on macOS. A tolerance test reruns them for real and fails "
            "if this snapshot drifts from what the code now answers.",
            "- Schema figures are counted twice: over the **common fields** every server "
            "here publishes, and **verbatim** over everything it sends. The ratios below "
            "are the common-field ones, because Mapbox declares an `outputSchema` that "
            "PlaceRoot does not declare at all — see the note under the table.",
            f"- Schema surface, whole install (common fields): PlaceRoot "
            f"**{ours.common_tokens}** tokens ({ours.tool_count} tools) · Mapbox "
            f"**{mapbox.common_tokens}** ({mapbox.tool_count} tools) · Google Maps "
            f"**{google.common_tokens}** ({google.tool_count} tools)",
            f"- Schema surface, the six tools each server needs for the scenarios below "
            f"(common fields): PlaceRoot **{ours.subset_common_tokens}** · Mapbox "
            f"**{mapbox.subset_common_tokens}** ({subset_vs_mapbox:.1f}x ours) · Google Maps "
            f"**{google.subset_common_tokens}** "
            f"({google.subset_tool_count} tools — no isochrone tool exists)",
            f"- Whole-install surface: Mapbox is **{whole_vs_mapbox:.1f}x** PlaceRoot's on "
            f"common fields, on {mapbox.tool_count} tools against {ours.tool_count}. "
            f"Verbatim — counting the output schemas Mapbox publishes and we don't — it is "
            f"{whole_vs_mapbox_verbatim:.1f}x ({mapbox.tokens} against {ours.tokens}), and "
            f"{subset_vs_mapbox_verbatim:.1f}x on the six-tool subset.",
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
            render_field_note(surfaces),
            "",
            "### Answer size (paid per tool call)",
            "",
            render_answer_table(index),
            "",
            "Competitor answers are their real servers' output: each server was run over "
            "stdio with its upstream HTTP calls pointed at a local stub replying with the "
            "vendor's own documented example response for that endpoint "
            "(`benchmarks/competitors/upstream_examples/`). The stub answers with that "
            "example whatever the request says, so a competitor cell is the size of their "
            "code's rendering of a payload they publish — not of an answer to our exact "
            "question, and not of the same content ours answered. Read the caveats below "
            "before comparing any row. Both pretty-print their JSON with two-space "
            "indentation, so the whitespace-free count is shown alongside; PlaceRoot "
            "serializes compact, and its two counts come out equal.",
            "",
            f"PlaceRoot's answers were captured the same way, on "
            f"{ours_snapshot['captured_on']}: the six scenarios run through the same "
            "`placeroot.server` functions the MCP server exposes, answered from the "
            "committed fixtures with the Overture release pinned and the tile cache off.",
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
    parser.add_argument(
        "--capture-answers",
        action="store_true",
        help=(
            "re-snapshot PlaceRoot's own scenario answers into "
            "benchmarks/competitors/placeroot_answers.json from a live fixture run, "
            "recording the platform it was captured on"
        ),
    )
    args = parser.parse_args(argv)

    if args.capture_answers:
        capture_placeroot_answers()

    section = render_generated_section()
    print(section)
    if args.write:
        write_doc(section)
        print(f"\nWrote {DOC_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
