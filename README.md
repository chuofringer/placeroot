<p align="center">
  <a href="https://placeroot.dev">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="site/og-image-dark.png">
      <img src="site/og-image.png" width="720" alt="PlaceRoot — Ask about anywhere. Get a real answer. Open map data for your AI — free, no account, no API key.">
    </picture>
  </a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a>
  ·
  <a href="#what-it-can-do">Tools</a>
  ·
  <a href="docs/REFERENCE.md">Reference</a>
  ·
  <a href="docs/benchmarks.md">Benchmarks</a>
  ·
  <a href="https://placeroot.dev">Website</a>
</p>

<p align="center">
  <a href="https://github.com/chuofringer/placeroot/actions/workflows/ci.yml"><img src="https://github.com/chuofringer/placeroot/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/placeroot/"><img src="https://img.shields.io/pypi/v/placeroot" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/placeroot"><img src="https://img.shields.io/npm/v/placeroot" alt="npm"></a>
  <a href="https://pypi.org/project/placeroot/"><img src="https://img.shields.io/pypi/pyversions/placeroot" alt="Python versions"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
</p>

**PlaceRoot grounds AI agents in open map data.** It's an MCP server that answers spatial questions — what's nearby, what's in this neighborhood, how do I get there — from [Overture Maps](https://overturemaps.org) open data. No API key, no signup, no vendor platform.

- 🎯 **Answers, not data dumps.** Every tool returns compact, ranked results sized for an agent's context window — never a raw GeoJSON dump.
- 🗺️ **Real routing, zero keys.** `route` and `isochrone` walk an actual street graph built from Overture's transportation segments — not a straight-line guess — anywhere on Earth.
- 🏙️ **Rich, filterable place data.** Category, brand, confidence, operating status, contactability — sourced from Overture's open dataset (contributed by Meta, Uber, TomTom, and others).
- 📐 **Boundary-accurate.** Search inside a named place's real administrative polygon, not a guessed radius circle.
- ⚡ **Zero setup.** Reads Overture's public data directly — no key, no database, nothing to install beyond the server itself.

## Quick start

Run it straight from PyPI or npm — no install step:

```bash
uvx placeroot             # stdio MCP server
uvx placeroot --http      # HTTP endpoint at http://127.0.0.1:8321/mcp
```

<details>
<summary><b>Add to Claude Code</b></summary>

```bash
claude mcp add placeroot -- uvx placeroot
```

</details>

<details>
<summary><b>Add to Claude Desktop</b></summary>

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "placeroot": {
      "command": "uvx",
      "args": ["placeroot"]
    }
  }
}
```

</details>

<details>
<summary><b>Add to Cursor / other MCP clients</b></summary>

Same config as above wherever your client keeps its MCP server list. Prefer npm? Use `"command": "npx", "args": ["placeroot"]`.

```json
{
  "mcpServers": {
    "placeroot": {
      "command": "npx",
      "args": ["placeroot"]
    }
  }
}
```

</details>

Then ask your agent something spatial:

> *"What's around downtown Palo Alto?" · "Compare these two neighborhoods for a bike shop." · "Plan my errands: pharmacy, hardware store, post office."*

## What it can do

**29 tools**, grouped into four families — every answer fits in a couple of thousand tokens:

| Family | Tools | Answers questions like |
|---|---|---|
| 🔍 **Search & identify** | `find_places`, `geocode`, `reverse_geocode`, `place_details`, `search_categories`, … | What cafés are near this point? What's at this address? |
| 📊 **Area analysis** | `summarize_area`, `compare_areas`, `summarize_buildings`, `land_use_at`, … | What's in this neighborhood, and how does it differ from that one? |
| 🚴 **Routing** | `route`, `isochrone`, `optimize_route`, `places_along_route`, `distance_matrix` | How far by bike? What's reachable in 15 minutes? Best order for 6 stops? |
| 🗺️ **Geometry & maps** | `render_map`, `simplify_geometry` | Show me this result as an interactive map |

It also ships three **workflow prompts** (site selection, neighborhood comparison, errand planning) and two attachable **resources** — and a `PLACEROOT_TOOLS` setting to load only the tool profiles you need, cutting schema overhead by up to 96%.

📚 **[Full tool catalog, prompts, resources & configuration → docs/REFERENCE.md](docs/REFERENCE.md)**

> [!NOTE]
> Open data has honest limits: no live traffic, no opening hours, no ratings or photos — [what PlaceRoot deliberately doesn't do](docs/REFERENCE.md#what-it-deliberately-does-not-do). For everything else about where things are and what's reachable from them, it answers without a key.

## Why PlaceRoot

It's the only keyless MCP server doing real graph routing over global open map data — with every tool declaring proper [MCP annotations](docs/REFERENCE.md#tool-annotations-and-listing-cache-hints) so clients know which calls are read-only before prompting you. Stable [GERS ids](https://docs.overturemaps.org/gers/) let agents hold onto places across turns; local caching makes repeat queries answer in milliseconds and keeps working offline; and the whole thing is [self-hostable end to end](docs/MIRROR.md).

How it stacks up against Mapbox MCP and Google Maps MCP: [head-to-head benchmarks](docs/benchmarks-vs.md) · [token-efficiency numbers](docs/benchmarks.md).

## Recreation places

Overture's places theme is derived from business listings, which makes it strong on businesses and thin on the places a family goes on a Saturday — playgrounds, neighbourhood parks, dog parks, beaches. Those features aren't missing from Overture, though; they're in a different theme. Overture's `base` theme is a direct conflation of OpenStreetMap, and PlaceRoot already queries it for `land_use_at` and `infrastructure_near`. The places tools read it too, by default.

Nothing is downloaded, built, or hosted — it's one more live scan of the same public Overture release, and it roughly **2.5x**es playground coverage (1,552 vs 674 across New York City in release `2026-07-22.0`, with 1,013 of them more than 150 m from any places-theme playground). Every places tool answers from both at once, with no other change: same tools, same response shape, same category filters.

The cost is a second dataset scan per places query (cached like everything else), and these rows carry no `confidence` or `operating_status` and are often unnamed — an unnamed playground comes back with `name: null` rather than being dropped. If you'd rather have the latency than the coverage:

```bash
export PLACEROOT_RECREATION_LAYER=0
```

`data_version` reports the layer whenever it's active. Full details, including why live Overpass queries and raw OSM Parquet were measured and rejected: [docs/RECREATION.md](docs/RECREATION.md).

## Development

```bash
uv sync           # install dev dependencies
uv run pytest     # offline test suite
uv run ruff check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, design rules, and how to propose a tool. Other docs: [CHANGELOG](CHANGELOG.md) · [SECURITY](SECURITY.md) · [publishing](docs/PUBLISHING.md) · [website](docs/WEBSITE.md) · [running a data mirror](docs/MIRROR.md) · [the recreation layer](docs/RECREATION.md)

## Contact

`hello@placeroot.dev`

## License and attribution

The code is [MIT](LICENSE). The data it queries is the Overture Maps public release, licensed per theme — places under CDLA-Permissive-2.0; the OSM-derived themes (divisions, transportation, base) under ODbL, which asks for attribution on anything user-facing you build from them:

> © Overture Maps Foundation · © OpenStreetMap contributors ([ODbL](https://opendatacommons.org/licenses/odbl/))

Per-theme obligations: [docs/DATA-LICENSE.md](docs/DATA-LICENSE.md).

<p align="center">
  <img src="https://placeroot.dev/logo-mark.svg" width="48" alt="PlaceRoot logo mark">
</p>
