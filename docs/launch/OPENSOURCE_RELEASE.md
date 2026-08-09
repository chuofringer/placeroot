# Open-source release checklist

Going from a private `chuofringer/placeroot` to a public repository, and what
follows for releases, docs and the website.

Ordered by when it has to happen. Everything in **Phase 1** must land *before*
the visibility flip, because it is either irreversible after the fact (history,
secrets) or embarrassing on day one (missing `CONTRIBUTING.md` on a repo that
just hit Hacker News). Phases 3+ are followups you can work through over the
first weeks.

**Where this repo already stands:** 50 commits, single author, 66 test modules,
CI on 3.11–3.13 with ruff + offline pytest + a browser smoke job, trusted
publishing to PyPI and npm with no stored tokens, MIT license, `server.json` and
`mcpb/manifest.json` registry manifests, third-party benchmark snapshots with
full `provenance.json`, a bundled Overture taxonomy CSV with a pinned source and
refresh instructions, and a weekly upstream canary. That is further along than
most projects are on their *first* public day. The gaps below are almost all
community-surface and legal-hygiene, not engineering.

---

## Phase 0 — decisions to make first

These change what you do in every later phase, so settle them before writing
files.

- [ ] **Identity.** `LICENSE`, `pyproject.toml`, `npm/package.json` and
      `mcpb/manifest.json` all carry `Vibe Mapper <chuo.fringer@gmail.com>`.
      That address is already public on PyPI and npm, so nothing is *leaked* by
      the flip — but decide now whether the project keeps a pseudonym or moves
      to a real name / project alias, because the copyright line in `LICENSE` is
      the awkward one to change after contributors start signing on to it.
      Consider a dedicated address (e.g. `hello@placeroot.dev`) so your personal
      inbox isn't the security contact.
- [ ] **Governance shape.** Solo-maintainer BDFL (fine, and honest) vs.
      inviting co-maintainers. This determines whether you need `MAINTAINERS.md`
      and a decision process, or just a line in `CONTRIBUTING.md`.
- [ ] **What stays private.** See Phase 1 → "Documents that should not ship
      as-is". `PLAN.md` and `docs/launch/` are internal strategy documents, not
      user docs.
- [ ] **Contribution licensing.** Inbound = outbound under MIT (the default,
      stated in `CONTRIBUTING.md`) vs. a CLA/DCO. Recommendation: inbound =
      outbound, plus DCO sign-off if you want a paper trail. A CLA suppresses
      drive-by contributions and buys you nothing unless you plan to relicense.
- [ ] **Support expectation.** Say it out loud in the README: is this
      "maintained, issues answered within a week" or "works, PRs welcome, no
      SLA"? Setting it low and beating it is better than the reverse.

---

## Phase 1 — before the visibility flip

### 1.1 Secrets and history audit (irreversible after the flip)

Every commit, branch and tag becomes public and gets mirrored by scrapers within
minutes. Assume anything ever committed is permanently public.

- [x] No secret-shaped strings in the working tree (scanned: no API keys,
      tokens, private keys, `.env` files; `.gitignore` already excludes `.env`,
      `.env.*`, `.claude/`, `.wrangler/`).
- [x] No credentials in workflows — `deploy-site.yml` reads
      `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` from repo secrets, which
      is correct; PyPI and npm use OIDC trusted publishing with no stored
      tokens at all.
- [x] No `pull_request_target` anywhere (the classic fork-PR secret-exfil
      vector).
- [ ] **Run a proper history scan anyway**, not just a working-tree grep — a
      secret deleted in a later commit is still in the pack:
      ```bash
      pipx run detect-secrets scan --all-files
      pipx run trufflehog git file://. --since-commit $(git rev-list --max-parents=0 HEAD)
      # or: docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest detect -s /repo
      ```
- [ ] **Review all 50 commit messages and diffs** for internal-only content —
      local paths, hostnames, screenshots, personal notes, anything referring to
      systems that aren't yours to name.
      ```bash
      git log -p | grep -niE "localhost:|/Users/|/home/[a-z]|internal|todo\(.*\)|xxx|hack"
      ```
