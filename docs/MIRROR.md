# Mirroring the Overture places theme (#20)

**Owner's runbook.** This is a mechanism, not a default: PlaceRoot queries
the public Overture bucket directly out of the box, no setup required. Read
this only if you want to run your own mirror.

## Why

CONTRIBUTING.md design rule #3 is that nothing on the critical path may
depend on an API key, another project's roadmap, or someone else's rate
limit. Right now that
rule has one real gap: every query ultimately depends on
`s3://overturemaps-us-west-2` staying reachable, in the same region, with
the same `theme=<x>/type=<y>/*` layout it has today. Overture changing that
layout, moving regions, or having a bad day is out of our control and, as
things stand, would turn into an outage for every PlaceRoot deployment at
once.

Mirroring the places theme to storage we control turns that failure mode
into an inconvenience instead: if upstream changes shape or goes down, flip
one environment variable and keep answering queries from the mirror. This
doesn't replace Overture as the source of truth — the mirror is refreshed
from Overture, not the other way around — it just removes Overture's
availability from PlaceRoot's own critical path.

This runbook walks through the places theme; `scripts/mirror_theme.py`
takes `--theme`/`--type` so the same mechanism covers divisions, addresses,
buildings, or transportation without new tooling.

## What you need (your side)

An S3-compatible bucket you control — Cloudflare R2, MinIO, or plain AWS S3
all work, since everything here goes through DuckDB's `httpfs` extension.
R2 is the reference target below because it has no egress fees, which
matters once a mirror is actually being served from: **R2 setup is entirely
your call** — this repo doesn't provision or manage the bucket, it just
reads from and writes to whatever `s3://...` URL and credentials you give it.

Cloudflare R2, roughly:

1. Create a bucket (R2 dashboard, or `wrangler r2 bucket create <name>`).
2. Create an R2 API token with read+write access to that bucket
   (R2 → Manage API Tokens). Note the Access Key ID, Secret Access Key, and
   your account's S3 API endpoint — it looks like
   `https://<account-id>.r2.cloudflarestorage.com`.
3. Export credentials for the mirror script (write access) and, if you're
   pointing a PlaceRoot server at a *private* mirror, for reads too — see
   "Flip the switch" below:

   ```bash
   export PLACEROOT_S3_ACCESS_KEY_ID=<your R2 access key id>
   export PLACEROOT_S3_SECRET_ACCESS_KEY=<your R2 secret access key>
   export PLACEROOT_S3_ENDPOINT=<account-id>.r2.cloudflarestorage.com
   export PLACEROOT_S3_REGION=auto   # R2's convention; DuckDB requires something here
   ```

Any other S3-compatible target (plain AWS S3, MinIO, ...) works the same
way — set `PLACEROOT_S3_ENDPOINT` for anything that isn't AWS S3 itself, and
`PLACEROOT_S3_REGION` to whatever your provider expects.

## 1. Dry run first

The places theme is tens of gigabytes. Always run `--dry-run` before
spending time or egress on a real mirror — it lists every source file and
the total size, and touches only the (public, anonymous, free-to-list)
Overture bucket:

```bash
uv run python scripts/mirror_theme.py --dry-run --target s3://my-bucket/overture
```

Sample output — a real dry run against a live release
(`2026-07-22.0`, `places`/`place`):

```
part-00000-721bdadd-4327-5b81-bc83-aa244c71ceaa-c000.zstd.parquet     642712932
part-00001-1ca5badf-9d79-5942-9102-67a0db79e5ff-c000.zstd.parquet     943424032
part-00002-845ec8ef-52e8-5eca-87c2-05310205b0da-c000.zstd.parquet     937741772
...

TOTAL   16 files   11246851752 bytes (10.5GB)
```

Counts and sizes vary release to release — always re-run `--dry-run` rather
than trusting the numbers above.

## 2. Mirror

```bash
uv run python scripts/mirror_theme.py --target s3://my-bucket/overture
```

This copies every source file for `--theme places --type place` (both are
the defaults; pass `--release` to pin a specific one instead of the
auto-resolved current release) into
`s3://my-bucket/overture/<release>/theme=places/type=place/...` — the exact
layout `PLACEROOT_UPSTREAM_BASE` expects underneath it (see "Flip the
switch"). Progress logs one line per file as it copies. The run is
resumable: interrupt it (network blip, laptop closed) and re-run the same
command — files already mirrored are skipped, so you're only ever paying
for what's left. A manifest recording what was copied (source size, the
size actually written, row count) is kept alongside the target for a local
target, or under `~/.cache/placeroot/mirror-manifests/` for an S3 target —
`--verify` (next) reads it back.

