#!/usr/bin/env python3
"""Mirrors one theme+type of an Overture release to our own storage (#20).

Owns the tooling side of "no hard dependency on anyone else" (ROADMAP.md
rule #3) for the Overture bucket itself: an upstream layout change, region
outage, or listing-format change should be an inconvenience (re-point
PLACEROOT_UPSTREAM_BASE at a mirror), not an outage. The bucket/account this
writes to is the operator's call — see docs/MIRROR.md for the runbook
(creating a target bucket, running this script, flipping the switch).

Usage:

    # Always run this first — places is 10s of GB; know the size before
    # paying for it.
    uv run python scripts/mirror_theme.py --dry-run --target s3://my-bucket/overture

    uv run python scripts/mirror_theme.py --target s3://my-bucket/overture
    uv run python scripts/mirror_theme.py --target s3://my-bucket/overture --verify
    uv run python scripts/mirror_theme.py --target /local/mirror   # local dir works too

Design (see docs/MIRROR.md for the full rationale):

- Source enumeration reuses the same anonymous S3 ListObjectsV2 HTTPS
  listing release.py already uses for release discovery, generalized to a
  caller-supplied prefix and paginated (the places theme is far more than
  one listing page). A local directory works as --source too (plain
  filesystem walk) — that's what makes the offline test suite possible.
- Every file is copied through DuckDB's read_parquet -> COPY TO, which works
  uniformly whether source/target is a local path or an s3:// URL (via
  httpfs). This re-encodes rather than byte-copying, so a target file's size
  generally won't equal the source's raw S3-reported size — resumability and
  --verify are therefore built around a manifest this script writes itself
  (source size seen + row count + the size *we* wrote), not a byte-for-byte
  comparison against the source. That manifest is local operator state (see
  --manifest) and isn't required to exist on the target.
- Every copy is staged through a local temp file first (even for a local
  target) so the manifest always has an exact byte count for what actually
  landed, whether the final destination is a local disk or an S3-compatible
  bucket whose object size isn't cheaply queryable over plain httpfs reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import duckdb

from placeroot import release as release_mod

logger = logging.getLogger("mirror_theme")

DEFAULT_SOURCE_BASE = "s3://overturemaps-us-west-2/release"
DEFAULT_SOURCE_REGION = "us-west-2"
MANIFEST_NAME = ".mirror_manifest.json"
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


@dataclass(frozen=True)
class RemoteFile:
    """One parquet file under a theme/type root."""

    key: str  # path relative to the theme/type root, e.g. "part-0001.parquet"
    size: int  # bytes, as reported by the source listing
    url: str  # full readable path/URL: s3://... or a local filesystem path


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if unit == "B":
            if n < 1024:
                return f"{int(n)}B"
        elif n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"  # pragma: no cover - unreachable, loop always returns


def theme_root(base: str, release: str, theme: str, type_: str) -> str:
    """<base>/<release>/theme=<theme>/type=<type_> — the same layout
    Overture's own bucket uses, and what overture._upstream_glob expects a
    PLACEROOT_UPSTREAM_BASE mirror to preserve. Works whether base is
    s3://... or a local directory."""
    return f"{base.rstrip('/')}/{release}/theme={theme}/type={type_}"


def is_s3(path: str) -> bool:
    return path.startswith("s3://")


def parse_s3(url: str) -> tuple[str, str]:
    rest = url.removeprefix("s3://")
    bucket, _, key = rest.partition("/")
    return bucket, key


