"""Bundled per-release file-extent manifests: bbox scans without the footer pass.

The cold cost of a theme is one parquet-footer read per file — DuckDB must
read every file's metadata before it can prune a single row group, and the
big themes span hundreds of files (buildings: 512, ~25s at 64 threads,
measured). But a release is immutable, and Overture's files are spatially
clustered, so each file's overall bbox extent is a fixed, tiny fact. The
manifests under data/manifests/<release>/ record exactly that (built by
scripts/build_release_manifest.py at release-pin time, a few tens of KB per
theme), and this module turns a (glob, bbox) pair into an explicit list of
just the intersecting files — typically 1-4 — so a truly cold query's scan
reads a handful of footers instead of all of them. As a bonus the explicit
list also skips the per-query S3 LIST the glob would do.

Strictly an optimization, never a correctness dependency:

- Only applies to globs in the standard <base>/<release>/theme=X/type=Y/*
  layout whose release has a bundled manifest. Pinned local datasets,
  mirrors on a different layout, and releases newer than the wheel fall
  back to the plain glob (return None).
- File pruning is conservative: intersection on the file's recorded extent,
  a superset of any row-group or row the query could match; files without
  bbox statistics are recorded with a world extent and never pruned.
- A bbox that runs past [-180, 180] (antimeridian) falls back to the glob
  rather than mis-pruning the wrapped side — same seam rule as
  geo.bbox_prune_literal_sql.
- Zero intersecting files also falls back to the glob: "the manifest says
  no file can match" and "the scan of the real dataset returns no rows"
  are the same answer, and letting the scan say it keeps the manifest out
  of the correctness story entirely.
"""

import json
import logging
import re
from functools import lru_cache
from importlib import resources

logger = logging.getLogger(__name__)

# <base>/<release>/theme=<theme>/type=<type>/* — only the public bucket
# (_DEFAULT_BASE) is eligible: a mirror may be a partial copy, and an
# explicit file list must never 404 where the glob would have succeeded.
# Kept equal to overture.DEFAULT_UPSTREAM_BASE (asserted by a test);
# defined locally so this module stays import-cycle-free.
_DEFAULT_BASE = "s3://overturemaps-us-west-2/release"

_GLOB_RE = re.compile(
    r"^(?P<base>(?:s3|https?)://.+)/(?P<release>[^/]+)/"
    r"theme=(?P<theme>[^/]+)/type=(?P<type>[^/]+)/\*$"
)


@lru_cache(maxsize=32)
def _load(release: str, theme: str, type_: str) -> dict | None:
    """The bundled manifest dict for (release, theme, type), or None."""
    name = f"{theme}__{type_}.json"
    try:
        path = resources.files("placeroot") / "data" / "manifests" / release / name
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None


def pruned_source_sql(
    upstream_glob: str, bbox: tuple[float, float, float, float] | None
) -> str | None:
    """read_parquet([...intersecting files...]) SQL for glob+bbox, or None.

    None means "no manifest applies — use the glob"; it is never an error.
    """
    if bbox is None:
        return None
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    if not (xmin >= -180.0 and xmax <= 180.0):
        return None  # antimeridian: fall back rather than mis-prune
    m = _GLOB_RE.match(upstream_glob)
    if m is None:
        return None
    if m.group("base") != _DEFAULT_BASE:
        # A mirror (PLACEROOT_UPSTREAM_BASE) may host only a regional
        # subset of the release; an explicit file list would 404 on files
        # the mirror lacks where the glob simply lists what exists. Only
        # the public bucket is known-complete, so only it gets pruned.
        return None
    manifest = _load(m.group("release"), m.group("theme"), m.group("type"))
    if manifest is None:
        return None
    prefix = upstream_glob[:-1]  # strip the trailing '*'
    keep = [
        prefix + file
        for file, (fxmin, fymin, fxmax, fymax) in manifest["files"].items()
        if fxmax >= xmin and fxmin <= xmax and fymax >= ymin and fymin <= ymax
    ]
    if not keep or len(keep) == len(manifest["files"]):
        # Nothing pruned (or nothing left): the glob is simpler and, for the
        # empty case, keeps the manifest out of the correctness story.
        return None
    joined = ", ".join("'" + p + "'" for p in keep)
    logger.debug(
        "manifest pruned %s to %d of %d files for bbox %s",
        upstream_glob, len(keep), len(manifest["files"]), bbox,
    )
    return f"read_parquet([{joined}], hive_partitioning=1)"