- [ ] Confirm the committer email on every commit is the one you intend to be
      public: `git log --format='%an <%ae>' | sort -u` → currently a single
      identity, consistent. If you want it changed, that is a full history
      rewrite and it must happen **before** the flip.
- [ ] Check for large or stray blobs you don't want redistributed:
      `git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectname) %(objsize) %(rest)' | sort -k3 -n -r | head -20`
      (pack is 2.23 MiB today — healthy; the `.parquet` test fixtures and
      `site/placeroot.mcpb` are the biggest items and all belong.)
- [ ] If anything must be scrubbed: rewrite with `git filter-repo` **before**
      flipping, and rotate the leaked credential regardless. After the flip,
      rewriting doesn't help — forks and caches keep the old objects.

### 1.2 Legal and attribution (the highest-value gap)

This is the one substantive risk in an otherwise clean repo. PlaceRoot's code is
MIT, but the *data* it queries and redistributes is not uniformly licensed, and
nothing in the repo currently says so.

- [ ] **Add `docs/DATA-LICENSE.md`** (or an `## Attribution` section in the
      README) covering the Overture theme licenses:
      - `places` — CDLA-Permissive-2.0 (attribution, no share-alike)
      - `transportation`, `divisions`, `addresses`, `base` — substantially
        OSM-derived, **ODbL 1.0**, which carries share-alike obligations on
        *derived databases* and an attribution requirement on *produced works*
      - `buildings` — mixed sources, per-feature; check the `sources` array
      Confirm each against the current Overture release notes before publishing
      the claim — theme licensing has shifted between releases, and a
      confidently wrong license page is worse than none.
- [ ] **Fix the map attribution string.** `src/placeroot/mapview.py:41` renders
      `© Overture Maps Foundation contributors`. Every rendered map that shows
      routing, admin boundaries or addresses is a produced work over ODbL data
      and conventionally needs `© OpenStreetMap contributors` and an ODbL
      pointer alongside it. Cheap to fix, awkward to be called out on publicly.
- [ ] **Say what downstream users owe.** The README's "open data you may store,
      cache, and train on" line (echoed in `PLAN.md` positioning) is the pitch
      against Google/Mapbox. It is broadly right for CDLA places data and much
      shakier for ODbL themes. Soften it to something you can defend, and point
      at `docs/DATA-LICENSE.md` for the specifics — a keyless-open-data project
      that is sloppy about open-data licensing is the single most likely source
      of a hostile top comment on launch day.
- [ ] `src/placeroot/data/overture_categories.csv` — already documented with
      source, pinned schema tag `v1.9.0` and a refresh procedure. Add the
      upstream schema repo's license to that README for completeness.
- [ ] `benchmarks/competitors/` — vendored third-party snapshots. Provenance is
      already exemplary (`provenance.json` records repo, commit, date and
      capture method per file; the README states both sources are MIT). Verify
      that the *Mapbox docs example payloads* in `upstream_examples/` are
      redistributable — they carry embedded `"attribution"` notices asserting
      "may not be retained" on the Mapbox terms. Those are Mapbox's own
      published documentation examples, so fair use for benchmarking is a
      defensible position, but note it explicitly in the README rather than
      leaving it to be discovered.
- [ ] Add a `NOTICE` file if you end up with third-party code (not just data)
      in the tree. Currently you don't.

### 1.3 Community health files (all currently missing)

GitHub's "Community Standards" page will grade these; more importantly they are
what a first-time visitor checks before opening an issue.

- [ ] **`CONTRIBUTING.md`** — dev setup (`uv sync`, `uv run pytest`,
      `uv run ruff check .`), the offline-vs-`-m live` test split, the fixture
      story (`scripts/build_*_fixture.py`), the "answers not data dumps" design
      rules from `ROADMAP.md`, how to propose a new tool given the token-budget
      constraint, commit/PR conventions, and inbound=outbound licensing.
      **Include the token-budget rule prominently** — the single most likely bad
      first PR is one that adds a tool without accounting for its schema cost in
      `tools/list`.
