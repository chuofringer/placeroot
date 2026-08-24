"""Smoke test for benchmarks/token_efficiency.py (issue #178).

Deliberately does not pin any token count: the whole point of that script
is that its numbers move when tools or responses change, so asserting them
here would turn every legitimate edit into a test failure. What's asserted
is that the script still runs offline against the committed fixtures, still
reads the tool list from the live registry, and still emits the sections
docs/benchmarks.md is built from.

benchmarks/ isn't part of the installed package, so the script is loaded by
file path — same approach as tests/test_token_benchmark.py.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "token_efficiency", REPO_ROOT / "benchmarks" / "token_efficiency.py"
)
token_efficiency = importlib.util.module_from_spec(_SPEC)
# dataclass field-type resolution looks the module up in sys.modules by
# __module__ name, so it must be registered before exec_module runs.
sys.modules["token_efficiency"] = token_efficiency
_SPEC.loader.exec_module(token_efficiency)


def test_schema_surface_covers_every_registered_tool():
    rows = token_efficiency.measure_schema_surface()
    names = {name for name, _, _, _, _ in rows}
    # Introspected from the registry, so this only checks that well-known
    # tools are present — not the exact tool count, which changes often.
    assert {"find_places", "geocode", "isochrone", "route"} <= names
    for _name, whole, desc, schema, output_schema in rows:
        assert whole.tokens > 0
        # roadmap §4.3: `whole` now also includes outputSchema.
        assert whole.chars >= desc.chars + schema.chars + output_schema.chars - 3
        assert whole.bytes_ >= whole.chars


def test_every_scenario_answers_from_fixtures_without_network():
    results = token_efficiency.run_scenarios()
    assert len(results) == len(token_efficiency.SCENARIOS)
    for result in results:
        assert "error" not in result.payload, f"{result.scenario.name}: {result.payload}"
        assert result.measurement.tokens > 0


def test_report_has_the_sections_the_doc_is_built_from():
    rows = token_efficiency.measure_schema_surface()
    results = token_efficiency.run_scenarios()
    section = token_efficiency.render_generated_section(rows, results)

    assert section.startswith(token_efficiency.GENERATED_BEGIN)
    assert section.rstrip().endswith(token_efficiency.GENERATED_END)
    assert "Token counting method:" in section
    assert "Total schema surface:" in section
    assert "### Schema surface" in section
    assert "### Response cost" in section
    for scenario in token_efficiency.SCENARIOS:
        assert scenario.name in section


def test_write_replaces_only_the_generated_region(tmp_path):
    doc = tmp_path / "benchmarks.md"
    doc.write_text(
        f"before\n{token_efficiency.GENERATED_BEGIN}\nstale numbers\n"
        f"{token_efficiency.GENERATED_END}\nafter\n"
    )
    token_efficiency.write_doc("FRESH", doc_path=doc)
    assert doc.read_text() == "before\nFRESH\nafter\n"


def test_committed_doc_carries_the_generated_markers():
    """docs/benchmarks.md must stay regenerable — losing the markers would
    silently turn its numbers into hand-maintained prose."""
    text = (REPO_ROOT / "docs" / "benchmarks.md").read_text()
    assert token_efficiency.GENERATED_BEGIN in text
    assert token_efficiency.GENERATED_END in text
    assert text.index(token_efficiency.GENERATED_BEGIN) < text.index(
        token_efficiency.GENERATED_END
    )