To mirror a different theme/type (e.g. once buildings/transportation
matter too):

```bash
uv run python scripts/mirror_theme.py --theme buildings --type building --target s3://my-bucket/overture
```

## 3. Verify

```bash
uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --verify
```

Checks every file the manifest says was mirrored: the source size hasn't
silently changed since, the target file is present with the expected size,
and — the check that actually catches a corrupted or truncated file, since
size alone can coincidentally match — the target's row count still matches
what was recorded when it was written. Exits non-zero and logs every
problem found if anything's off; exits 0 and logs a one-line "OK" summary
otherwise. Safe to run any time, as often as you like — it never touches
the source or target's contents, only reads them.

## 4. Flip the switch

Point a PlaceRoot server at the mirror instead of the public Overture
bucket:

```bash
export PLACEROOT_UPSTREAM_BASE=s3://my-bucket/overture
# Only needed if the mirror bucket isn't plain AWS S3, or is private:
export PLACEROOT_S3_ENDPOINT=<account-id>.r2.cloudflarestorage.com
export PLACEROOT_S3_REGION=auto
export PLACEROOT_S3_ACCESS_KEY_ID=<read-capable key>
export PLACEROOT_S3_SECRET_ACCESS_KEY=<read-capable secret>

uv run placeroot
```

`PLACEROOT_UPSTREAM_BASE` replaces just the bucket root
(`s3://overturemaps-us-west-2/release`, by default) — every tool still
builds the same `<base>/<release>/theme=<theme>/type=<type>/*` path
underneath it, so this is a one-variable switch, not a per-theme
reconfiguration. It composes with the existing
`PLACEROOT_DATA_PATH[_<THEME>]` overrides and with release pinning
(`PLACEROOT_OVERTURE_RELEASE`) exactly as before; nothing else about how a
tool query gets built changes.

To go back to the public bucket, unset `PLACEROOT_UPSTREAM_BASE` (and the
`PLACEROOT_S3_*` variables, if set) and restart.

**A public mirror bucket needs no `PLACEROOT_S3_*` variables at all** —
`PLACEROOT_UPSTREAM_BASE` alone is enough, the same way the default public
Overture bucket needs none today. Those variables exist for a private
target or a non-AWS endpoint.

## Keeping the mirror current

Overture ships a new release roughly monthly. This script mirrors one
release at a time (`--release`, default the currently-resolved one) — there
is no scheduled sync built in. Re-run step 2 with the new release once one
ships (`overture.resolve_release()` / the server's own release discovery
will pick it up the same way it picks up new upstream releases today); old
releases already mirrored are untouched unless you clean them up yourself.

### `--check-current`: is the mirror behind upstream?

```bash
uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --check-current
```

Compares the newest release found under `--target` (a plain listing of
`<target>/<release>/...` — local directory listing, or DuckDB's `glob()`
against the same credentialed connection the mirror was written through,
so a private target needs no separate auth path) against
`placeroot.release.resolve_release()` — the same discovery the server and
this script's own default `--release` use. Exits `0` and logs one line when
the mirror is current (or ahead — a manually seeded release upstream
discovery hasn't caught up to isn't treated as a problem); exits `1` with
`mirror holds X, upstream is at Y` when it's behind, or when the mirror
holds no releases at all (never mirrored, or `--target`/`--s3-*` point at
the wrong place). Pass `--release` to compare against a specific release
instead of upstream's newest. No files are copied or listed from
`--source` — this only touches `--target`.

### `--prune-releases`: drop superseded releases

```bash
# Dry run (default): prints what WOULD be deleted, deletes nothing.
uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --prune-releases

# Actually delete.
uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --prune-releases --yes
```

Deletes every mirrored release **older than the newest one the mirror
holds** — a whole release directory (every theme/type mirrored under it),
not just the theme/type `--theme`/`--type` would otherwise default to,
since a superseded release is superseded across every theme at once.
Dry-run by default; deleting mirrored data never happens on a bare flag,
only with an explicit `--yes`.

