# PlaceRoot

**Ground AI agents in open map data.**

PlaceRoot is an MCP server that answers spatial questions from [Overture Maps](https://overturemaps.org) — no API key, no signup, no vendor platform.

- **Answers, not data dumps.** Every tool returns compact, ranked results sized for an agent's context window — never a raw GeoJSON dump.
- **Rich, filterable place data.** Category, brand, confidence, operating status, contactability — all queryable, sourced from Overture's open dataset (contributed by Meta, Uber, TomTom, and others).
- **Boundary-accurate.** Search inside a named place's real administrative polygon, not just a guessed radius circle.
- **Zero setup.** Reads Overture's public data directly — nothing to install beyond the server itself, no key, no database.

## Quick start

Add to Claude Desktop / Claude Code:

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

(`npx placeroot` also works, if you'd rather use the npm launcher: set `"command": "npx"`.)

Or run it directly:

```bash
uvx placeroot             # stdio MCP server
uvx placeroot --http      # HTTP endpoint at http://127.0.0.1:8321/mcp
```

## What it can do

**22 tools**, all returning compact, budgeted answers. Several single-item tools have a `*_batch` sibling that collapses many calls into one round-trip.

| Tool | Answers |
|---|---|
| `find_places` | Named places near a point **or inside a named area / division polygon**, nearest first — filter by category, brand, confidence, operating status, or has-website / has-phone |
| `summarize_area` | What's in an area: total places and top categories |
| `compare_areas` | 2–5 areas side by side: category mix, density, and what differs most |
| `within_distance` | Is the nearest matching place within N meters of a point? |
| `distance_matrix` | Straight-line distances between many origins and destinations at once |
| `place_details` | One place in full: addresses, contacts, brand, sources, confidence |
| `admin_lookup` | The admin hierarchy containing a point: neighborhood up to country |
| `summarize_buildings` | Building stock in an area: count, footprint area, height and use mix |
| `buildings_at` | Nearest building footprints to a point |
| `land_use_at` | What kind of land is this: land use and land cover classification at a point |
| `geocode` | Free-text place name → ranked candidates with coordinates and admin context (`geocode_batch` for many at once) |
| `resolve_place` | Free-text place reference → stable ids an agent can hold onto across turns (`resolve_place_batch`) |
| `reverse_geocode` | Point → nearest address plus its containing admin areas (`reverse_geocode_batch`) |
| `search_categories` | Free text → the right Overture category slug to filter `find_places` by |
| `isochrone` | The area reachable within N minutes on foot, bike, or car |
| `route` | Shortest-path distance and duration between two points, on foot, bike, or car |
| `render_map` | Any result → a self-contained interactive HTML map |
| `simplify_geometry` | Any geometry → simplified to fit a token budget |
| `data_version` | Which Overture release the answers are drawn from |

## Loading fewer tools (`PLACEROOT_TOOLS`)

All 22 tool schemas cost roughly **7.6k tokens** of every conversation's context, paid before the agent asks anything — about the cost of 56 median answers. Most installs use a slice of that surface, so `PLACEROOT_TOOLS` selects which tools get registered. Unselected tools are never registered and never appear in `tools/list`.

```json
{
  "mcpServers": {
    "placeroot": {
      "command": "uvx",
      "args": ["placeroot"],
      "env": { "PLACEROOT_TOOLS": "core" }
    }
  }
}
```

The value is a comma-separated list of profile names, tool names, or both — the union of everything named:

| `PLACEROOT_TOOLS` | Tools | Schema tokens | Saved |
|---|---:|---:|---:|
| unset / `all` (default) | 22 | ~7,650 | — |
| `search` | 10 | ~3,880 | 49% |
| `core` | 8 | ~3,530 | 54% |
| `routing` | 5 | ~1,870 | 76% |
| `analysis` | 7 | ~1,620 | 79% |
| `geometry` | 3 | ~730 | 90% |

- **`core`** — `find_places`, `geocode`, `reverse_geocode`, `place_details`, `resolve_place`, `summarize_area`, `route`. The single-purpose tools that answer most spatial questions; no batch siblings, no buildings/land-use, no rendering.
- **`search`** — the find/name/identify family: `find_places`, `place_details`, `geocode`, `resolve_place`, `reverse_geocode`, their `*_batch` siblings, and `search_categories`.
- **`routing`** — `route`, `isochrone`, `distance_matrix`, `within_distance`.
- **`analysis`** — `summarize_area`, `summarize_buildings`, `compare_areas`, `buildings_at`, `land_use_at`, `admin_lookup`.
- **`geometry`** — `simplify_geometry`, `render_map`.

`data_version` is registered under every profile: it is ~120 tokens and the only way an agent can tell which Overture release backs its answers.

Profiles may overlap, and a list may mix them with bare tool names — `PLACEROOT_TOOLS=routing,find_places` or `PLACEROOT_TOOLS=find_places,geocode,route`. A name that is neither a profile nor a tool **fails at startup** with the list of valid names, rather than quietly falling back to loading everything.

## Why

Agents are bad at maps. Existing map tools either require vendor API keys or return payloads far too large for a context window. PlaceRoot's rule: every answer fits in a couple of thousand tokens, and anything bigger comes back as a summary.

A few things that set it apart:

- **Stable place ids.** Every place carries its Overture [GERS id](https://docs.overturemaps.org/gers/), so an agent can hold onto a place across turns and look it up again later instead of re-searching.
- **Built-in geocoding.** Place-name lookup works out of the box — no third-party geocoding service involved.
- **Fast repeat queries.** Frequently used data is cached locally, so repeat questions answer in milliseconds and keep working offline.
- **Self-hostable end to end.** Run it locally, serve it over HTTP, or point it at your own copy of the data — no dependency on anyone else's service.

## Development

```bash
uv sync           # install dev dependencies
uv run pytest     # offline test suite
uv run ruff check .
```

## Docs

- [ROADMAP.md](ROADMAP.md) — where this is going · [PLAN.md](PLAN.md) — product plan and positioning
- [docs/PUBLISHING.md](docs/PUBLISHING.md) — how releases reach PyPI + npm
- [docs/WEBSITE.md](docs/WEBSITE.md) — the marketing site: serve locally and deploy
- [docs/MIRROR.md](docs/MIRROR.md) · [docs/METRICS.md](docs/METRICS.md) · [docs/launch/](docs/launch/)

## License

MIT

