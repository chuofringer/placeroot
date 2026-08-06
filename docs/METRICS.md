# Traction metrics we own

Stars measure other people's attention, not whether PlaceRoot is worth
building. The Phase 2 traction bar (PLAN.md) is judged on the four signals
below — each measurable from data we control, each with a concrete
collection method, reviewed on a fixed cadence.

## The four signals

### 1. Download trend (four-week slope, not absolute count)

- **PyPI**: `https://pypistats.org/api/packages/placeroot/recent` (or the
  `pypistats` CLI). Record weekly; the signal is week-over-week direction
  across four consecutive weeks, not any single number.
- **npm**: `https://api.npmjs.org/downloads/range/last-month/placeroot`.
- Collection starts the week #16's publishes land. Until then this signal
  is intentionally blank — do not backfill or estimate.

### 2. Distinct connecting clients

MCP clients identify themselves in the `initialize` handshake
(`clientInfo.name`/`version`). The stdio server sees this today; a
follow-up can log a daily count of distinct client names (locally, no
telemetry phoned home — a hosted endpoint (#24) may count connections
server-side and that is the only place counting happens). Signal: number
of distinct client *names* seen in a week (Claude Desktop, Cline, Zed, …)
— breadth of integration, not volume.

### 3. Inbound issues and PRs from people we did not contact

Count issues/PRs opened by GitHub accounts that are not the maintainer
and were not solicited in outreach threads. Collection:
`gh issue list --json author` filtered against a small known-contacts
list kept in this file's appendix. One unsolicited, reproducible bug
report is worth more than a hundred stars: it means someone ran it
against real work.

### 4. Named real uses

A hand-maintained list (appendix below) of concrete, verifiable uses:
a project README that names placeroot, a blog post, a talk, a company
that says "we use this." Each entry needs a URL or a named person. No
anonymous "someone said" entries.

## Cadence and decision rule

- Record all four in a dated row of the appendix table every Monday
  during Phase 2 (weeks 8–13).
- The Phase 3 gate (PLAN.md) is judged at ~week 14 on: positive download
  slope over the last four weeks, ≥3 distinct client names, ≥5
  unsolicited inbound items, ≥2 named real uses. Miss the bar → Phase 3
  waits; the local server keeps shipping regardless.
- Stars are recorded in the same table for context, never as a gate.

## Appendix

### Weekly log

| date | pypi wk downloads | npm wk downloads | distinct clients | inbound (unsolicited) | named uses | stars (context) |
|---|---|---|---|---|---|---|
| (starts at first publish) | | | | | | |

### Known-contacts list (excluded from "inbound")

- (maintainer)

### Named real uses

- (none yet — that's the honest starting point)
