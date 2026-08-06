# Registry submissions

Ready-to-paste entries per registry. **Blocker, read first:** PyPI and npm
publishing (ROADMAP #16 / PLAN.md Phase 1) has not shipped yet as of this
draft — `uvx placeroot` / `npx placeroot` are the intended install commands
and match `pyproject.toml` (`name = "placeroot"`) and `npm/package.json`
(`name: "placeroot"`, currently a `0.0.1` placeholder pointing at the PyPI
release), but neither package is live on its registry today. Every entry
below is written as it should read once #16 ships — do not submit any of
these until the packages are published and the install commands actually
work, or the submission will be rejected/flagged by registry maintainers who
test before merging.

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

- **Qualified name:** `chuofringer/placeroot` (adjust to Smithery's actual GitHub-based naming once claimed)
- **Display name:** PlaceRoot
- **Description:** Keyless MCP server that answers spatial questions from Overture Maps open data (GeoParquet via DuckDB) — token-budgeted answers, geocoding without Nominatim, GERS ids on every place.
- **Category:** geospatial / maps
- **Install/run command:** `uvx placeroot`
- **Config:** none required — no API key, no environment variables for basic operation (`PLACEROOT_CACHE=off` is optional, to disable the local tile cache)

## Official MCP registry (registry.modelcontextprotocol.io)

The registry's submission unit is a `server.json` file, published via the
`mcp-publisher` CLI (schema: `pkg/model/` in
`modelcontextprotocol/registry`; namespace convention
`io.github.<user>/<name>` or `<domain>/<name>`). Draft below, following the
registry's documented generic-server-json example shape — **this needs a
final check against the live schema and a real `mcp-publisher` dry run
before submitting**, since the schema can change and this draft was written
from documentation, not a validated publish:

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.chuofringer/placeroot",
  "description": "Keyless MCP server answering spatial questions from Overture Maps open data (GeoParquet via DuckDB) — token-budgeted answers, no API key, no vendor platform.",
  "title": "PlaceRoot",
  "websiteUrl": "https://placeroot.dev",
  "repository": {
    "url": "https://github.com/chuofringer/placeroot",
    "source": "github"
  },
  "version": "0.1.0",
  "packages": [
    {
      "registryType": "pypi",
      "registryBaseUrl": "https://pypi.org",
      "identifier": "placeroot",
      "version": "0.1.0",
      "runtimeHint": "uvx",
      "transport": {
        "type": "stdio"
      }
    }
  ]
}
```

TODO before submitting: confirm the `packages[].registryType`/`transport`
enum values against the current schema (`pkg/model/` in the registry repo)
and bump `version` to whatever actually lands on PyPI — this draft's
`0.1.0` matches `pyproject.toml` at time of writing but the PyPI publish
itself (#16) hasn't happened yet. The official registry currently returns
zero results for "overture" (noted in PLAN.md) — this is an open namespace.

## awesome-agentic-AI-for-ST

Format is typically `- **[Name](repo-url)** — one-line description.` under
whatever spatiotemporal/geospatial-agent category the list uses:

```markdown
- **[PlaceRoot](https://github.com/chuofringer/placeroot)** — Keyless MCP server grounding AI agents in Overture Maps open data: token-budgeted spatial answers (nearby places, geocoding, admin hierarchy, area comparison), no API key, no ETL.
```