For a **local** `--target`, `--yes` deletes the release directories
directly. For an **S3** `--target`, `--prune-releases` always lists what it
found and would delete, but never deletes in-process, even with
`--yes` — DuckDB's `httpfs` has no S3 delete primitive (it's how every
other command here reads and writes objects, but object deletion isn't
part of that surface), and this script deliberately doesn't hand-roll
signed S3 delete requests to work around that. With `--yes` against an S3
target it logs the exact prefix to remove and exits `2`; run the printed
command with your own tooling, e.g.:

```bash
aws s3 rm --recursive s3://my-bucket/overture/2026-06-22.0/
# or, for R2 / other S3-compatible endpoints:
rclone purge r2:my-bucket/overture/2026-06-22.0/
```

### Scheduled refresh

Neither flag runs anything on a schedule by itself — that's the operator's
infra, not this repo's (mirroring writes to a bucket you own; this repo has
no standing credentials to run anything against it). Wire the three steps
(mirror the current release, verify, prune old ones) into whatever
scheduler you already run.

**cron**, roughly monthly (Overture's own cadence), on a host with the
target's S3 write credentials exported:

```cron
# Refresh the places mirror every Monday at 03:00; --release defaults to
# the currently-resolved upstream release, so this always pulls forward.
0 3 * * 1 cd /path/to/placeroot && \
  uv run python scripts/mirror_theme.py --target s3://my-bucket/overture && \
  uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --verify && \
  uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --prune-releases --yes \
  >> /var/log/placeroot-mirror.log 2>&1
```

**GitHub Actions**, for an operator who wants this to run in their own
repo/infra (this is documentation only — it is *not* a workflow committed
to this repo, since a mirror belongs to whoever owns the target bucket, not
to PlaceRoot's own CI):

```yaml
name: refresh-overture-mirror
on:
  schedule:
    - cron: "0 3 * * 1"  # weekly; Overture ships roughly monthly
  workflow_dispatch: {}

jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          repository: chuofringer/placeroot
      - uses: astral-sh/setup-uv@v3
      - name: Mirror the current release
        env:
          PLACEROOT_S3_ACCESS_KEY_ID: ${{ secrets.MIRROR_S3_ACCESS_KEY_ID }}
          PLACEROOT_S3_SECRET_ACCESS_KEY: ${{ secrets.MIRROR_S3_SECRET_ACCESS_KEY }}
          PLACEROOT_S3_ENDPOINT: ${{ secrets.MIRROR_S3_ENDPOINT }}
          PLACEROOT_S3_REGION: auto
        run: |
          uv run python scripts/mirror_theme.py --target s3://my-bucket/overture
          uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --verify
      - name: Prune superseded releases
        env:
          PLACEROOT_S3_ACCESS_KEY_ID: ${{ secrets.MIRROR_S3_ACCESS_KEY_ID }}
          PLACEROOT_S3_SECRET_ACCESS_KEY: ${{ secrets.MIRROR_S3_SECRET_ACCESS_KEY }}
          PLACEROOT_S3_ENDPOINT: ${{ secrets.MIRROR_S3_ENDPOINT }}
          PLACEROOT_S3_REGION: auto
        run: |
          # --prune-releases never deletes an S3 target in-process (see
          # above) — this step only reports what to remove; wire in your
          # own `aws s3 rm` / `rclone purge` step if you want it automated.
          uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --prune-releases --yes
      - name: Fail if the mirror ended up behind
        env:
          PLACEROOT_S3_ACCESS_KEY_ID: ${{ secrets.MIRROR_S3_ACCESS_KEY_ID }}
          PLACEROOT_S3_SECRET_ACCESS_KEY: ${{ secrets.MIRROR_S3_SECRET_ACCESS_KEY }}
          PLACEROOT_S3_ENDPOINT: ${{ secrets.MIRROR_S3_ENDPOINT }}
          PLACEROOT_S3_REGION: auto
        run: |
          uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --check-current
```

## What this doesn't do

- It doesn't make the mirror the source of truth — Overture is, always.
  This is a read replica for availability, not a fork.
- It doesn't byte-for-byte replicate source files. Every file is read
  through DuckDB and re-written as Parquet, which can produce a
  different-sized (same-data) file — `--verify`'s row-count check is what
  actually proves correctness, not a byte comparison against the source.
- It doesn't touch bucket creation, lifecycle policy, cost, or access
  control on the target — that's the owner's infrastructure, not something
  this repo manages.
