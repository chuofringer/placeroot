<!-- Generated from the root README.md by scripts/sync_npm_readme.py.
     Edit that file (or the script's npm-only sections), then re-run:
         uv run python scripts/sync_npm_readme.py -->

# PlaceRoot

**Ground AI agents in open map data.**

PlaceRoot is an MCP server that answers spatial questions from [Overture Maps](https://overturemaps.org) — no API key, no signup, no vendor platform.

- **Answers, not data dumps.** Every tool returns compact, ranked results sized for an agent's context window — never a raw GeoJSON dump.
- **Rich, filterable place data.** Category, brand, confidence, operating status, contactability — all queryable, sourced from Overture's open dataset (contributed by Meta, Uber, TomTom, and others).
- **Boundary-accurate.** Search inside a named place's real administrative polygon, not just a guessed radius circle.
- **Zero setup.** Reads Overture's public data directly — nothing to install beyond the server itself, no key, no database.

This npm package is the Node launcher. The server itself is Python, distributed on PyPI; `npx placeroot` spawns `uvx placeroot` under the hood and passes through arguments, stdio, and the exit code, so it behaves exactly like running `uvx placeroot` directly.

## Why PlaceRoot

It is the only keyless MCP server that does real graph routing over global open map data. `isochrone` and `route` walk an actual street graph built from Overture's transportation segments — not a straight-line approximation — anywhere on Earth, with no key, no signup, and no per-call quota. All 29 tools work that way.

Every one of those 29 tools declares MCP annotations — closed-world, plus a human-readable title, and `readOnlyHint: true` on the 28 that are pure lookups — so a client can tell before it prompts you which calls touch nothing. The one exception is honest about itself: `render_map` writes an HTML file, so it declares `readOnlyHint: false`. Keyless *and* annotated is a combination the field mostly hasn't shipped. Honest caveat: Claude Code does not gate its permission prompts on `readOnlyHint` (it uses its own classifier), so the practical win is with clients that do, such as Codex CLI and Copilot-class agents — plus spec hygiene everywhere else.

What it deliberately does not do, because Overture's open data does not carry it:

- **No live traffic.** Routing is free-flow; durations do not reflect current conditions.
- **No opening hours.** Places carry categories, brands and contacts, not schedules.
- **No ratings or photos.** There is no review corpus and no imagery behind these answers.

If a question needs one of those three, a commercial maps API is the right tool. For where things are, what is around them, and what is reachable from them, PlaceRoot answers without a key.

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

(`uvx placeroot` also works, if you'd rather run the Python server directly: set `"command": "uvx"`. This package is a thin wrapper around exactly that.)

Or run it directly:

```bash
npx placeroot             # stdio MCP server
npx placeroot --http      # HTTP endpoint at http://127.0.0.1:8321/mcp
```

## Requirements

- **Node 18+** for the launcher itself.
- **[uv](https://docs.astral.sh/uv/)** on your `PATH` — the launcher shells out to `uvx`. If it isn't installed, the command exits with install instructions rather than a stack trace. `pip install uv` works too.

If you already use Python tooling, `uvx placeroot` skips this launcher entirely and is equivalent.

## What it can do

**29 tools**, all returning compact, budgeted answers. Several single-item tools have a `*_batch` sibling that collapses many calls into one round-trip.

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
| `infrastructure_at` | Infrastructure near a point, nearest first — filter by `subtype`/`infra_class` (e.g. `bridge`, `tower`) to see past the street furniture |
| `water_near` | Water near a point, nearest first — is this waterfront, how far to the nearest river/canal/lake; filter by `subtype`/`water_class` |
| `geocode` | Free-text place name → ranked candidates with coordinates and admin context (`geocode_batch` for many at once) |
| `resolve_place` | Free-text place reference → stable ids an agent can hold onto across turns (`resolve_place_batch`) |
| `reverse_geocode` | Point → nearest address plus its containing admin areas (`reverse_geocode_batch`) |
| `address_at` | Point → the nearest street addresses (number, street, unit, postcode), with an explicit note when the country is outside Overture's 39-country address coverage |
| `geocode_address` | Street address → coordinates: "1600 Amphitheatre Parkway, Mountain View" — city-bounded, deduplicated to distinct number+street, nearest first |
| `gers_lookup` | Any GERS id → the entity it names (place, division, or building), what it's inside, and the building at its point |
| `search_categories` | Free text → the right Overture category slug to filter `find_places` by |
| `isochrone` | The area reachable within N minutes on foot, bike, or car |
| `route` | Shortest-path distance and duration between two points, on foot, bike, or car; `include_path=true` adds the simplified route polyline |
| `places_along_route` | Places on the way from A to B: corridor search along the route, with each result's detour and how far along it sits |
| `optimize_route` | Best order to visit 2–10 stops, solved exactly over the street graph — order, per-leg distance/duration, and totals |
| `render_map` | Any result → a self-contained interactive HTML map |
| `simplify_geometry` | Any geometry → simplified to fit a token budget |
| `data_version` | Which Overture release the answers are drawn from |

## Workflow prompts

Three MCP **prompts** ship with the server: canned multi-tool workflows that encode which tool to call first, what to do with its output, and what the answer should look like. In Claude Code they appear as slash commands; Claude Desktop and Cursor surface them in their own prompt pickers.

| Prompt | Arguments | Workflow |
|---|---|---|
| `/mcp__placeroot__site_selection` | `business_type`, `area` | `search_categories` → `geocode` → `summarize_area` → `compare_areas` → `find_places` + `within_distance` → one ranked recommendation |
| `/mcp__placeroot__compare_neighborhoods` | `area_a`, `area_b` | `geocode`/`resolve_place` + `admin_lookup` → `summarize_area` ×2 → `compare_areas` → `summarize_buildings` → a small difference table |
| `/mcp__placeroot__plan_errands` | `stops`, `start` (optional) | `geocode_batch` → `distance_matrix` → `route` per leg → optional `places_along_route` → an ordered run with per-leg distance and duration |

```
/mcp__placeroot__site_selection bike repair shop | Portland, Oregon
```

Prompts cost **zero tokens in `tools/list`** — a client fetches them only on `prompts/list`, and only materializes one when you invoke it. A test asserts `tools/list` is byte-identical with and without them registered, so adding a workflow here can never grow the context every conversation pays for.

They are also registered under **every** `PLACEROOT_TOOLS` selection, including subsets that drop tools a prompt names — a workflow is still worth reading when one step is unavailable, and it costs a subset install nothing. When the active selection excludes a referenced tool, the rendered prompt ends with a note naming it and telling the agent to route around the gap rather than to attempt a call that would fail.

## Resources

Two MCP **resources** expose the server's argument-free lookups as attachable context, so you can pin them into a conversation without spending a tool call:

| Resource | Contents |
|---|---|
| `placeroot://data-version` | The resolved Overture release, its date, and how it was resolved (discovery, env override, or the pinned fallback). Same values the `data_version` tool returns — one shared code path, so they cannot drift. |
| `placeroot://categories` | Summary of the place-category taxonomy: all 22 top-level categories with how many slugs sit under each, plus how to get an exact slug. ~530 tokens — a summary, not the 2,117-slug CSV, which stays behind `search_categories`. |

In Claude Code they auto-complete as @-mentions:

```
@placeroot:placeroot://data-version
@placeroot:placeroot://categories
```

Claude Desktop and Cursor list them in their own attachment pickers.

Like prompts, resources are registered under **every** `PLACEROOT_TOOLS` selection: they never appear in `tools/list`, so gating them would save a subset install nothing. A test asserts `tools/list` is byte-identical with and without them registered.

## Loading fewer tools (`PLACEROOT_TOOLS`)

All 29 tool schemas cost roughly **13.1k tokens** of every conversation's context, paid before the agent asks anything — about the cost of 100 median answers. Most installs use a slice of that surface, so `PLACEROOT_TOOLS` selects which tools get registered. Unselected tools are never registered and never appear in `tools/list`.

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

The value is a comma-separated list of profile names, tool names, or both — the union of everything named:

| `PLACEROOT_TOOLS` | Tools | Schema tokens | Saved |
|---|---:|---:|---:|
| unset / `all` (default) | 29 | ~13,100 | — |
| `search` | 13 | ~6,100 | 53% |
| `core` | 10 | ~5,220 | 60% |
| `routing` | 6 | ~2,890 | 78% |
| `analysis` | 9 | ~3,240 | 75% |
| `geometry` | 3 | ~850 | 93% |

- **`core`** — `find_places`, `geocode`, `reverse_geocode`, `place_details`, `resolve_place`, `search_categories`, `summarize_area`, `route`, `places_along_route`. The single-purpose tools that answer most spatial questions; no batch siblings, no buildings/land-use, no rendering. `search_categories` is in for its own reason: `find_places`' `category` filter takes Overture taxonomy slugs, and a wrong slug comes back as zero results plus a note to look the slug up — a dead end without the lookup tool to call.
- **`search`** — the find/name/identify family: `find_places`, `place_details`, `geocode`, `resolve_place`, `reverse_geocode`, their `*_batch` siblings, `address_at`, `geocode_address`, `search_categories`, and `gers_lookup`.
- **`routing`** — `route`, `isochrone`, `distance_matrix`, `within_distance`, `optimize_route`.
- **`analysis`** — `summarize_area`, `summarize_buildings`, `compare_areas`, `buildings_at`, `land_use_at`, `infrastructure_at`, `water_near`, `admin_lookup`.
- **`geometry`** — `simplify_geometry`, `render_map`.

`data_version` is registered under every profile: it is ~230 tokens and the only way an agent can tell which Overture release backs its answers.

Profiles may overlap, and a list may mix them with bare tool names — `PLACEROOT_TOOLS=routing,find_places` or `PLACEROOT_TOOLS=find_places,geocode,route`. A name that is neither a profile nor a tool **fails at startup** with the list of valid names, rather than quietly falling back to loading everything. The server logs one line at startup naming what it registered (`registered 10 of 29 tools (PLACEROOT_TOOLS=core)`), so a selection that didn't apply — an empty value, a variable that never reached the process — is visible rather than silently the full 29.

## Design notes

Agents are bad at maps. Existing map tools either require vendor API keys or return payloads far too large for a context window. PlaceRoot's rule: every answer fits in a couple of thousand tokens, and anything bigger comes back as a summary.

A few things that set it apart:

- **Stable place ids.** Every place carries its Overture [GERS id](https://docs.overturemaps.org/gers/), so an agent can hold onto a place across turns and look it up again later instead of re-searching.
- **Built-in geocoding.** Place-name lookup works out of the box — no third-party geocoding service involved.
- **Fast repeat queries.** Frequently used data is cached locally, so repeat questions answer in milliseconds and keep working offline.
- **Self-hostable end to end.** Run it locally, serve it over HTTP, or point it at your own copy of the data — no dependency on anyone else's service.

## Documentation

Full docs, the complete tool reference, and setup guides: **[placeroot.dev](https://placeroot.dev)**

## License

MIT
