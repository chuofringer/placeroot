"""Drift guard for benchmarks/competitor_comparison.py and docs/benchmarks-vs.md (issue #208).

Unlike tests/test_benchmark_script.py, this one *does* pin the committed
output: the whole point of a published head-to-head is that its numbers are
reproducible, so the generated region of docs/benchmarks-vs.md has to match
what the script produces right now, byte for byte. That is only safe because
the comparison deliberately uses a fixed estimator (chars/4, never tiktoken)
and reads *every* answer figure — the competitors' and our own — from vendored
snapshots rather than recomputing them. Our answers are snapshotted for the
same reason theirs are captured once: floating-point differences in routing and
geometry change digit counts between platforms, so a live rerun of the
scenarios costs a token or two more on Linux than on macOS, and byte-identity
would be unachievable across CI runners. The live run is not lost — it moves
into `test_vendored_placeroot_answers_match_a_live_run_within_tolerance`, which
fails if the snapshot rots.

If this test fails after a legitimate change — a tool added, a response shape
edited, a snapshot refreshed — the fix is to rerun:

    uv run python benchmarks/competitor_comparison.py --write   # doc only
    uv run python benchmarks/competitor_comparison.py --capture-answers --write

benchmarks/ isn't part of the installed package, so the script is loaded by
file path, the same way tests/test_benchmark_script.py loads its sibling.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPETITORS = REPO_ROOT / "benchmarks" / "competitors"

_SPEC = importlib.util.spec_from_file_location(
    "competitor_comparison", REPO_ROOT / "benchmarks" / "competitor_comparison.py"
)
competitor_comparison = importlib.util.module_from_spec(_SPEC)
sys.modules["competitor_comparison"] = competitor_comparison
_SPEC.loader.exec_module(competitor_comparison)

PLACEROOT = competitor_comparison.PLACEROOT
MAPBOX = competitor_comparison.MAPBOX
GOOGLE = competitor_comparison.GOOGLE


def test_snapshots_are_vendored_with_provenance():
    """Every competitor carries a repo, a commit and a capture date."""
    provenance = competitor_comparison.load_provenance()
    assert provenance["captured"]
    for name in (MAPBOX, GOOGLE):
        entry = provenance["servers"][name]
        assert entry["repo"].startswith("https://github.com/")
        assert len(entry["commit"]) == 40, f"{name}: want a full commit hash"
        assert entry["commit_date"]
        assert entry["package_version"]
        # The recorded path is repo-relative and must be the file actually read.
        assert (REPO_ROOT / entry["tools_list"]).is_file()
        assert entry["tools_list_method"], f"{name}: how the snapshot was taken is unrecorded"


def test_competitor_tool_lists_load_and_carry_schemas():
    for name in (MAPBOX, GOOGLE):
        tools = competitor_comparison.competitor_tools(name)
        assert tools, f"{name}: empty tool list"
        for tool in tools:
            assert tool["name"]
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"


def test_scenario_tool_subsets_exist_in_every_snapshot():
    """A rename upstream must fail loudly, not silently shrink their subset."""
    surfaces = competitor_comparison.measure_all_surfaces()
    for name in competitor_comparison.SERVER_ORDER:
        surface = surfaces[name]
        assert surface.tool_count >= surface.subset_tool_count
        assert surface.subset_tokens > 0
        assert surface.tokens >= surface.subset_tokens


def test_every_captured_answer_names_a_known_scenario_and_a_source_file():
    known = {key for key, _question, _call in competitor_comparison.SCENARIOS}
    rows = competitor_comparison.competitor_answers()
    assert rows
    for row in rows:
        assert row["server"] in (MAPBOX, GOOGLE)
        assert row["scenario"] in known, f"unknown scenario {row['scenario']}"
        assert not row["is_error"], f"{row['server']}/{row['scenario']} captured an error"
        assert row["response_text"].strip()
        # No hand-typed figures: each answer points at the vendored upstream
        # example the competitor's own server was fed to produce it.
        assert (REPO_ROOT / row["upstream_example"]).is_file(), row["upstream_example"]


def test_upstream_examples_are_json_and_all_are_used():
    used = {row["upstream_example"] for row in competitor_comparison.competitor_answers()}
    on_disk = sorted((COMPETITORS / "upstream_examples").glob("*.json"))
    assert on_disk
    for path in on_disk:
        json.loads(path.read_text())
        assert str(path.relative_to(REPO_ROOT)) in used, f"{path.name} is vendored but unused"


def test_placeroot_answers_every_scenario_from_fixtures_without_network():
    answers = competitor_comparison.live_placeroot_answers()
    assert len(answers) == len(competitor_comparison.SCENARIOS)
    for answer in answers:
        assert answer.tokens > 0
        # PlaceRoot serializes compact, so there is no whitespace to strip.
        assert answer.minified_tokens == answer.tokens


def test_our_answers_are_vendored_with_the_platform_they_were_captured_on():
    """Same honesty bar we hold the competitors to: say where a number came from."""
    snapshot = json.loads(competitor_comparison.PLACEROOT_ANSWERS_PATH.read_text())
    assert snapshot["captured_on"]
    assert snapshot["python"]
    assert snapshot["overture_release"]
    assert snapshot["method"]
    captured = {row["scenario"] for row in snapshot["answers"]}
    assert captured == {key for key, _question, _call in competitor_comparison.SCENARIOS}
    for row in snapshot["answers"]:
        assert row["response_text"].strip()


def test_vendored_placeroot_answers_match_a_live_run_within_tolerance():
    """The vendored snapshot of our own answers must not rot as the code changes.

    Deliberately a tolerance, not an equality: routing and geometry do
    floating-point work whose last digits differ between platforms, so a live
    rerun lands a few characters — and therefore a token or two, at chars/4 —
    away from the snapshot. Observed between this repo's macOS and Linux CI
    runners: route_a_to_b 41 vs 42 tokens, isochrone_15min 208 vs 205. That is
    the whole reason the published page is built from the snapshot rather than
    from a live run; pinning equality here would just move the platform
    dependence into this test.

    A real change in what a tool answers moves the number far more than that
    and fails here, with the fix being:

        uv run python benchmarks/competitor_comparison.py --capture-answers --write
    """
    live = {answer.scenario: answer for answer in competitor_comparison.live_placeroot_answers()}
    vendored = {
        answer.scenario: answer for answer in competitor_comparison.vendored_placeroot_answers()
    }
    assert live.keys() == vendored.keys()
    for scenario, snapshot in vendored.items():
        observed = live[scenario].tokens
        tolerance = max(10, snapshot.tokens * 0.05)
        assert abs(observed - snapshot.tokens) <= tolerance, (
            f"{scenario}: live run answers {observed} tokens, snapshot says "
            f"{snapshot.tokens} — rerun "
            "`uv run python benchmarks/competitor_comparison.py --capture-answers --write`"
        )


def test_the_same_estimator_is_applied_to_everyone():
    """chars/4, the one `placeroot.budget.estimate_tokens` uses — never tiktoken."""
    from placeroot.budget import CHARS_PER_TOKEN

    assert competitor_comparison.count_tokens("x" * 400) == 400 // CHARS_PER_TOKEN
    assert "tiktoken" not in competitor_comparison.TOKENIZER_LABEL


def test_write_replaces_only_the_generated_region(tmp_path):
    doc = tmp_path / "benchmarks-vs.md"
    doc.write_text(
        f"before\n{competitor_comparison.GENERATED_BEGIN}\nstale\n"
        f"{competitor_comparison.GENERATED_END}\nafter\n"
    )
    competitor_comparison.write_doc("FRESH", doc_path=doc)
    assert doc.read_text() == "before\nFRESH\nafter\n"


@pytest.mark.skipif(
    os.environ.get("PLACEROOT_TOOLS", "all") != "all",
    reason="PLACEROOT_TOOLS narrows the registry, so the committed schema surface won't match",
)
def test_committed_doc_matches_a_fresh_run():
    """The drift guard, byte for byte.

    Safe to pin exactly because every answer figure comes from a committed
    snapshot and the estimator is fixed; the only live input is the schema
    surface, which is platform-independent text.
    """
    text = competitor_comparison.DOC_PATH.read_text()
    start = text.index(competitor_comparison.GENERATED_BEGIN)
    end = text.index(competitor_comparison.GENERATED_END) + len(
        competitor_comparison.GENERATED_END
    )
    assert text[start:end] == competitor_comparison.render_generated_section(), (
        "docs/benchmarks-vs.md is stale — "
        "rerun `uv run python benchmarks/competitor_comparison.py --write`"
    )


def test_doc_states_the_method_and_the_limitations():
    text = competitor_comparison.DOC_PATH.read_text()
    snapshot = json.loads(competitor_comparison.PLACEROOT_ANSWERS_PATH.read_text())
    assert snapshot["captured_on"] in text, "the page must say where our own answers were captured"
    assert "## Honest limitations" in text
    assert "keyless" in text and "live-data" in text
    assert "once per conversation" in text and "per call" in text
    assert "not measured" in text
