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
**Automated** — run **Actions → Prepare Release** (step 1 of
[PUBLISHING.md](PUBLISHING.md#cut-a-release)) and merge the PR it opens. That
covers the version-linked copy:

- `site/index.html` — the `vX.Y.Z` chip in the developer section, the
  `PlaceRoot vX.Y.Z` footer byline, and the "new in this release" chip
  (auto-filled with the tools added since the previous release tag, or with
  the workflow's free-text `highlight` input).
- `site/add-to-your-ai.html` — the "latest release vX.Y.Z" note.

Still yours to write when a release calls for it:
- The tool grid + both "N tools" count claims, when tools were added or
  renamed (guarded, so CI tells you).
- Capability copy a new tool makes stale — e.g. how-it-works' "WHAT IT KNOWS"
  list, or the use-case cards.

Three layers keep this honest, so a stale site fails loudly rather than
shipping:
- `tests/test_site_version_sync.py` — every version string on the site equals
  `pyproject.toml`'s, `npm/package.json` matches it too, and any tool named in
  the "new in this release" chip is really registered.
- `tests/test_site_tools_sync.py` — the tool grid and count claims match the
  tools registered in `server.py`.
- `release.yml`'s `verify` job — re-runs both guards against the tagged tree
  and refuses to publish to PyPI/npm if the site doesn't match the release.

`scripts/bump_version.py` is the bumper the workflow drives; run it directly
(`--dry-run` to preview) if you'd rather prepare a release locally. Its
rewrite helpers are tested in `tests/test_bump_version.py` — if the site's
chip or version markup is ever restyled, update the regexes there and in the
guard together.

## Serve locally
```bash
cd site && python3 -m http.server 8000
# open http://localhost:8000/
```
Any static file server works; there is nothing to build.

## Deploy — Cloudflare Pages (primary)
`.github/workflows/deploy-site.yml` direct-uploads `site/` on every push to
`main` that touches it (direct `wrangler pages deploy`, not the Pages
Git-integration build). Manual runs:
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
