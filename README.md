# PlaceRoot

**Ground AI agents in open map data.**

PlaceRoot is an MCP server that answers spatial questions from [Overture Maps](https://overturemaps.org) — queried live with DuckDB, no API key, no signup, no vendor platform.

- **Answers, not data dumps.** Every tool returns compact, ranked results that fit in an agent's context window (~2K tokens), never raw GeoJSON.
- **Fresh, rich place data.** Operating status, confidence scores, and brands from Overture (contributed by Meta, Uber, TomTom, and others).
- **Zero setup.** Queries run directly against Overture's public GeoParquet on S3 — no ETL, no database, no key.

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

Or run it directly:

```bash
uv run placeroot          # stdio MCP server
uv run placeroot --http   # streamable-HTTP endpoint at http://127.0.0.1:8321/mcp
```

`--http` serves plain HTTP with no TLS or auth — put a reverse proxy in front for anything beyond local use.

## Tools

| Tool | Answers |
|---|---|
| `find_places` | Named places near a point, nearest first, with category, confidence, and operating status |
| `summarize_area` | What's in an area: total places and top categories |
| `place_details` | One place in full: addresses, contacts, brand, sources, confidence |
| `admin_lookup` | The admin hierarchy containing a point: neighborhood up to country |
| `compare_areas` | 2–5 areas side by side: category mix, density, and what differs most |
| `within_distance` | Is the nearest matching place within N meters of a point? |
| `geocode` | Free-text place name → ranked candidates with coordinates and admin context |
| `reverse_geocode` | Point → nearest address plus its containing division chain |
| `simplify_geometry` | Any GeoJSON geometry → simplified to a token budget |

More on the way — see [ROADMAP.md](ROADMAP.md).

## Why

Agents are bad at maps. Existing map tools either require vendor API keys or return raw GeoJSON far too large for a context window. PlaceRoot's design rule: every answer fits in ~2K tokens, and anything bigger returns a summary plus a link.

A few things that make it work:

- **GERS ids everywhere.** Every place carries its stable Overture [GERS](https://docs.overturemaps.org/gers/) id, so an agent can hold onto a place across turns and look it up again with `place_details(id=...)` instead of re-searching.
- **Keyless geocoding.** `geocode`/`reverse_geocode` are built entirely on Overture's divisions and addresses themes — deterministic matching, no third-party geocoding API. 100% hit@1 on a ~113-query real-world benchmark (`scripts/geocode_benchmark.py`).
- **Local caching.** Hot data is cached on first use, so repeat queries answer in milliseconds and keep working offline. Set `PLACEROOT_CACHE=off` to always query upstream.
- **Self-hostable end to end.** Optionally mirror the data to your own S3-compatible storage and point PlaceRoot at it — see [docs/MIRROR.md](docs/MIRROR.md).

## Development

```bash
uv sync                    # installs pytest + ruff (dev dependency group)
uv run pytest              # offline tests against committed fixtures
uv run pytest -m live      # also run opt-in tests against real Overture S3
uv run ruff check .
```

## License

MIT