def _list_s3(bucket: str, prefix: str, region: str = DEFAULT_SOURCE_REGION) -> list[RemoteFile]:
    """Paginated, anonymous ListObjectsV2 over HTTPS for a public bucket —
    the same mechanism release.py uses for release discovery, generalized
    with pagination (a theme's file count routinely exceeds one listing
    page) and a caller-supplied prefix. Only ever used for --source: a
    mirror target is written to via DuckDB/httpfs (see copy_one), not listed
    this way, since a private target bucket wouldn't answer an anonymous
    GET anyway.
    """
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    host = f"{bucket}.s3.{region}.amazonaws.com"
    files: list[RemoteFile] = []
    token: str | None = None
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        url = f"https://{host}/?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            xml_text = resp.read().decode("utf-8")
        root = ElementTree.fromstring(xml_text)
        for c in root.findall("s3:Contents", _S3_NS):
            key = c.findtext("s3:Key", namespaces=_S3_NS) or ""
            size = int(c.findtext("s3:Size", default="0", namespaces=_S3_NS) or "0")
            rel = key[len(prefix) :] if key.startswith(prefix) else key
            files.append(RemoteFile(key=rel, size=size, url=f"s3://{bucket}/{key}"))
        truncated = (root.findtext("s3:IsTruncated", namespaces=_S3_NS) or "").lower() == "true"
        token = root.findtext("s3:NextContinuationToken", namespaces=_S3_NS)
        if not truncated or not token:
            break
    return files


def _list_local(root: str) -> list[RemoteFile]:
    p = Path(root)
    if not p.exists():
        return []
    return [
        RemoteFile(key=str(f.relative_to(p)), size=f.stat().st_size, url=str(f))
        for f in sorted(p.rglob("*.parquet"))
    ]


def list_source_files(
    source_base: str, release: str, theme: str, type_: str, region: str = DEFAULT_SOURCE_REGION
) -> list[RemoteFile]:
    """Every parquet file (with size) under source_base's theme/type root.

    source_base may be s3://... (real listing, paginated) or a local
    directory (filesystem walk — what the offline test suite uses instead
    of hitting the network).
    """
    root = theme_root(source_base, release, theme, type_)
    if is_s3(root):
        bucket, prefix = parse_s3(root)
        return _list_s3(bucket, prefix, region)
    return _list_local(root)


# --- Connection ---------------------------------------------------------


def _sql_str(value: str) -> str:
    """A single-quoted SQL string literal with embedded single quotes escaped.

    DuckDB's `SET <opt> = '...'` takes a literal, not a bind parameter, so a
    region/endpoint/credential interpolated into one must have any single
    quote doubled — otherwise a value containing one breaks the statement or
    injects SQL. Mirrors placeroot.db._sql_str (this script deliberately
    doesn't import from the package's connection module — see
    configure_connection's own note).
    """
    return "'" + value.replace("'", "''") + "'"


def configure_connection(endpoint: str | None, region: str) -> duckdb.DuckDBPyConnection:
    """A DuckDB connection configured for both anonymous reads (the public
    Overture source) and, if endpoint/credentials are set, writes to a
    private mirror target. Deliberately separate from overture._configure:
    that one always defaults to anonymous unless PLACEROOT_S3_ENDPOINT is
    set, but a mirror target can be plain AWS S3 (no custom endpoint) that
    still needs real write credentials.
    """
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET enable_progress_bar=false;")
    con.execute(f"SET s3_region={_sql_str(region)};")
    if endpoint:
        con.execute(f"SET s3_endpoint={_sql_str(endpoint)};")
        con.execute("SET s3_url_style='path';")
    access_key = os.environ.get(
        "PLACEROOT_S3_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", "")
    )
    secret_key = os.environ.get(
        "PLACEROOT_S3_SECRET_ACCESS_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    )
    con.execute(f"SET s3_access_key_id={_sql_str(access_key)};")
    con.execute(f"SET s3_secret_access_key={_sql_str(secret_key)};")
    con.execute("SET http_timeout=30000;")
    con.execute("SET http_retries=3;")
    con.execute("SET http_retry_wait_ms=500;")
    return con


# --- Manifest -------------------------------------------------------------


def default_manifest_path(target: str) -> Path:
    """<target>/.mirror_manifest.json for a local target; a stable path
    under ~/.cache/placeroot for an S3 target, keyed by a hash of the target
    URL (an S3 object listing can't cheaply answer "is this manifest here"
    without credentials wired up for listing specifically, so this script
    keeps its own bookkeeping locally rather than depending on that).
    """
    if not is_s3(target):
        return Path(target) / MANIFEST_NAME
    digest = hashlib.sha256(target.encode()).hexdigest()[:16]
    return Path(os.path.expanduser("~/.cache/placeroot/mirror-manifests")) / f"{digest}.json"


def load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"release": None, "theme": None, "type": None, "files": {}}


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    tmp.replace(path)


