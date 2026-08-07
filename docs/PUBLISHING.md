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

1. Bump the version in **both** `pyproject.toml` and `npm/package.json` (keep
   them in sync) and land it on `main`.
2. GitHub → **Releases → Draft a new release** → tag `vX.Y.Z` on `main` →
   **Publish**. That fires `release.yml`:
   - `pypi` job: `uv build` → `pypa/gh-action-pypi-publish` (with
     `skip-existing: true`, so a re-run after a partial failure is a no-op
     rather than an error — this is what lets the npm job still run).
   - `npm` job: `npm publish --provenance` via trusted publishing.
3. If the release event doesn't fire a run (it occasionally doesn't after a
   tag delete/recreate), trigger it manually: the workflow also has a
   `workflow_dispatch`, so run **Actions → Release → Run workflow** on `main`.

## Verify
- `uvx placeroot` resolves and starts the server.
- pypi.org/project/placeroot shows the new version, MIT license, and links.
- registry.npmjs.org/placeroot shows the version (name-claim placeholder that
  points users to `uvx placeroot`).

## Then
Registry listings and the launch post (`docs/launch/`) are safe to submit
**only after** the package resolves on PyPI — entries pointing at a 404 get
rejected.
