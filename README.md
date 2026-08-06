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

## Hosted / HTTP mode

By default `placeroot` speaks stdio, the standard MCP transport for a locally-run server (as above). It can also speak streamable-HTTP — the SDK's first-party HTTP transport, not a hand-rolled bridge — so it can run as a long-lived process reachable over the network:

```bash
uv run placeroot --http                          # binds 127.0.0.1:8321
uv run placeroot --http --host 0.0.0.0 --port 8080
```

This starts an MCP endpoint at `http://<host>:<port>/mcp` and can serve multiple concurrent requests (the underlying DuckDB connections are lock-serialized internally, so this is safe). Point a remote-capable MCP client at it:

```json
{
  "mcpServers": {
    "placeroot": {
      "url": "http://127.0.0.1:8321/mcp"
    }
  }
}
```

`--http` only starts the transport — it doesn't run behind TLS, auth, or a process supervisor, and it's on you to put one in front of it (a reverse proxy, `systemd`, etc.) for anything beyond local/trusted-network use. A public, no-setup hosted tier is tracked as a roadmap item — see [ROADMAP.md](ROADMAP.md) issue #24 — and isn't part of this repo yet.

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
ambiguous or misspelled queries. Same-tier ties break on population (or a
documented subtype-rank/hierarchy-depth/region-population proxy when
population is null) — see `src/placeroot/geocode.py`'s module docstring for
the full design.

The first `geocode()` call in a process materializes a local name table
from Overture's `divisions`/`type=division` theme (once per release,
~20-30s, logged) so every later query answers from a local parquet file
instead of a live S3 scan — set `PLACEROOT_CACHE=off` to disable this and
always scan upstream directly.

`scripts/geocode_benchmark.py` runs ~100 real-world queries (cities,
neighborhoods, "city, state" forms) against live Overture data and reports
hit@1/hit@5:

```bash
uv run python scripts/geocode_benchmark.py
```

Measured against a live release (113 queries): **hit@1 98.2%, hit@5 98.2%**
(up from an initial 35.4%/54.9%). The two biggest contributors, in order:
the local name table finally made it possible to push match-tier and
population into the SQL `ORDER BY` *before* the row-limit that feeds
Python-side ranking — without that, a common name with more matches than
the overfetch limit (122 divisions worldwide are literally named "Los
Angeles") could drop the well-known candidate before ranking ever saw it;
and "City, ST"/"City, Region" parsing plus the population/prominence
tiebreak (#46, #47) fixed the rest.

One concrete, honest gap remains, not smoothed over:

- **Abbreviated saint names still miss.** `"St. Louis"` and `"St. Petersburg"`
  don't match Overture's canonical names, which spell out "Saint" in full
  ("Saint Louis", "Saint Petersburg") — matching is substring-based, not a
  learned model, so `"St."` and `"Saint"` are just different strings to it.
  A small alias table (`St. -> Saint`, and the reverse) would close this;
  flagged as a follow-up rather than solved here, since it's a narrow
  string-normalization fix, not a ranking or performance one.

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
