# PlaceRoot

**Ground AI agents in open map data.**

PlaceRoot is an MCP server that answers spatial questions from [Overture Maps](https://overturemaps.org) open data — queried live from the public GeoParquet release with DuckDB.

- **No API key. No signup. No vendor platform.**
- **Answers, not data dumps** — every tool returns compact, ranked, token-budgeted results an agent can actually use.
- **Fresh place data** — Overture's `operating_status` and confidence signals (contributed by Meta, Uber, TomTom, and others), not a stale snapshot.
- **No ETL** — queries run directly against Overture's S3 GeoParquet with row-group pruning.

## Quick start

```bash
uv run placeroot        # stdio MCP server
```

Claude Desktop / Claude Code config:

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

## Tools

| Tool | Answers |
|---|---|
| `find_places` | Named places near a point, nearest first, with category, confidence, and operating status |
| `summarize_area` | What's in an area: total places and top categories |
| `place_details` | One place in full: addresses, websites, phones, socials, brand, source attribution, confidence, operating status — by GERS id or by name + point |
| `admin_lookup` | The admin hierarchy containing a point: neighborhood up to country |
| `compare_areas` | 2-5 areas side by side: category mix, place density, and what differs most |
| `within_distance` | Is the nearest place matching a category/name within N meters of a point? |

More on the way — see [ROADMAP.md](ROADMAP.md).

## GERS ids

Every place PlaceRoot returns carries its Overture **GERS id** (Global Entity Reference System) in an `id` field. GERS ids are stable identifiers for real-world entities, not row numbers — the same place gets the same id across queries and across Overture releases, so an agent can hold onto one across turns (or persist it) and look the place back up later with `place_details(id=...)` instead of re-searching by name and location. `find_places`, `place_details`, `within_distance`, and `admin_lookup`'s division entries all return one.

## Development

```bash
uv sync                    # installs pytest + ruff (dev dependency group)
uv run pytest              # offline tests, run against a committed fixture
uv run pytest -m live      # also run the opt-in test against real Overture S3
uv run ruff check .
```

## Why

Agents are bad at maps. Existing map tools for agents either require vendor API keys or return raw GeoJSON far too large for a context window. PlaceRoot's design rule: every answer fits in ~2K tokens, and anything bigger returns a summary plus a link.

## License

MIT
