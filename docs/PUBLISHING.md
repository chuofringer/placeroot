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
   add GitHub Actions: repo `chuofringer/placeroot`, workflow `release.yml`.
   (See <https://docs.npmjs.com/trusted-publishers>.)
2. No `NPM_TOKEN` secret is needed. The npm job already has `id-token: write`,
   upgrades npm to ≥ 11.5.1, and publishes with `--provenance`.
   - History note: the earlier automation-token approach failed with `EOTP`
     (the account requires an OTP on publish, which CI can't supply). Trusted
     publishing removes tokens and OTP from the path entirely.

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

## Verify
- `uvx placeroot` resolves and starts the server.
- pypi.org/project/placeroot shows the new version, MIT license, and links.
- registry.npmjs.org/placeroot shows the version (name-claim placeholder that
  points users to `uvx placeroot`).

## Then
Registry listings and the launch post (`docs/launch/`) are safe to submit
**only after** the package resolves on PyPI — entries pointing at a 404 get
rejected.
