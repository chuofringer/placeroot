"""Offline smoke test for examples/site_selection/run_demo.py (issue #17).

Runs the demo script's --offline mode as a real subprocess (it's a standalone
CLI, not a library) against the committed fixtures and checks the printed
report has the sections a launch-post walkthrough depends on. Kept in the
default (non-live) suite so it stays green in CI with no network.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_SCRIPT = REPO_ROOT / "examples" / "site_selection" / "run_demo.py"


def _run_offline_demo() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--offline"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_offline_demo_runs_end_to_end():
    result = _run_offline_demo()
    assert result.returncode == 0, result.stderr


def test_offline_demo_report_has_expected_sections():
    result = _run_offline_demo()
    report = result.stdout
    assert "Connected to MCP server: placeroot" in report
    assert "## Tool calls" in report
    assert "## Token accounting" in report
    assert "TOTAL across" in report
    assert "## Recommendation" in report
    assert "Recommended:" in report


def test_offline_demo_calls_every_expected_tool():
    result = _run_offline_demo()
    report = result.stdout
    for tool in (
        "summarize_area",
        "compare_areas",
        "find_places",
        "within_distance",
        "place_details",
        "admin_lookup",
    ):
        assert f"{tool} —" in report or f"{tool}(" in report, f"{tool} missing from report"
