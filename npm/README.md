<!-- Generated from the root README.md by scripts/sync_npm_readme.py.
     Edit that file (or the script's npm-only sections), then re-run:
         uv run python scripts/sync_npm_readme.py -->

<p align="center">
  <a href="https://placeroot.dev">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/chuofringer/placeroot/v0.9.6/site/og-image-dark.png">
      <img src="https://raw.githubusercontent.com/chuofringer/placeroot/v0.9.6/site/og-image.png" width="100%" alt="PlaceRoot — Ask about anywhere. Get a real answer. Open map data for your AI — free, no account, no API key.">
    </picture>
  </a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a>
  ·
  <a href="#what-it-can-do">Tools</a>
  ·
  <a href="https://github.com/chuofringer/placeroot/blob/v0.9.6/docs/REFERENCE.md">Reference</a>
  ·
  <a href="https://github.com/chuofringer/placeroot/blob/v0.9.6/docs/benchmarks.md">Benchmarks</a>
  ·
  <a href="https://placeroot.dev">Website</a>
</p>

<p align="center">
  <a href="https://github.com/chuofringer/placeroot/actions/workflows/ci.yml"><img src="https://github.com/chuofringer/placeroot/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/placeroot/"><img src="https://img.shields.io/pypi/v/placeroot" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/placeroot"><img src="https://img.shields.io/npm/v/placeroot" alt="npm"></a>
  <a href="https://pypi.org/project/placeroot/"><img src="https://img.shields.io/pypi/pyversions/placeroot" alt="Python versions"></a>
  <a href="https://github.com/chuofringer/placeroot/blob/v0.9.6/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
</p>

**PlaceRoot grounds AI agents in open map data.** It's an MCP server that answers spatial questions — what's nearby, what's in this neighborhood, how do I get there — from [Overture Maps](https://overturemaps.org) open data. No API key, no signup, no vendor platform.

- 🎯 **Answers, not data dumps.** Every tool returns compact, ranked results sized for an agent's context window — never a raw GeoJSON dump.
- 🗺️ **Real routing, zero keys.** `route` and `isochrone` walk an actual street graph built from Overture's transportation segments — not a straight-line guess — anywhere on Earth.
- 🏙️ **Rich, filterable place data.** Category, brand, confidence, operating status, contactability — sourced from Overture's open dataset (contributed by Meta, Uber, TomTom, and others).
- 📐 **Boundary-accurate.** Search inside a named place's real administrative polygon, not a guessed radius circle.
- ⚡ **Zero setup.** Reads Overture's public data directly — no key, no database, nothing to install beyond the server itself.

This npm package is the Node launcher. The server itself is Python, distributed on PyPI; `npx placeroot` spawns `uvx placeroot` under the hood and passes through arguments, stdio, and the exit code, so it behaves exactly like running `uvx placeroot` directly.

## Quick start

Run it straight from PyPI or npm — no install step:

```bash
npx placeroot             # stdio MCP server
npx placeroot --http      # HTTP endpoint at http://127.0.0.1:8321/mcp
```

<details>
<summary><b>Add to Claude Code</b></summary>

```bash
claude mcp add placeroot -- npx placeroot
```

</details>

<details>
<summary><b>Add to Claude Desktop</b></summary>

Add to your `claude_desktop_config.json`:

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

<details>
<summary><b>Add to Cursor / other MCP clients</b></summary>

Same config as above wherever your client keeps its MCP server list. Prefer running the Python server directly? Use `"command": "uvx", "args": ["placeroot"]`. This package is a thin wrapper around exactly that.

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

## Requirements

- **Node 18+** for the launcher itself.
- **[uv](https://docs.astral.sh/uv/)** on your `PATH` — the launcher shells out to `uvx`. If it isn't installed, the command exits with install instructions rather than a stack trace. `pip install uv` works too.

If you already use Python tooling, `uvx placeroot` skips this launcher entirely and is equivalent.

## What it can do

**29 tools**, grouped into four families — every answer fits in a couple of thousand tokens:

| Family | Tools | Answers questions like |
|---|---|---|
| 🔍 **Search & identify** | `find_places`, `geocode`, `reverse_geocode`, `place_details`, `search_categories`, … | What cafés are near this point? What's at this address? |
| 📊 **Area analysis** | `summarize_area`, `compare_areas`, `summarize_buildings`, `land_use_at`, … | What's in this neighborhood, and how does it differ from that one? |
| 🚴 **Routing** | `route`, `isochrone`, `optimize_route`, `places_along_route`, `distance_matrix` | How far by bike? What's reachable in 15 minutes? Best order for 6 stops? |
| 🗺️ **Geometry & maps** | `render_map`, `simplify_geometry` | Show me this result as an interactive map |

It also ships three **workflow prompts** (site selection, neighborhood comparison, errand planning) and two attachable **resources** — and a `PLACEROOT_TOOLS` setting to load only the tool profiles you need, cutting schema overhead by up to 96%.

📚 **[Full tool catalog, prompts, resources & configuration → docs/REFERENCE.md](https://github.com/chuofringer/placeroot/blob/v0.9.6/docs/REFERENCE.md)**

> **Note:**
> Open data has honest limits: no live traffic, no opening hours, no ratings or photos — [what PlaceRoot deliberately doesn't do](https://github.com/chuofringer/placeroot/blob/v0.9.6/docs/REFERENCE.md#what-it-deliberately-does-not-do). For everything else about where things are and what's reachable from them, it answers without a key.

## Why PlaceRoot

It's the only keyless MCP server doing real graph routing over global open map data — with every tool declaring proper [MCP annotations](https://github.com/chuofringer/placeroot/blob/v0.9.6/docs/REFERENCE.md#tool-annotations-and-listing-cache-hints) so clients know which calls are read-only before prompting you. Stable [GERS ids](https://docs.overturemaps.org/gers/) let agents hold onto places across turns; local caching makes repeat queries answer in milliseconds and keeps working offline; and the whole thing is [self-hostable end to end](https://github.com/chuofringer/placeroot/blob/v0.9.6/docs/MIRROR.md).

How it stacks up against Mapbox MCP and Google Maps MCP: [head-to-head benchmarks](https://github.com/chuofringer/placeroot/blob/v0.9.6/docs/benchmarks-vs.md) · [token-efficiency numbers](https://github.com/chuofringer/placeroot/blob/v0.9.6/docs/benchmarks.md).

## Documentation

Full docs, the complete tool reference, and setup guides: **[placeroot.dev](https://placeroot.dev)**

## License

MIT
