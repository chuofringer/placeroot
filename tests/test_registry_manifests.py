"""Guard: the registry distribution manifests stay in sync with the package.

Two manifests are checked into the repo for registry distribution (#172):

- `server.json` — the official MCP registry submission unit, published with
  the `mcp-publisher` CLI.
- `mcpb/manifest.json` — the MCPB bundle manifest, the artifact Smithery's
  current publishing flow takes for a local stdio server.

Both restate things that live elsewhere (the package version, the package
names on PyPI/npm, the tool list), so both can go stale silently at release
time — the same staleness class `test_site_version_sync.py` and
`test_site_tools_sync.py` guard for the marketing site. These checks are
offline; they do not validate against the remote JSON schemas.
"""

import asyncio
import json
import re
import tomllib
from pathlib import Path

from placeroot import server

ROOT = Path(__file__).parent.parent
SERVER_JSON = ROOT / "server.json"
MCPB_MANIFEST = ROOT / "mcpb" / "manifest.json"


def _package_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _registered_tool_names() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_server_json_version_matches_pyproject():
    doc = _load(SERVER_JSON)
    expected = _package_version()
    assert doc["version"] == expected, (
        f"server.json version is {doc['version']} but pyproject.toml says "
        f"{expected} — bump the manifest with the release."
    )
    for package in doc["packages"]:
        assert package.get("version") == expected, (
            f"server.json packages[] entry {package['identifier']!r} "
            f"({package['registryType']}) pins version {package.get('version')!r}, "
            f"expected {expected}."
        )


def test_server_json_packages_point_at_the_published_package_names():
    doc = _load(SERVER_JSON)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pypi_name = pyproject["project"]["name"]
    npm_name = _load(ROOT / "npm" / "package.json")["name"]
    expected = {"pypi": pypi_name, "npm": npm_name}

    for package in doc["packages"]:
        registry = package["registryType"]
        assert registry in expected, (
            f"server.json declares an unexpected registryType {registry!r}; "
            f"this guard only knows about {sorted(expected)}."
        )
        assert package["identifier"] == expected[registry], (
            f"server.json {registry} package is {package['identifier']!r} but the "
            f"repo publishes {expected[registry]!r}."
        )


def test_mcpb_manifest_version_matches_pyproject():
    doc = _load(MCPB_MANIFEST)
    expected = _package_version()
    assert doc["version"] == expected, (
        f"mcpb/manifest.json version is {doc['version']} but pyproject.toml "
        f"says {expected} — bump the manifest with the release."
    )


def test_mcpb_manifest_lists_exactly_the_registered_tools():
    listed = {tool["name"] for tool in _load(MCPB_MANIFEST)["tools"]}
    registered = _registered_tool_names()

    assert listed == registered, (
        "mcpb/manifest.json's tools[] has drifted from the tools registered in "
        f"server.py. Only in the manifest: {sorted(listed - registered)}; "
        f"only in server.py: {sorted(registered - listed)}."
    )


def test_mcpb_manifest_entry_point_exists():
    doc = _load(MCPB_MANIFEST)
    entry_point = ROOT / doc["server"]["entry_point"]
    assert entry_point.is_file(), (
        f"mcpb/manifest.json entry_point {doc['server']['entry_point']!r} does "
        f"not exist at {entry_point}."
    )


def test_server_json_description_is_count_free():
    doc = _load(SERVER_JSON)
    description = doc["description"]
    assert not re.search(r"\d+\s*tools", description), (
        f"server.json description {description!r} names a tool count — "
        "it isn't guarded and will go stale as tools are added (#366). "
        "Keep the description count-free instead."
    )


def test_every_manifest_tool_carries_a_description():
    for tool in _load(MCPB_MANIFEST)["tools"]:
        description = tool.get("description", "")
        assert description.strip(), (
            f"mcpb/manifest.json tool {tool['name']!r} has no description; "
            "registries show these verbatim."
        )