# --- Copy -------------------------------------------------------------


def copy_one(
    con: duckdb.DuckDBPyConnection, source_url: str, target_url: str, stage_dir: Path
) -> tuple[int, int]:
    """Copies one file source_url -> target_url via a local staging file.

    Returns (row_count, bytes_written). Raises duckdb.Error if the source
    can't be read (corrupt/truncated/unreachable) — callers should let that
    propagate; a mirror run that silently skips a bad source file is worse
    than one that stops and says so.
    """
    stage_file = stage_dir / "stage.parquet"
    con.execute(
        f"COPY (SELECT * FROM read_parquet('{source_url}')) TO '{stage_file}' (FORMAT PARQUET)"
    )
    size = stage_file.stat().st_size
    row_count = con.execute(f"SELECT count(*) FROM read_parquet('{stage_file}')").fetchone()[0]
    if is_s3(target_url):
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{stage_file}')) TO '{target_url}' (FORMAT PARQUET)"
        )
    else:
        dest = Path(target_url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage_file), str(dest))
    if stage_file.exists():
        stage_file.unlink()
    return row_count, size


# --- Commands -------------------------------------------------------------


def cmd_dry_run(source_files: list[RemoteFile]) -> int:
    total_bytes = sum(f.size for f in source_files)
    for f in source_files:
        print(f"{f.key}\t{f.size}")
    print(f"\nTOTAL\t{len(source_files)} files\t{total_bytes} bytes ({human_bytes(total_bytes)})")
    return 0


