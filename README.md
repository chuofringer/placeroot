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

More on the way — see [ROADMAP.md](ROADMAP.md).

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
