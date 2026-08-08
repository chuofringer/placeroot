# Vendored competitor snapshots

Everything `benchmarks/competitor_comparison.py` knows about Mapbox MCP and the
archived Google Maps reference server is in this directory. Nothing here is
fetched at test time or at generation time — that is the point. Full provenance
(repo, commit hash, capture date, and *how* each file was produced) is in
[`provenance.json`](provenance.json); the rendered comparison and its
limitations live in [`../../docs/benchmarks-vs.md`](../../docs/benchmarks-vs.md).

```
provenance.json                   repo + commit + date + method for every file below
mapbox-mcp/tools_list.json        verbatim `tools/list` reply from their real server
google-maps-archived/
  tools_list.json                 their MAPS_TOOLS array, evaluated out of index.ts
answers.json                      what each competitor server really returned, per scenario
upstream_examples/*.json          the vendors' own documented example API responses,
                                  fed to their servers through a local stub during capture
capture/                          the scripts that produced all of the above (need network,
                                  need Node, run by hand — never by CI or by pytest)
```

Refreshing a snapshot: see the last section of
[`../../docs/benchmarks-vs.md`](../../docs/benchmarks-vs.md) for the exact
commands, then update the commit hashes and `captured` date in
`provenance.json` and rerun
`uv run python benchmarks/competitor_comparison.py --write`.

## Third-party content

These are snapshots of other projects' output, kept for a reproducible
benchmark and unmodified. Both sources are MIT-licensed:
[mapbox/mcp-server](https://github.com/mapbox/mcp-server) © Mapbox, Inc., and
[modelcontextprotocol/servers-archived](https://github.com/modelcontextprotocol/servers-archived)
`src/google-maps` © Anthropic, PBC. The files under `upstream_examples/` are
example responses excerpted from Mapbox's and Google's public API reference
documentation, cited per file in `provenance.json`.