- [ ] **`CODE_OF_CONDUCT.md`** — Contributor Covenant 2.1, with a real reporting
      address (see Phase 0 identity decision).
- [ ] **`SECURITY.md`** — supported versions, how to report privately, response
      expectations. Enable **GitHub Private Vulnerability Reporting** and point
      at it so nothing lands in a public issue. Note the actual threat model:
      the server runs locally, reads public S3 data, writes a local cache and
      (for `render_map`) writes an HTML file — so the interesting surfaces are
      the ones `tests/test_security.py` already covers (resource exhaustion via
      oversized bbox / adversarial geometry, DuckDB `SET` interpolation) plus
      path handling on the artifact write.
- [ ] **`.github/ISSUE_TEMPLATE/`** — `bug_report.yml`, `feature_request.yml`,
      `tool_request.yml`, plus `config.yml` linking the site and Discussions.
      Bug reports should ask for the MCP client, the `PLACEROOT_TOOLS` value,
      the Overture release from `data_version`, and the Python version — those
      four answer most reports on their own.
- [ ] **`.github/pull_request_template.md`** — tests pass, ruff clean, README
      and site synced if tools changed (call out `tests/test_site_tools_sync.py`
      and `tests/test_npm_readme_sync.py` so contributors aren't surprised by
      those failures).
- [ ] **`CHANGELOG.md`** — Keep a Changelog format, backfilled from the git log
      for 0.1 through 0.8.0. You have clean squash-merged commits with PR
      numbers, so this is an hour of work and it is the artifact people check
      before upgrading.
- [ ] **`.github/dependabot.yml`** — `github-actions`, `pip`/`uv` and `npm`
      ecosystems, weekly.
- [ ] Optional: `CITATION.cff` (this is plausibly citable in GIS research),
      `.github/FUNDING.yml`, `GOVERNANCE.md`.

### 1.4 CI/CD hardening for a public repo

Everything below is about the fact that after the flip, strangers can open PRs
that trigger your workflows.

- [ ] **Add least-privilege `permissions:` at the top of every workflow.**
      `ci.yml` and `deploy-site.yml` have no top-level `permissions` block, so
      they inherit the repo default. Set `permissions: contents: read` at the
      workflow level and grant more only per-job (`release.yml` and
      `prepare-release.yml` already scope theirs per-job — good).
- [ ] **Repo → Settings → Actions → General:**
      - Workflow permissions → **Read repository contents** (not read-write)
      - **Require approval for all outside collaborators'** workflow runs
      - Restrict which actions can run (allow `actions/*`, `astral-sh/*`,
        `pypa/*` and any others you actually use)
- [ ] **Pin third-party actions to full commit SHAs**, not floating tags.
      `astral-sh/setup-uv@v3` and `pypa/gh-action-pypi-publish@release/v1` are
      the ones worth pinning; Dependabot will keep the SHAs current once
      `dependabot.yml` lands.
- [ ] **Re-read `deploy-site.yml`'s branch policy.** It deploys *every* branch
      that touches `site/` — main to production, everything else as a Cloudflare
      preview. Fork PRs can't reach secrets, so this is safe by default, but
      once you have collaborators, a push to any branch publishes a real preview
      URL under your domain. Decide if you want a branch filter.
- [ ] **Decide between `pages.yml` and `deploy-site.yml`.** Two site-deploy
      workflows for one site is confusing on a public repo. Cloudflare is
      clearly the live path; keep the Pages one only if you want it documented
      as the fork-friendly option, and say so in `docs/WEBSITE.md`.
- [ ] Enable **Secret scanning + push protection** and **Dependabot alerts**
      (both free on public repos, not enabled by default on the flip).
- [ ] **Branch protection on `main`:** require CI to pass, require a PR, no
      force pushes, no deletion. Even solo, this stops an accidental
      `git push --force` from rewriting public history other people have forked.
