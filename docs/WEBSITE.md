# Marketing site runbook (`site/`)

The three-page marketing site is plain static HTML/CSS/JS — no framework, no
build step. Recreated from the design handoff (issue #84).

- `site/index.html` — landing (hero typewriter, use cases, positioning,
  under-the-hood, developer section with every registered tool — kept in
  sync with `server.py` by `tests/test_site_tools_sync.py`)
- `site/how-it-works.html` — the animated four-node pipeline
- `site/add-to-your-ai.html` — the tabbed per-tool installer
- assets: `logo-mark.svg` (also the favicon), `logo-lockup.png`, `og-image.png`

Fonts (Sora + Fira Code) load from Google Fonts; everything else is local.
`tests/test_site.py` guards structure, the external-reference allowlist, the
verbatim install commands, and cross-links.

## Per-release refresh
Every release, update the site in the same change that bumps the version
(step 2 of [PUBLISHING.md](PUBLISHING.md#cut-a-release)):

- `site/index.html` — the `vX.Y.Z` chip in the developer section, the
  `PlaceRoot vX.Y.Z` footer byline, the "new in this release" tool names,
  and the tool grid + both "N tools" count claims.
- `site/add-to-your-ai.html` — the "latest release vX.Y.Z" note.
- Any capability copy that a new tool makes stale (e.g. how-it-works'
  "WHAT IT KNOWS" list).

Two offline guards enforce the mechanical parts, so a stale site fails CI
rather than shipping:
- `tests/test_site_version_sync.py` — every version string on the site equals
  `pyproject.toml`'s, `npm/package.json` matches it too, and the "new in this
  release" chip names real registered tools.
- `tests/test_site_tools_sync.py` — the tool grid and count claims match the
  tools registered in `server.py`.

## Serve locally
```bash
cd site && python3 -m http.server 8000
# open http://localhost:8000/
```
Any static file server works; there is nothing to build.

## Deploy — Cloudflare Pages (primary)
`.github/workflows/deploy-site.yml` direct-uploads `site/` on every push to
`main` that touches it (same pattern as the funradar/flood sibling repos —
`wrangler pages deploy`, not the Pages Git-integration build). Manual runs:
**Actions → Deploy Site → Run workflow**.

One-time setup (owner):
1. Create a Cloudflare **Pages project**; its name must match `PROJECT` in the
   workflow (default `placeroot`).
2. Repo secrets **`CLOUDFLARE_API_TOKEN`** (Pages:Edit) and
   **`CLOUDFLARE_ACCOUNT_ID`** (same names the sibling repos use).
3. Attach **placeroot.dev** as a custom domain on the Pages project.

## Deploy — GitHub Pages (alternative)
`.github/workflows/pages.yml` publishes `site/` to GitHub Pages. It is
manual-only (`workflow_dispatch`) so it never double-deploys alongside
Cloudflare. To use Pages as the primary instead, add a
`push: { branches: [main], paths: ['site/**'] }` trigger to it.

One-time setup: repo **Settings → Pages → Source: "GitHub Actions"**, then run
the workflow. It publishes at `https://<owner>.github.io/<repo>/` (or a Pages
custom domain).

## Editing
Styles are inline per the design; responsive behavior is in each page's
`<style>` block via `@media` rules at 980px and 720px that stack the fixed
grids (class hooks `pr-*`). Keep the desktop inline styles untouched and adjust
the media queries for mobile changes. Verify at a phone width before shipping.
