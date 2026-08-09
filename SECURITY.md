# Security Policy

## Supported versions

Only the latest released version receives security fixes. There is no
hosted PlaceRoot service: the server runs on your machine (stdio) or on
infrastructure you operate (`--http`), so there is no server-side
deployment of ours to report against.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Report privately via GitHub's private vulnerability reporting on this
repository (Security → Report a vulnerability), or by email to
<hello@placeroot.dev> with `[placeroot security]` in the subject.

You can expect an acknowledgement within a week. Fixes ship as a normal
release; credit is given unless you ask otherwise.

## Threat model, so reports land well

PlaceRoot is a local MCP server that:

- reads public Overture Maps GeoParquet from S3 (or a mirror you configure
  via `PLACEROOT_UPSTREAM_BASE`),
- writes a local on-disk tile cache,
- optionally serves streamable-HTTP on `127.0.0.1:8321/mcp`, and
- for `render_map`, writes a self-contained HTML file to local disk.

The interesting surfaces are therefore: resource exhaustion reachable from
tool arguments (oversized bboxes, adversarial geometry), injection into
DuckDB statements, path handling on the `render_map` artifact write, and
anything that would make the HTTP transport unsafe to expose beyond
localhost. Regression tests for the known-hardened surfaces live in
`tests/test_security.py`.

Reports about the *data* (wrong places, stale POIs, miscategorized
features) are data-quality issues for the ordinary issue tracker — the
data comes from Overture's public release, not from us.