- [ ] Consider adding an OpenSSF **Scorecard** workflow and its badge — cheap,
      and it answers the supply-chain question before anyone asks it.

### 1.5 Documents that should not ship as-is

Public repos publish *everything*, including your notes about other people.

- [ ] **`PLAN.md`** — the positioning table describes named competing projects
      as "dormant", "0★ each", "abandoned >1yr", "near-dormant", and characterizes
      specific maintainers' work as a "near-clone". It is accurate market
      research and it reads as punching down when it's a top-level file in a
      public repo. Options, best first:
      1. Move it to a private notes repo and drop it from the tree.
      2. Rewrite as a public `docs/POSITIONING.md` that describes *what
         PlaceRoot chose to do differently* without star counts or liveness
         judgments about named projects.
      3. Keep it and accept that the first person who finds it will screenshot
         the table.
      The README already links `PLAN.md` — update that link with whatever you
      decide.
- [ ] **`docs/launch/`** — `show-hn.md` (title candidates, planted first
      comment), `outreach.md` (per-subreddit drafts) and
      `registry-submissions.md` are launch-marketing artifacts. Publishing the
      Show HN draft *before* you post it is a self-inflicted wound; HN readers
      reliably find and quote "post immediately after submitting" instructions.
      Move `show-hn.md` and `outreach.md` out of the repo. `registry-submissions.md`
      is genuinely useful reference and can stay.
      `post-why-agents-are-bad-at-maps.md` is a good essay — publish it on the
      site rather than leaving it in `docs/launch/`.
- [ ] **`ROADMAP.md`** links a personal GitHub project board
      (`/users/chuofringer/projects/2`). Confirm the board's visibility matches
      your intent — a private board behind a public link is a dead end for
      readers; a public one exposes every card you've written.
- [ ] Sweep the codebase for issue-number references (`#14`, `#33`, `#203`
      appear in comments and docstrings). Once public these become live links —
      which is good — but verify the referenced issues exist and read sensibly
      to a stranger.

### 1.6 Restore the "repo is private" workarounds

Several files were deliberately written around the private repo and must be
un-written the moment it goes public:

- [ ] **`pyproject.toml`** carries an explicit comment saying the repo is
      private and routing `Issues`/`Documentation` to the website instead.
      Restore:
      ```toml
      [project.urls]
      Homepage = "https://placeroot.dev"
      Repository = "https://github.com/chuofringer/placeroot"
      Issues = "https://github.com/chuofringer/placeroot/issues"
      Documentation = "https://placeroot.dev/how-it-works.html"
      Changelog = "https://github.com/chuofringer/placeroot/blob/main/CHANGELOG.md"
      ```
      and delete the comment. This only reaches PyPI on the next release.
- [ ] **`npm/package.json`** has no `repository` or `bugs` field — add both.
      Also missing on npm today.
- [ ] **`README.md` has zero GitHub links.** Add: a source link, badges (CI
      status, PyPI version, npm version, license, Python versions), and links to
      `CONTRIBUTING.md` / `SECURITY.md` / `CHANGELOG.md`.
- [ ] **The website has zero GitHub links** (`grep -i github site/*.html`
      returns nothing). Every page needs a source link — for a keyless
      open-source pitch, "where's the code" is the first question a visitor has.
- [ ] `server.json` and `mcpb/manifest.json` already point at the GitHub repo —
      those links start working on the flip, no change needed.

### 1.7 Final pre-flip verification

- [ ] `uv run ruff check . && uv run pytest` clean locally
- [ ] `uv run pytest -m live` passes (the network-touching suite CI skips)
- [ ] `uv build` produces a clean sdist/wheel; inspect the sdist contents
      (`tar tzf dist/*.tar.gz`) to confirm the `[tool.hatch.build.targets.sdist]`
      excludes still do what you expect after adding new files
- [ ] Fresh-clone smoke test in a clean container: `uvx placeroot` and
      `npx placeroot` from the published packages, and a from-source run
