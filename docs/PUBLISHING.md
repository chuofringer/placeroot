# Publishing runbook (PyPI + npm)

How releases of the `placeroot` package are cut and published. The publish is
fully automated by `.github/workflows/release.yml`; the steps below are the
one-time setup and the per-release action.

Both registries use **trusted publishing** (OIDC) — no API tokens are stored
in the repo, and there is no 2FA prompt for CI.

## One-time setup (owner)

### PyPI (trusted publishing)
1. pypi.org → your account → **Publishing** → **Add a pending publisher**:
   - PyPI Project Name: `placeroot`
   - Owner: `chuofringer` · Repository: `placeroot`
   - Workflow name: `release.yml` · Environment: `pypi`
2. GitHub repo → **Settings → Environments** → create an environment named
   exactly **`pypi`** (no secret inside — trusted publishing uses the OIDC
   `id-token`).

### npm (trusted publishing)
1. npmjs.com → the `placeroot` package → **Settings → Trusted Publisher** →
   add GitHub Actions: repo `chuofringer/placeroot`, workflow `release.yml`,
   environment `npm`. (See <https://docs.npmjs.com/trusted-publishers>.)
   The environment is part of the publisher key — if the npmjs.com config
   omits it while the workflow's npm job declares `environment: npm`, the
   OIDC exchange fails and the publish is rejected.
2. GitHub repo → **Settings → Environments** → create an environment named
   exactly **`npm`** (same shape as `pypi`; required reviewers optional but
   recommended so a publish needs a human approval).
3. No `NPM_TOKEN` secret is needed. The npm job already has `id-token: write`,
   upgrades npm to ≥ 11.5.1, and publishes with `--provenance`.
   - History note: the earlier automation-token approach failed with `EOTP`
     (the account requires an OTP on publish, which CI can't supply). Trusted
     publishing removes tokens and OTP from the path entirely.
4. The npm job publishes from `npm/`, so the package's README must live there —
   npm ignores a README one directory up, and a package without one renders as
   `ERROR: No README data found!` on npmjs.com (issue #203). `npm/README.md` is
   **generated** from the root `README.md`:

   ```bash
   uv run python scripts/sync_npm_readme.py          # regenerate
   uv run python scripts/sync_npm_readme.py --check  # what CI runs
   ```

   Edit the root README (or the npm-only sections inside the script), never
   `npm/README.md` directly — `tests/test_npm_readme_sync.py` fails CI on drift.
   The README is baked into each published version's metadata, so a README fix
   only reaches npmjs.com when the next version publishes.

## Cut a release

1. **Actions → Prepare Release → Run workflow**, entering the new version
   (`0.6.0`, no leading `v`). It runs `scripts/bump_version.py`, which bumps
   every place the version is claimed — `pyproject.toml`, `npm/package.json`,
   `uv.lock`, and the site's version chip, footer byline and installer note —
   fills the site's "new in this release" chip with the tools added since the
   previous release tag, runs the full offline suite, and opens a PR.
   - Optional **highlight** input: free-text chip copy for a release whose
     headline isn't a new tool (e.g. `faster tile cache`). Leave blank to
     derive the tool names.
   - Locally instead, if you'd rather: `uv run python scripts/bump_version.py
     0.6.0 [--highlight "…"]` then `uv lock`. `--dry-run` shows the edits
     without making them.
2. Review and merge that PR. Merging deploys the site (`deploy-site.yml` on
   push to `main`), so the website updates as part of the release rather than
   after it.
3. GitHub → **Releases → Draft a new release** → tag `vX.Y.Z` on `main` →
   **Publish**. That fires `release.yml`:
   - `verify` job: refuses to publish unless the tag, `pyproject.toml` and
     `npm/package.json` all agree **and** the site sync guards pass. A release
     tagged without step 1 fails here instead of shipping a stale website.
   - `pypi` job: `uv build` → `pypa/gh-action-pypi-publish` (with
     `skip-existing: true`, so a re-run after a partial failure is a no-op
     rather than an error — this is what lets the npm job still run).
   - `npm` job: `npm publish --provenance` via trusted publishing.
   - Publishing also re-deploys the site from the tagged tree
     (`deploy-site.yml` on `release: published`), so production matches the
     release even if step 2's deploy failed or the merge didn't touch `site/`.
4. If the release event doesn't fire a run (it occasionally doesn't after a
   tag delete/recreate), trigger it manually: the workflow also has a
   `workflow_dispatch`, so run **Actions → Release → Run workflow** on `main`.
   (With no tag in the event, `verify` still cross-checks the two package
   versions and the site.)

If `verify` fails on a tag mismatch, don't hand-edit: run **Prepare Release**
for the right version, merge, then delete and re-create the tag.

## Regenerating the banner

`site/og-image.png` / `site/og-image-dark.png` are **generated** — never
hand-edit them. After changing the wording, palette or byline:

```bash
uv run --group browser python scripts/build_og_image.py
```

The npm README embeds the banner by release tag, so a banner change only
reaches npmjs.com with the next published version (same rule as the README
itself).

## Verify
- `uvx placeroot` resolves and starts the server.
- pypi.org/project/placeroot shows the new version, MIT license, and links.
- registry.npmjs.org/placeroot shows the version (name-claim placeholder that
  points users to `uvx placeroot`).

## Then
Registry listings and the launch post (drafts live outside the repo; see
issue #254) are safe to submit **only after** the package resolves on PyPI —
entries pointing at a 404 get rejected.

## Desktop Extension bundle (.mcpb, issue #233)

`site/placeroot.mcpb` is the one-click install for Claude Desktop's **Chat**
surface, served publicly from placeroot.dev (a stable URL that install
instructions can point at, independent of GitHub). It is a committed build
artifact:

```bash
uv run python scripts/build_mcpb.py site/placeroot.mcpb   # rebuild after a version bump
```

`tests/test_mcpb_bundle.py` fails when the bundle's manifest version doesn't
match `pyproject.toml`, so a release can't ship a stale bundle; the Prepare
Release workflow rebuilds it as part of the bump. The Release workflow also
rebuilds from the tagged tree and attaches `placeroot.mcpb` to the GitHub
release for repo users.

Honest limitation, repeated wherever the bundle is offered: the server type
is `uv`, and Claude Desktop does not bundle a Python/uv runtime — one-click
removes the config-file step, not the uv prerequisite.
