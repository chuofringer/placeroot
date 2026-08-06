"""Local tile cache of touched Overture row groups.

Cold queries are network-bound (~5.6s) because every query re-scans the
remote GeoParquet dataset. This is a per-tile *materialization* cache, not a
row-group proxy: the world is divided into a coarse 1-degree lat/lon grid,
and the first query touching a tile copies the matching rows out of
upstream into a small local parquet file (one COPY per tile, keyed by
release/theme/tile). Every later query touching that tile reads the local
file instead of going to S3.

Tradeoff: a query whose radius straddles N tiles pays for N tile fetches
the first time, even if the tiles barely overlap the search circle, and a
tile is never partially invalidated — it's whole-tile or nothing. That's
the right trade for a point-radius search tool (queries cluster
geographically and get reused), but it would be a poor fit for a workload
that scans arbitrary large regions once each.

Eviction is mtime-based LRU over the whole cache directory, capped by
total size (not by file count or age), and re-checked after every write.

Query-first, materialize-later (issue #31): a COPY that pulls a whole tile
out of upstream costs seconds, and a caller shouldn't block a user-facing
query on it. When a query touches a tile that isn't cached yet,
local_paths_for_query() returns None (the caller falls back to scanning
upstream directly for *that* query) and kicks off materialization of the
missing tile(s) on a background daemon thread instead of waiting. Each
background fetch gets its own DuckDB connection — connections aren't safe
for concurrent use, and this one is created and used entirely off the
caller's connection/thread. An in-flight set (guarded by a lock) makes sure
two queries that both miss the same tile only trigger one fetch. Tests and
the startup warm-start want deterministic, synchronous behavior instead —
set PLACEROOT_CACHE_SYNC to materialize missing tiles inline, on the
caller's own connection, before returning.
"""

import logging
import math
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

TILE_DEG = 1.0
DEFAULT_CACHE_DIR = os.path.expanduser("~/.cache/placeroot")
DEFAULT_MAX_MB = 500

# Tiles currently being fetched by a background thread, as (release, theme,
# tile) keys — guards against two concurrent cache misses on the same tile
# both starting a fetch.
_inflight: set[tuple[str, str, tuple[int, int]]] = set()
_inflight_lock = threading.Lock()


def enabled() -> bool:
    """False iff PLACEROOT_CACHE=off. On (the default) otherwise."""
    return os.environ.get("PLACEROOT_CACHE", "").strip().lower() != "off"


def sync_mode() -> bool:
    """True iff PLACEROOT_CACHE_SYNC is set to a truthy value.

    Forces local_paths_for_query() to materialize missing tiles inline
    instead of handing them to a background thread — used by tests (so
    cache behavior is deterministic) and the startup warm-start (which is
    already an explicit, best-effort blocking call).
    """
    value = os.environ.get("PLACEROOT_CACHE_SYNC", "").strip().lower()
    return value not in ("", "0", "false", "off")


def cache_dir() -> Path:
    return Path(os.environ.get("PLACEROOT_CACHE_DIR") or DEFAULT_CACHE_DIR)


def max_bytes() -> float:
    mb = float(os.environ.get("PLACEROOT_CACHE_MAX_MB", DEFAULT_MAX_MB))
    return mb * 1024 * 1024


def tiles_for_bbox(
    xmin: float, ymin: float, xmax: float, ymax: float, tile_deg: float = TILE_DEG
) -> list[tuple[int, int]]:
    """(tile_x, tile_y) grid cells a bbox touches, tile_x/y = floor(lon/lat / tile_deg).

    xmin/xmax may fall outside [-180, 180] (issue #42: overture._bbox_around
    doesn't clamp/wrap longitude, so a search near the antimeridian produces
    a box like xmin=179.9, xmax=180.4). floor() over that raw range still
    walks a contiguous run of tile columns; wrap_x() then folds any column
    outside the canonical [-180, 180) range back into it (mod 360), which is
    exactly the tile on the *other* side of the seam — e.g. tile column 180
    (out of range) wraps to -180 (the westmost column, valid). For a
    non-crossing box every column is already in range, so wrap_x is the
    identity and tile ids are unchanged from before this fix.
    """
    x0, x1 = math.floor(xmin / tile_deg), math.floor(xmax / tile_deg)
    y0, y1 = math.floor(ymin / tile_deg), math.floor(ymax / tile_deg)
    span = round(360.0 / tile_deg)
    half = span // 2

    def wrap_x(tx: int) -> int:
        return ((tx + half) % span) - half

    tiles = [(wrap_x(tx), ty) for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)]
    # Preserve first-seen order while deduping: wrapping can only fold two
    # raw columns onto the same tile if the box is wider than a full 360
    # degrees of longitude, i.e. radius_m far beyond any realistic query —
    # cheap insurance against a duplicate tile fetch in that pathological case.
    return list(dict.fromkeys(tiles))