def cmd_mirror(
    con: duckdb.DuckDBPyConnection,
    source_files: list[RemoteFile],
    target: str,
    release: str,
    theme: str,
    type_: str,
    manifest_path: Path,
) -> int:
    manifest = load_manifest(manifest_path)
    manifest.setdefault("files", {})
    target_root = theme_root(target, release, theme, type_)

    copied = skipped = 0
    total = len(source_files)
    with tempfile.TemporaryDirectory(prefix="placeroot-mirror-") as stage_dir_s:
        stage_dir = Path(stage_dir_s)
        for i, f in enumerate(source_files, 1):
            entry = manifest["files"].get(f.key)
            target_local = None if is_s3(target_root) else Path(target_root) / f.key
            already_done = (
                entry is not None
                and entry.get("source_size") == f.size
                and (
                    target_local is None
                    or (
                        target_local.exists()
                        and target_local.stat().st_size == entry.get("target_size")
                    )
                )
            )
            if already_done:
                skipped += 1
                logger.debug("[%d/%d] skip %s (already mirrored)", i, total, f.key)
                continue

            target_dest = f"{target_root}/{f.key}" if is_s3(target_root) else str(target_local)
            row_count, size = copy_one(con, f.url, target_dest, stage_dir)
            manifest["files"][f.key] = {
                "source_size": f.size,
                "target_size": size,
                "row_count": row_count,
                "mirrored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            manifest["release"], manifest["theme"], manifest["type"] = release, theme, type_
            save_manifest(manifest_path, manifest)
            copied += 1
            logger.info(
                "[%d/%d] copied %s (%s, %d rows)", i, total, f.key, human_bytes(size), row_count
            )

    logger.info(
        "done: %d copied, %d skipped, %d total. manifest: %s", copied, skipped, total, manifest_path
    )
    return 0


def cmd_verify(
    con: duckdb.DuckDBPyConnection,
    source_files: list[RemoteFile],
    target: str,
    release: str,
    theme: str,
    type_: str,
    manifest_path: Path,
) -> int:
    manifest = load_manifest(manifest_path)
    files = manifest.get("files", {})
    if not files:
        logger.error(
            "verify FAILED: no manifest entries at %s (mirror hasn't run yet?)", manifest_path
        )
        return 1

    source_by_key = {f.key: f for f in source_files}
    target_root = theme_root(target, release, theme, type_)
    problems: list[str] = []
    checked = 0

    for key, entry in sorted(files.items()):
        checked += 1
        src = source_by_key.get(key)
        if src is not None and src.size != entry["source_size"]:
            problems.append(
                f"{key}: source size changed since mirror ({entry['source_size']} -> {src.size})"
            )

        target_url = (
            f"{target_root}/{key}" if is_s3(target_root) else str(Path(target_root) / key)
        )
        if not is_s3(target_root):
            p = Path(target_url)
            if not p.exists():
                problems.append(f"{key}: missing from target")
                continue
            actual_size = p.stat().st_size
            if actual_size != entry["target_size"]:
                expected = entry["target_size"]
                problems.append(
                    f"{key}: target size mismatch (expected {expected}, found {actual_size})"
                )

        try:
            row_count = con.execute(
                f"SELECT count(*) FROM read_parquet('{target_url}')"
            ).fetchone()[0]
        except duckdb.Error as e:
            problems.append(f"{key}: target file unreadable/corrupted ({e})")
            continue
        if row_count != entry["row_count"]:
            problems.append(
                f"{key}: row count mismatch (expected {entry['row_count']}, found {row_count})"
            )

    missing_from_manifest = sorted(set(source_by_key) - set(files))
    for key in missing_from_manifest:
        problems.append(f"{key}: in source but never mirrored")

    if problems:
        logger.error(
            "verify FAILED: %d problem(s) across %d manifest entries", len(problems), checked
        )
        for p in problems:
            logger.error("  %s", p)
        return 1

    logger.info("verify OK: %d files match (%d in source)", checked, len(source_by_key))
    return 0


# --- CLI -------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--release", default=None,
        help="Overture release (default: resolved — env override, discovery, or the pin)",
    )
    p.add_argument("--theme", default="places")
    p.add_argument("--type", dest="type_", default="place")
    p.add_argument(
        "--source", default=DEFAULT_SOURCE_BASE,
        help=f"Base to mirror FROM (default: {DEFAULT_SOURCE_BASE}). A local directory also works.",
    )
    p.add_argument(
        "--target", default=None,
        help="Base to mirror TO: a local directory, or an s3://bucket/prefix URL",
    )
    p.add_argument(
        "--verify", action="store_true",
        help="Check an existing mirror against its manifest instead of copying",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="List source files and total size; copies nothing, needs no --target",
    )
    p.add_argument(
        "--manifest", default=None,
        help="Manifest path (default: <target>/.mirror_manifest.json, or a "
        "~/.cache/placeroot path keyed by target for S3)",
    )
    p.add_argument(
        "--s3-endpoint", default=os.environ.get("PLACEROOT_S3_ENDPOINT"),
        help="Custom S3-compatible endpoint for the TARGET (R2/minio/...); "
        "also settable via PLACEROOT_S3_ENDPOINT",
    )
    p.add_argument(
        "--s3-region", default=os.environ.get("PLACEROOT_S3_REGION", DEFAULT_SOURCE_REGION),
        help="Region for the TARGET connection",
    )
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    release = args.release or release_mod.resolve_release()
    logger.info(
        "release=%s theme=%s type=%s source=%s", release, args.theme, args.type_, args.source
    )

    source_files = list_source_files(args.source, release, args.theme, args.type_)
    total_bytes = sum(f.size for f in source_files)
    logger.info("source: %d files, %s total", len(source_files), human_bytes(total_bytes))

    if args.dry_run:
        return cmd_dry_run(source_files)

    if not args.target:
        logger.error("--target is required unless --dry-run")
        return 2

    manifest_path = Path(args.manifest) if args.manifest else default_manifest_path(args.target)
    con = configure_connection(args.s3_endpoint, args.s3_region)

    if args.verify:
        return cmd_verify(
            con, source_files, args.target, release, args.theme, args.type_, manifest_path
        )
    return cmd_mirror(
        con, source_files, args.target, release, args.theme, args.type_, manifest_path
    )


if __name__ == "__main__":
    sys.exit(main())
