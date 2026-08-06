"""Loads scripts/build_routing_fixture.py so tests can reference its grid
constants and helpers (node_id, node_latlon, GRID_N, ...) without
duplicating the generator's geometry logic and risking drift.
"""

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "build_routing_fixture.py"
_spec = importlib.util.spec_from_file_location("build_routing_fixture", _SCRIPT_PATH)
build_routing_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_routing_fixture)