def tile_path(release: str, theme: str, tile: tuple[int, int]) -> Path:
    tx, ty = tile
    return cache_dir() / release / theme / f"tile_{ty}_{tx}.parquet"


def ensure_tile(con, release: str, theme: str, tile: tuple[int, int], upstream_glob: str) -> Path:
    """Local parquet path for `tile`, materializing it from upstream if needed.

    Raises whatever exception the upstream COPY raises if the tile isn't
    already cached and the fetch fails — callers that want cache-as-fallback
    behavior should check tile_path(...).exists() first and only call this
    when they're prepared to hit upstream.
    """
    path = tile_path(release, theme, tile)
    if path.exists():
        os.utime(path, None)  # bump mtime: this tile is recently used
        return path

    tx, ty = tile
    lon_min, lon_max = tx * TILE_DEG, (tx + 1) * TILE_DEG
    lat_min, lat_max = ty * TILE_DEG, (ty + 1) * TILE_DEG
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    sql = f"""
        COPY (
            SELECT * FROM read_parquet('{upstream_glob}', hive_partitioning=1)
            WHERE bbox.xmax >= {lon_min} AND bbox.xmin < {lon_max}
              AND bbox.ymax >= {lat_min} AND bbox.ymin < {lat_max}
        ) TO '{tmp_path}' (FORMAT PARQUET)
    """
    con.execute(sql)
    tmp_path.replace(path)
    evict_if_needed()
    return path


def cached_tile_paths(release: str, theme: str) -> list[Path]:
    """Every tile parquet already materialized locally for release/theme.

    Used by a GERS-id lookup (issue #41): before touching upstream at all,
    check whatever tiles the local cache already has on disk — cheap, since
    they're small local files. Returns [] if caching hasn't touched this
    release/theme yet (directory doesn't exist), never raises.
    """
    d = cache_dir() / release / theme
    if not d.exists():
        return []
    return sorted(d.glob("*.parquet"))


def evict_if_needed() -> None:
    """Delete least-recently-used cached tiles until under the size cap."""
    root = cache_dir()
    if not root.exists():
        return
    files = list(root.rglob("*.parquet"))
    files.sort(key=lambda p: p.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    cap = max_bytes()
    i = 0
    while total > cap and i < len(files):
        f = files[i]
        try:
            total -= f.stat().st_size
            f.unlink()
        except OSError:
            pass
        i += 1


def _materialize_in_background(
    release: str, theme: str, tile: tuple[int, int], upstream_glob: str, new_connection
) -> None:
    """Fetch `tile` on a daemon thread with its own connection, deduped by _inflight.

    Never raises to the caller — a failed background fetch is logged and
    just leaves the tile uncached for next time (the calling query already
    got its answer from upstream directly).
    """
    key = (release, theme, tile)
    with _inflight_lock:
        if key in _inflight:
            return
        _inflight.add(key)

    def _run():
        try:
            ensure_tile(new_connection(), release, theme, tile, upstream_glob)
        except Exception as e:  # noqa: BLE001 - background fetch must never surface
            logger.warning("Background tile materialization failed for %s: %s", key, e)
        finally:
            with _inflight_lock:
                _inflight.discard(key)

    threading.Thread(target=_run, daemon=True).start()


def local_paths_for_query(
    con,
    release: str,
    theme: str,
    bbox: tuple[float, float, float, float],
    upstream_glob: str,
    new_connection=None,
) -> list[str] | None:
    """Local cached parquet paths covering bbox, or None to fall back to upstream.

    Returns None if caching is disabled. If every touched tile is already
    cached, returns their paths without touching upstream at all — this is
    what lets queries keep answering from cache when upstream later goes
    down (issue #5's cache-as-fallback path).

    If any touched tile is missing, this does NOT block materializing it
    (a tile COPY costs seconds — issue #31): under PLACEROOT_CACHE_SYNC it
    fetches the missing tiles inline on `con` and returns the complete set
    of paths; otherwise it schedules background fetches (via
    `new_connection`, a zero-arg factory for a fresh connection — required
    whenever caching is enabled, since the background thread must not share
    `con`) and returns None so the caller queries upstream directly for
    this one query instead of waiting.
    """
    if not enabled():
        return None
    tiles = tiles_for_bbox(*bbox)

    cached, missing = [], []
    for t in tiles:
        path = tile_path(release, theme, t)
        if path.exists():
            os.utime(path, None)  # bump mtime: this tile is recently used
            cached.append(path)
        else:
            missing.append(t)

    if not missing:
        return [str(p) for p in cached]

    if sync_mode():
        for t in missing:
            cached.append(ensure_tile(con, release, theme, t, upstream_glob))
        return [str(p) for p in cached]

    for t in missing:
        _materialize_in_background(release, theme, t, upstream_glob, new_connection)
    return None


def parse_warm_region(spec: str) -> tuple[float, float, float] | None:
    """Parse "lat,lon,radius_m" -> (lat, lon, radius_m), or None if malformed."""
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