- [ ] README's quick-start JSON pasted verbatim into a real Claude Desktop
      config, end to end
- [ ] All 29 tools called once against live data (the `-m live` suite should
      cover this; verify it actually does)

---

## Phase 2 — the flip

Do this on a weekday morning when you can watch it, not before a weekend.

1. [ ] Merge everything from Phase 1 to `main`; confirm CI green.
2. [ ] **Settings → General → Danger Zone → Change visibility → Public.**
3. [ ] Immediately re-enable the things the flip resets or newly offers:
       secret scanning + push protection, Dependabot alerts + security updates,
       private vulnerability reporting, branch protection (verify it survived),
       Actions permissions (verify).
4. [ ] Fill in the repo **About** panel: description, `https://placeroot.dev`,
       and topics — `mcp`, `model-context-protocol`, `overture-maps`,
       `openstreetmap`, `geospatial`, `gis`, `duckdb`, `ai-agents`, `geocoding`,
       `routing`, `python`. Topics are how people find you inside GitHub.
5. [ ] Turn on **Discussions** (questions belong there, not in issues). Turn
       **Wiki off** — docs live in `docs/`.
6. [ ] Set up issue **labels** (`bug`, `enhancement`, `good first issue`,
       `help wanted`, `tool-request`, `docs`, `upstream`) and tag 3–5 real
       `good first issue`s from `ROADMAP.md` before you announce anywhere. An
       empty issue tracker on launch day converts nobody.
7. [ ] **Backfill releases and tags.** There are no git tags at all today, and
       the release workflow keys off published releases. Tag `v0.8.0` at minimum
       and publish a GitHub Release with notes so the Releases page isn't empty.
8. [ ] Verify the previously-404ing links now resolve: `server.json`'s
       repository URL, `mcpb/manifest.json`'s, the `ROADMAP.md` project board.
9. [ ] Cut **v0.8.1** (or 0.9.0) shortly after, purely to push the corrected
       `project.urls` and `npm` repository metadata to PyPI and npm — registry
       metadata only updates on publish.

---

## Phase 3 — release process followups

- [ ] **Write down a versioning policy** in `CONTRIBUTING.md` or
      `docs/PUBLISHING.md`: semver, and specifically what counts as breaking for
      an MCP server. Tool removal and a changed response shape are breaking;
      adding a tool is minor; loosening a filter is patch. Agents parse your
      output — response-shape changes hurt more than in a normal library.
- [ ] **Deprecation policy for tools.** One minor release of warning minimum,
      announced in `CHANGELOG.md` and in the tool description itself.
- [ ] **`CHANGELOG.md` enforcement** — a CI check that any PR touching `src/`
      also touches the changelog (or carries a `no-changelog` label).
- [ ] **Release notes automation** — derive GitHub Release bodies from the
      changelog section so the two never drift.
- [ ] **Supply chain:** npm already publishes with `--provenance`; confirm PyPI
      attestations are on (`gh-action-pypi-publish` emits them by default now).
      Consider an SBOM (CycloneDX) as a release asset.
- [ ] **Publish to the official MCP registry** using `server.json` — currently a
      registry search for "overture" returns nothing, per `PLAN.md`. That is a
      standing opportunity and it needs the public repo to exist first
      (`io.github.chuofringer/*` namespace ownership is proven via the repo).
- [ ] **The MCPB bundle** (`site/placeroot.mcpb`) ships from the site. Once
      public, also attach it as a **GitHub Release asset** — that's where people
      will look, and it gives you download counts.
- [ ] **Upstream-drift alerting.** The weekly `overture-canary.yml` is the right
      instinct; verify it opens an issue (rather than only failing a run) so a
      failure is visible without watching Actions.
- [ ] **Test the release runbook by following it literally** — `docs/PUBLISHING.md`
      is written for you; have a fresh reader (or a fresh session) execute it and
      note where it assumes context.
- [ ] **Support matrix statement:** Python 3.11–3.13 is tested; add a note on
      Node for the npm launcher (`engines` says ≥18) and on which Overture
      releases are supported.

