# Registry submissions

Ready-to-paste entries per registry. **Status:** both packages are live as
of v0.5.0 — `placeroot` on PyPI and `placeroot` on npm — so `uvx placeroot`
and `npx placeroot` work as written below. The repo-side manifests these
submissions draw from are checked in at `server.json` (official MCP
registry) and `mcpb/manifest.json` (MCPB bundle manifest); keep their
`version` fields in step with `pyproject.toml` when releasing.

Repo: https://github.com/chuofringer/placeroot · License: MIT · Homepage:
https://placeroot.dev

---

## awesome-mcp-servers (wong2/awesome-mcp-servers style list)

One-line entry, their `- [Name](url) - description` format, under a
"Geo / Maps" or "Data" category if one exists, otherwise a new one:

```markdown
- [PlaceRoot](https://github.com/chuofringer/placeroot) - Keyless MCP server answering spatial questions from Overture Maps open data (GeoParquet via DuckDB), with token-budgeted responses instead of raw GeoJSON dumps.
```

## sparkgeo/geo-mcp-servers (curated geospatial tracker, added 2026-08-06)

PR against https://github.com/sparkgeo/geo-mcp-servers — their categorized
list (77 servers as of Aug 2026); PlaceRoot belongs in "Spatial Databases &
Analytics" (currently 3 entries, incl. the dormant srivinod1 Overture
server) or a "Places / Geocoding" category if they split one out. Their
row format:

```markdown
| [PlaceRoot](https://github.com/chuofringer/placeroot) | Keyless spatial answers from Overture Maps GeoParquet (DuckDB, no ETL): places, geocoding, admin lookup, buildings, isochrones on its own routing graph — every response token-budgeted, GERS ids throughout. | Python | MIT |
```

## mcpservers.org

- **Name:** PlaceRoot
- **One-liner:** Keyless MCP server for spatial questions over Overture Maps open data — token-budgeted answers, no API key.
- **Category:** Geospatial / Maps
- **Install:** `uvx placeroot`
- **Repo:** https://github.com/chuofringer/placeroot
- **Homepage:** https://placeroot.dev

## Glama

- **Name:** placeroot
- **One-liner:** Answers spatial questions (nearby places, geocoding, admin lookup, area comparison) from Overture Maps' public GeoParquet release — no key, no vendor platform.
- **Category:** Geospatial
- **Install command:** `uvx placeroot` (stdio) or `uv run placeroot --http` (streamable-HTTP)
- **License:** MIT
- **Repo:** https://github.com/chuofringer/placeroot

## PulseMCP

- **Name:** PlaceRoot
- **Tagline:** Grounds AI agents in open map data — Overture Maps queried live, no API key, every answer sized for a context window.
- **Category:** Maps & Location
- **Install:** `uvx placeroot`
- **Source:** https://github.com/chuofringer/placeroot
- **Notable stat to lead with:** `find_places` returns 10 ranked results in 501 tokens vs. ~45,000 for the equivalent raw GeoJSON.

## Smithery

**There is no `smithery.yaml` any more.** Smithery's current docs
(https://smithery.ai/docs/build/publish, index at
https://smithery.ai/docs/llms.txt — checked 2026-08-07, zero occurrences of
"yaml") describe exactly two publishing paths, and neither reads a config
file from the repo:

1. **URL** — give Smithery a public HTTPS endpoint speaking streamable
   HTTP; their Gateway proxies to it and scans it for tools. PlaceRoot
   serves streamable HTTP (`uvx placeroot --http`) but is not hosted
   anywhere public, so this path needs a deployment decision first.
2. **Local (MCPB bundle)** — upload a `.mcpb` bundle for a stdio server:
   `smithery mcp publish ./server.mcpb -n chuofringer/placeroot`.

Path 2 is the one that fits a `uvx`-installed stdio server. The bundle
manifest is checked in at `mcpb/manifest.json` (MCPB manifest_version 0.4,
`server.type: "uv"` — deps come from `pyproject.toml`, nothing vendored),
validated against
https://github.com/modelcontextprotocol/mcpb/blob/main/schemas/mcpb-manifest-v0.4.schema.json.
The bundle itself is built (`scripts/build_mcpb.py` → `site/placeroot.mcpb`,
rebuilt on every version bump by the Prepare Release workflow), so the
submission is one `smithery mcp publish` away once the repo is public.
Caveat for the listing text: the bundle's server type is `uv`, so the
user's machine still needs uv installed — one-click removes the config
step, not the runtime prerequisite.

- **Qualified name:** `chuofringer/placeroot` (adjust once the namespace is claimed)
- **Display name:** PlaceRoot
- **Description:** Keyless MCP server that answers spatial questions from Overture Maps open data (GeoParquet via DuckDB) — token-budgeted answers, geocoding without Nominatim, GERS ids on every place.
- **Category:** geospatial / maps
- **Config:** none required — no API key, no environment variables for basic operation (`PLACEROOT_CACHE=off` is optional, to disable the local tile cache)

## Official MCP registry (registry.modelcontextprotocol.io)

The registry's submission unit is a `server.json` file, published with the
`mcp-publisher` CLI under an `io.github.<user>/<name>` namespace. That file
is checked in at the repo root: **`server.json`**. It declares three
packages — PyPI via `uvx` (stdio), npm via `npx` (stdio), and PyPI via
`uvx --http` (streamable-HTTP on 127.0.0.1:8321/mcp) — plus the optional
`PLACEROOT_CACHE` environment variable.

It validates cleanly (0 errors) against the published schema
https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json
as of 2026-08-07. Format reference:
https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/generic-server-json.md

Note that `server.json` carries no tool list — the schema has no field for
one; registries discover tools by connecting to the server. The per-tool
descriptions live in `mcpb/manifest.json` instead, which does have a
`tools` array.

Remaining owner-gated steps: `mcp-publisher login github` (proves ownership
of the `io.github.chuofringer` namespace) then `mcp-publisher publish`. The
repo is currently private, which may affect the ownership check and the
`repository.url` link in the manifest — make the repo public first, or
expect that step to need revisiting. The official registry still returns
zero results for "overture" (verified during market research): open namespace.

## awesome-agentic-AI-for-ST

Format is typically `- **[Name](repo-url)** — one-line description.` under
whatever spatiotemporal/geospatial-agent category the list uses:

```markdown
- **[PlaceRoot](https://github.com/chuofringer/placeroot)** — Keyless MCP server grounding AI agents in Overture Maps open data: token-budgeted spatial answers (nearby places, geocoding, admin hierarchy, area comparison), no API key, no ETL.
```
