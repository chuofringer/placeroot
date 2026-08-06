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
"""

import logging
import math
import os
from pathlib import Path

logger = logging.getLogger(__name__)

TILE_DEG = 1.0
DEFAULT_CACHE_DIR = os.path.expanduser("~/.cache/placeroot")
DEFAULT_MAX_MB = 500


def enabled() -> bool:
    """False iff PLACEROOT_CACHE=off. On (the default) otherwise."""
    return os.environ.get("PLACEROOT_CACHE", "").strip().lower() != "off"


def cache_dir() -> Path:
    return Path(os.environ.get("PLACEROOT_CACHE_DIR") or DEFAULT_CACHE_DIR)


def max_bytes() -> float:
    mb = float(os.environ.get("PLACEROOT_CACHE_MAX_MB", DEFAULT_MAX_MB))
    return mb * 1024 * 1024


def tiles_for_bbox(
    xmin: float, ymin: float, xmax: float, ymax: float, tile_deg: float = TILE_DEG
) -> list[tuple[int, int]]:
    """(tile_x, tile_y) grid cells a bbox touches, tile_x/y = floor(lon/lat / tile_deg)."""
    x0, x1 = math.floor(xmin / tile_deg), math.floor(xmax / tile_deg)
    y0, y1 = math.floor(ymin / tile_deg), math.floor(ymax / tile_deg)
    return [(tx, ty) for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)]


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


def local_paths_for_query(
    con, release: str, theme: str, bbox: tuple[float, float, float, float], upstream_glob: str
) -> list[str] | None:
    """Local cached parquet paths covering bbox, fetching uncached tiles from upstream.

    Returns None if caching is disabled. If a tile is already cached, its
    upstream is never touched again for that tile — this is what lets
    queries keep answering from cache when upstream later goes down (issue
    #5's cache-as-fallback path).
    """
    if not enabled():
        return None
    xmin, ymin, xmax, ymax = bbox
    tiles = tiles_for_bbox(xmin, ymin, xmax, ymax)
    paths = [ensure_tile(con, release, theme, t, upstream_glob) for t in tiles]
    return [str(p) for p in paths]


def parse_warm_region(spec: str) -> tuple[float, float, float] | None:
    """Parse "lat,lon,radius_m" -> (lat, lon, radius_m), or None if malformed."""
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
