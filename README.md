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
| `geocode` | Free-text place name -> ranked candidates (name, type, lat/lon, GERS id, admin context) |
| `reverse_geocode` | Point -> nearest address plus its containing division chain |
| `simplify_geometry` | Any GeoJSON geometry -> simplified to a token budget, with the deviation it cost |

More on the way — see [ROADMAP.md](ROADMAP.md).

## GERS ids

Every place PlaceRoot returns carries its Overture **GERS id** (Global Entity Reference System) in an `id` field. GERS ids are stable identifiers for real-world entities, not row numbers — the same place gets the same id across queries and across Overture releases, so an agent can hold onto one across turns (or persist it) and look the place back up later with `place_details(id=...)` instead of re-searching by name and location. `find_places`, `place_details`, `within_distance`, `admin_lookup`'s division entries, and `geocode` all return one.

### Geocoding, honestly

`geocode` and `reverse_geocode` are built entirely on Overture's `divisions`
and `addresses` themes — no Nominatim, no third-party geocoding API, no key.
Matching is deterministic (exact name > prefix > substring, falling back to
named places when divisions alone don't fill the result limit), not a
learned ranking model, so it's transparent but not state-of-the-art on
ambiguous or misspelled queries.

`scripts/geocode_benchmark.py` runs ~100 real-world queries (cities,
neighborhoods, "city, state" forms) against live Overture data and reports
hit@1/hit@5:

```bash
uv run python scripts/geocode_benchmark.py
```

Measured against a live release (113 queries): **hit@1 35.4%, hit@5 54.9%**.
Two concrete, honest gaps this surfaced, not smoothed over:

- **"City, ST" queries often miss.** Overture division names are bare
  ("Chicago", not "Chicago, IL"), so a query like `"Chicago, IL"` gets no
  division match and falls back to named places — surfacing businesses
  whose name happens to contain that string (e.g. a motel or clinic named
  "... - Chicago, IL") instead of the city.
- **Common city names are ambiguous worldwide**, and ranking has no
  population or prominence signal to break ties — `"Boston"` or `"Reno"`
  can rank a same-named locality in another country or state above the
  well-known one. Fixing this needs a real disambiguation signal (population,
  Wikidata-linked prominence); flagged as a follow-up rather than solved here.

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