---

## Phase 4 — docs followups

The README is excellent — thorough, honest about limitations, and it already
does the hardest job (explaining *why* this exists). It is also ~14k and
carrying the entire documentation load. Split it.

- [ ] **Trim the README to a landing page**: what it is, why, quick start, the
      tool table, limitations, links out. Move deep material to `docs/`.
- [ ] **`docs/TOOLS.md`** — full reference per tool: arguments, response shape,
      error modes, worked example. Generate it from the registered tool
      definitions so it can't drift (you already have drift tests for the site's
      tool grid — extend that machinery).
- [ ] **`docs/ARCHITECTURE.md`** — the thing a contributor needs and cannot get
      from the README: DuckDB over Overture GeoParquet on S3, bbox row-group
      pushdown, the tile cache, the routing graph build, the token-budget layer,
      and how a request flows through `server.py` → theme module → `db.py`.
      This is the single highest-leverage doc for getting outside contributions.
- [ ] **`docs/CONFIGURATION.md`** — every env var in one table:
      `PLACEROOT_TOOLS`, `PLACEROOT_CACHE`, `PLACEROOT_TOKEN_BUDGET`, the
      release-override and S3 settings. They're currently scattered across the
      README and code comments.
- [ ] **`docs/TROUBLESHOOTING.md`** — seed it from your own bugs: slow first
      query (cold cache / S3 latency), no results for a bad category slug,
      addresses outside the 39-country coverage, DuckDB extension install
      failures behind a proxy, `PLACEROOT_TOOLS` typos failing at startup.
- [ ] **`docs/SELF_HOSTING.md`** — the README claims "self-hostable end to end"
      and "point it at your own copy of the data". Show it: HTTP mode behind a
      reverse proxy, a local Overture mirror (`docs/MIRROR.md` is a start),
      resource expectations.
- [ ] **`docs/DATA-LICENSE.md`** — from Phase 1.2. Non-optional.
- [ ] **Per-client setup guides** — Claude Desktop, Claude Code, Cursor,
      VS Code/Copilot, Codex CLI, Continue, LM Studio. Each is short and each is
      a search-traffic entry point.
- [ ] **More `examples/`** — you have one (`site_selection`). Add 3–4 runnable
      notebooks/scripts: errand planning, neighborhood comparison, isochrone
      coverage analysis, GERS-id joins against a user's own dataset.
- [ ] **`docs/benchmarks*.md`** — already strong, unusually honest about
      limitations (the `known_understatements` list in `provenance.json` is a
      credibility asset). Surface that honesty in the rendered page rather than
      burying it in JSON; add a "how to reproduce" section.
- [ ] **A docs site.** The `site/` is marketing (3 pages). Once `docs/` grows
      past ~8 files, put MkDocs Material or Astro Starlight on
      `docs.placeroot.dev` with search and versioning, and keep `site/` as the
      landing page.
- [ ] **`llms.txt`** at the site root — an MCP server's own audience is agents;
      not shipping one is a missed joke and a missed integration path.

---

## Phase 5 — website followups

- [ ] **Add GitHub links everywhere** (Phase 1.6) — nav, footer, and a "Star on
      GitHub" affordance on the landing page. Currently zero.
- [ ] **Badges/social proof block** once there is any: stars, PyPI downloads,
      registry listings, "used by".
- [ ] **Publish the essay** (`post-why-agents-are-bad-at-maps.md`) as a blog
      post at `placeroot.dev/blog/why-agents-are-bad-at-maps`. It's the best
      top-of-funnel asset you have and it should exist at a linkable URL
      *before* you post anywhere, so submissions can point at it.
- [ ] **A live demo.** The strongest possible landing page for this project is
      an interactive one: type a query, see the compact answer and the rendered
      map, next to what a raw GeoJSON dump would have cost. You already generate
      self-contained HTML maps — the machinery is there.
