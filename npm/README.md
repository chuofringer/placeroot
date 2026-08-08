# PlaceRoot

**Ground AI agents in open map data.**

PlaceRoot is an MCP server that answers spatial questions from [Overture Maps](https://overturemaps.org) — no API key, no signup, no vendor platform.

This npm package is the Node launcher. The server itself is Python, distributed on PyPI; `npx placeroot` spawns `uvx placeroot` under the hood and passes through arguments, stdio, and the exit code, so it behaves exactly like running `uvx placeroot` directly.

## Quick start

Add to Claude Desktop / Claude Code:

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

Or run it directly:

```bash
npx placeroot             # stdio MCP server
npx placeroot --http      # HTTP endpoint at http://127.0.0.1:8321/mcp
```

## Requirements

- **Node 18+** for the launcher itself.
- **[uv](https://docs.astral.sh/uv/)** on your `PATH` — the launcher shells out to `uvx`. If it isn't installed, the command exits with instructions rather than a stack trace. `pip install uv` works too.

If you already use Python tooling, `uvx placeroot` skips this launcher entirely and is equivalent.

## What it can do

**25 tools**, all returning compact, ranked answers sized for an agent's context window — never a raw GeoJSON dump:

- **Find places** near a point or inside a named area's real administrative polygon — filter by category, brand, confidence, operating status, or has-website / has-phone.
- **Geocode and reverse-geocode**, with stable [GERS ids](https://docs.overturemaps.org/gers/) an agent can hold onto across turns.
- **Route and reach** — `route` and `isochrone` walk an actual street graph built from Overture's transportation segments, not a straight-line approximation, anywhere on Earth.
- **Summarize and compare areas** — category mix, density, building stock, land use, infrastructure.
- **Render** any result as a self-contained interactive HTML map.

What it deliberately does not do, because Overture's open data does not carry it: no live traffic, no opening hours, no ratings or photos. For those, a commercial maps API is the right tool.

## Loading fewer tools

All 25 tool schemas cost roughly 9.2k tokens of every conversation's context. `PLACEROOT_TOOLS` selects a slice — `core`, `search`, `routing`, `analysis`, `geometry`, or an explicit list of tool names:

```json
{
  "mcpServers": {
    "placeroot": {
      "command": "npx",
      "args": ["placeroot"],
      "env": { "PLACEROOT_TOOLS": "core" }
    }
  }
}
```

The narrowest profile cuts schema cost by 92%. See the full documentation for the profile tables.

## Documentation

Full docs, the complete tool reference, and setup guides: **[placeroot.dev](https://placeroot.dev)**

## License

MIT
