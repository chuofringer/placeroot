# Bumping the pinned Overture release

`PINNED_RELEASE` (`src/placeroot/release.py`) is the release PlaceRoot falls
back to whenever live discovery fails, and the release every bundled
acceleration artifact is built for. The weekly
[Overture canary](../.github/workflows/overture-canary.yml) tells you when
it's due to move: it opens an issue titled "Overture canary: stale pin or
schema drift" whenever upstream's newest release has moved past the pin, or
a required column has vanished from the newest release's schema (see
`scripts/overture_canary.py`).

This page is the runbook for what to do about it. Nothing described below
is destructive to run, but most of it needs network access to the live
Overture bucket, so it isn't something CI can do unattended (GitHub Actions
in this repo also cannot open pull requests — an org setting — so any
automation here can open an issue, never a PR).

## 1. Bump the constant

```bash
uv run python scripts/bump_pin.py 2026-08-19.0
```

Rewrites `PINNED_RELEASE = "..."` in `src/placeroot/release.py` and nothing
else — see step 2 for why the rest isn't automated — then prints the
checklist below. `--dry-run` reports the change without writing it.

This one line is the only thing that changes PlaceRoot's *behavior*: it's
the release served when discovery fails (network blip, S3 listing change,
restricted egress) and, via `bundled_artifact_release()`, the release every
"is this build's acceleration current" check compares against.

## 2. What else moves, and why the sweep isn't automatic

Every other mention of the old release string in this repo falls into one
of three categories. Only the first needs to move *with* the pin; the other
two are why `scripts/bump_pin.py` doesn't attempt a blind find-and-replace
— that would silently rewrite history, not just fix it.

### a. Generated artifacts keyed by release name — regenerate, don't edit

Three bundled artifact sets accelerate cold queries, each keyed by release
and read independently by `release.bundled_artifact_release()`:

| Set | Path | Regenerate with |
|---|---|---|
| File manifests (bbox pruning) | `src/placeroot/data/manifests/<release>/` | `uv run python scripts/build_release_manifest.py` |
| Geocode index | `src/placeroot/data/geocode-index/<release>/` | `uv run python scripts/build_geocode_index.py` |
| Land-cover grid | `src/placeroot/data/land-cover-grid/<release>.parquet` | `uv run python scripts/build_land_cover_grid.py` |

Each writes a *new*, release-named directory or file — it does not rewrite
the old one in place. **Delete the superseded release's three sets in the
same PR** once the new ones are committed. Keeping them is functionally
harmless (`bundled_artifact_release()` takes the newest release present in
*all three* sets, so a partial regeneration is safely ignored rather than
silently claimed) but it doubles `site/placeroot.mcpb`, and Cloudflare
Pages refuses any file over 25 MiB — which fails the *site deploy*, not
the test suite or the release job, so it surfaces only after the release
is tagged. 0.9.8 shipped with the bundle at 42.8 MiB and the site stuck on
the previous version until the old artifacts were pruned;
`tests/test_mcpb_bundle.py::test_site_bundle_fits_the_pages_file_limit`
now catches it before merge. Run all three in the same PR as the pin bump:
`resolve_release()`'s `_resolved()` logic (see its docstring) prefers a
*current* artifact release over a newer bare pin, so doing only some of them
just means the deployment stays on the old release a while longer — annoying,
not broken, but it does mean the pin bump bought nothing until the set that
was skipped catches up.

Regenerating the manifests also touches one test that hardcodes the real
bundled data's release name and a file count:

```
tests/test_manifest.py::test_bundled_manifests_load_and_prune_for_real
```

Update its `"2026-07-22.0"` literals to the new release, and its
`len(m["files"]) == 512` / `kept <= 16` assertions if the new release's file
counts differ (Overture's own theme partitioning can change release to
release).

### b. Historical "measured live on `<release>`" comments — leave alone

`src/placeroot/geocode.py`, `addresses.py`, `water.py`, `recreation.py`, and
several tests carry comments like *"measured live on release 2026-07-22.0:
216s end-to-end"* or *"Live against release 2026-07-22.0, Lake Michigan is a
single polygon spanning..."*. These document a specific past measurement —
why a design decision was made, or what a spot-check found — not "the
currently pinned release". They should **not** be rewritten when the pin
moves; doing so would replace a true historical record with a false claim
about a measurement nobody re-ran. If a behavior they describe seems worth
re-verifying against the new release, that's a follow-up investigation, not
a text edit — file an issue rather than editing the comment in place.

The one place this category needs judgment rather than blind preservation:
`addresses.py`'s `COVERED_COUNTRIES` and `_TERRITORY_PARENT` comments claim
coverage "verified against release `2026-07-22.0`". Overture's addresses
theme is still alpha and coverage can change release to release — worth a
live spot-check after a pin bump, documented the same way (rewrite the
comment to name the release actually checked, keep the verification
command it already describes).

### c. Documentation snapshots — regenerate with their own tooling

A few docs/benchmark files report numbers captured against a specific live
release. Each already has its own regeneration path; the pin bump doesn't
add a new one, it just means these are now stale until re-run:

| File | What it reports | Regenerate with |
|---|---|---|
| `docs/benchmarks.md` | Token-efficiency numbers | `uv run python benchmarks/token_efficiency.py --write` |
| `benchmarks/results.md`, `benchmarks/competitors/placeroot_answers.json` | Query-corpus answers/latency | `uv run python benchmarks/run_query_corpus.py` |
| `docs/benchmarks-vs.md` | Competitor comparison snapshot | `uv run python benchmarks/competitor_comparison.py` |
| `docs/RECREATION.md`, `README.md` (recreation-layer stat) | Places-vs-base theme coverage counts for one city box | Hand-recapture per `docs/RECREATION.md`'s own method note; no script yet |
| `docs/MIRROR.md` (dry-run sample output) | Illustrative only — the page already says "always re-run `--dry-run` rather than trusting the numbers above" | No action needed |

None of these gate correctness — the code doesn't read them — so they're a
"catch up when convenient" sweep, not a blocker for shipping the pin bump
itself. `git grep 2026-07-22.0` (substituting the old release) after a bump
finds every remaining mention if you want the full list at bump time.

### Test literals unrelated to the pin — no action needed

`tests/test_cache.py`, `tests/test_mirror_theme.py`, and
`tests/test_security.py` each declare a local `RELEASE = "2026-07-22.0"`
constant. These are arbitrary example release strings used to build
synthetic fixture paths — they exercise cache/mirror/security logic that
works identically for any release-shaped string, and are not references to
the real pin. They don't need to move when the pin does.

## 3. Verify

```bash
uv run ruff check .
uv run pytest -q -m "not live"
uv run pytest -q -m live          # network required; also re-runs the canary's checks for real
```

Then open the PR with whichever of step 2's regenerations you completed —
the artifact regeneration (2a) is the one that matters for users; the rest
can follow in a fast-follow PR without blocking the pin bump itself.