- [ ] **SEO/meta**: per-page `<title>`/`description`, canonical URLs, JSON-LD
      `SoftwareApplication`, OG images per page (one `og-image.png` today),
      updated `sitemap.xml` for any new pages.
- [ ] **Analytics** — privacy-respecting (Plausible/Umami/Cloudflare Web
      Analytics), so you can tell which install path people actually use.
      `docs/METRICS.md` suggests you've thought about this; wire it up.
- [ ] **Version-drift guards**: `tests/test_site_version_sync.py` and
      `test_site_tools_sync.py` already gate this and the release workflow
      re-runs them against the tagged tree. Extend the same pattern to any new
      page, and to the `placeroot.mcpb` bundle's version.
- [ ] **Accessibility + performance pass** — contrast, keyboard nav, alt text,
      Lighthouse. Cheap now, annoying later.
- [ ] **Custom 404** pointing back to the tool list and the repo.
- [ ] **Resolve the two-deploy-workflow ambiguity** in `docs/WEBSITE.md`
      (Phase 1.4) so an outside contributor knows which one is real.

---

## Phase 6 — launch and distribution

Sequence matters: registries and docs first (so arrivals find a complete
project), social last (so the spike lands on something finished).

- [ ] Official **MCP registry** submission via `server.json`
- [ ] `awesome-mcp-servers` (wong2's and punkpeye's), `awesome-geospatial`,
      `awesome-overture` if it exists — `docs/launch/registry-submissions.md`
      has the paste-ready entries
- [ ] Third-party MCP directories: Smithery, Glama, PulseMCP, mcp.so, Cursor's
      directory, VS Code's MCP gallery
- [ ] Claude connector directory submission (per `PLAN.md`, the slot is open)
- [ ] Overture Maps community: their Discord/forum and the monthly community
      call — a keyless MCP server over their data is exactly what they're
      promoting, and their amplification is worth more than any subreddit
- [ ] Show HN (draft ready — post the essay first, link it from the comment)
- [ ] r/gis, r/openstreetmap, r/LocalLLaMA, r/mcp; Mastodon `#OpenStreetMap`;
      the OSM weekly newsletter (weeklyOSM) — they cover tooling and reach
      exactly the right readers
- [ ] Have **`good first issue`s, a CHANGELOG, and Discussions live** before any
      of the above. Traffic converts to contributors only if there's an obvious
      next step.
- [ ] **Be present for 48h after each post.** Response latency on launch day is
      the single biggest variable in how these go.

---

## Phase 7 — ongoing

- [ ] **Triage cadence** — a fixed window (e.g. weekday mornings) beats
      best-effort. Label, respond, close stale.
- [ ] **A public roadmap** that reflects reality — `ROADMAP.md` is good; keep it
      current, since a stale roadmap is the main signal people use to judge
      whether a project is alive (see your own `PLAN.md` competitor table).
- [ ] **Upstream tracking** — the Overture places taxonomy migration (flat
      `categories` → `taxonomy`/`basic_category`, ~Sept 2026, already flagged in
      `src/placeroot/data/README.md`) is a known breaking change with a date on
      it. Put it on the roadmap with a target release.
- [ ] **Co-maintainers.** Solo bus factor is the biggest long-term risk to an
      infrastructure project. Watch for a repeat contributor and hand out commit
      access early rather than late.
- [ ] **Say when you stop.** If it ever goes unmaintained, archive it and say so
      in the README. Every project in your competitor table failed at this.

---

## Quick reference — the six things that actually block the flip

Everything else can follow. These cannot:

1. History/secret scan with a real scanner, not a grep (§1.1)
2. Data licensing page + corrected OSM/ODbL attribution in `mapview.py` (§1.2)
3. `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue templates (§1.3)
4. Workflow `permissions:` blocks, SHA-pinned actions, Actions settings (§1.4)
5. Decide the fate of `PLAN.md` and `docs/launch/show-hn.md` (§1.5)
6. Restore GitHub URLs in `pyproject.toml`, `npm/package.json`, README, site (§1.6)
