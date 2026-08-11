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

Schema fingerprinting (issue #63): a tile materialized under one column
layout (an older code version, or a fixture with a different schema during
dev) must never be read back once upstream's schema has moved on — a
column that used to exist and got selected into the tile might not exist
in a later query's SELECT list, or vice versa. So every tile lives under
`<cache_dir>/<release>/<theme>/<fingerprint>/tile_Y_X.parquet`, where
`fingerprint` is the first 12 hex chars of sha256 over the *sorted* column
names of the upstream dataset (probed via db.probe_schema, which is
lru_cached — the probe itself is a `LIMIT 0` metadata-only read, cheap
enough to redo per query). Two schemas with the same columns in a
different order hash identically (sorted first) since column order isn't
what breaks a SELECT *; two schemas differing by even one column name hash
differently and land in separate directories, so a query against the new
schema can never read an old-schema tile (or vice versa) — see
resolve_fingerprint().

Old-layout tiles (materialized before this change, sitting directly under
`<theme>/`) and any other-fingerprint directories are deliberately left
alone rather than actively migrated or deleted: they're inert (nothing
ever constructs a path back into them once the resolved fingerprint moves
on) and they still get walked by evict_if_needed()'s rglob, so they age
out of the size cap exactly like any other cold tile. Actively migrating
would mean rewriting files under a lock for no behavioral benefit — a
stale tile that's simply never looked at again is just as effectively
"invalidated" as one that's been deleted, and self-cleans for free.

The one subtlety is what happens when upstream is unreachable (issue #5's
cache-as-fallback promise: a query should still answer from whatever's
cached even if S3 is down). db.probe_schema() returns None on failure, so
resolve_fingerprint() can't compute a *current* fingerprint — but the
whole point of cache-as-fallback is answering without contacting upstream
at all. In that case it falls back to the most-recently-used existing
fingerprint directory for that release/theme, on the theory that whatever
fingerprint dir has tiles in it corresponds to *some* schema that
materialized successfully before, and it's the most likely one still to be
relevant. This means an offline query can, in principle, be served tiles
keyed under a fingerprint that's no longer upstream's current schema — but
that's strictly better than the alternative (no cache-as-fallback at all
when offline), and it only ever happens when upstream can't be reached to
tell us any different.
"""

import hashlib
import itertools
import logging
import math
import os
import threading
import time
from pathlib import Path

from placeroot import db

logger = logging.getLogger(__name__)

TILE_DEG = 1.0
DEFAULT_CACHE_DIR = os.path.expanduser("~/.cache/placeroot")
DEFAULT_MAX_MB = 500

# Ceiling on how many 1-degree tiles a single query may fan out into. Each
# missing tile in a query costs one background thread + one upstream COPY
# (or, under PLACEROOT_CACHE_SYNC, one sequential COPY), so an oversized
# bbox — e.g. from an abusive multi-thousand-km radius — could otherwise
# spawn tens of thousands of threads/connections and hammer upstream: a
# resource-exhaustion vector reachable straight from a tool argument. A
# query above this cap skips the tile cache and scans upstream directly for
# that one call (the bbox pushdown still prunes row groups); the query
# layer's radius clamp (geo.MAX_QUERY_RADIUS_M) keeps that direct scan
# bounded too. Normal point-radius queries touch a handful of tiles, far
# under this.
MAX_TILES_PER_QUERY = 128

# Early-out threshold tiles_for_bbox() checks BEFORE building the (tx, ty)
# cross product (issue #163's A1: a degenerate bbox — e.g. from an
# unclamped near-pole dlon — must not get to allocate tens of millions of
# tuples just so local_paths_for_query() can throw them away one line
# later for exceeding MAX_TILES_PER_QUERY). Deliberately looser than
# MAX_TILES_PER_QUERY itself, not equal to it: the raw x_span*y_span grid
# product checked here can be a slight overcount of the real (deduped)
# tile count for a bbox that wraps the antimeridian, so this stays a cheap,
# clearly-oversized early-out rather than a second, subtly different copy
# of local_paths_for_query's authoritative cap.
OVERSIZE_TILE_SPAN_GUARD = MAX_TILES_PER_QUERY * 4

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

    # Defense in depth for issue #163's A1: geo.bbox_around now clamps its
    # span before it ever gets here, but this function has no way to know
    # that every caller does — so compute the projected tile count from the
    # same x0/x1/y0/y1 this function already derived, BEFORE materialising
    # the (tx, ty) cross product, and bail out to a small sentinel list once
    # it's clearly beyond what local_paths_for_query()'s MAX_TILES_PER_QUERY
    # cap would accept anyway. Without this, an oversized bbox slipping
    # through builds tens of millions of tuples just to have the caller
    # throw the list away one line later. The threshold is deliberately
    # looser than MAX_TILES_PER_QUERY itself (not equal to it) so this stays
    # a cheap early-out rather than a second, subtly-different copy of the
    # caller's real cap — local_paths_for_query still does the authoritative
    # `len(tiles) > MAX_TILES_PER_QUERY` check and takes its existing
    # "too many tiles, scan upstream directly" branch either way.
    def wrap_x(tx: int) -> int:
        return ((tx + half) % span) - half

    x_span = x1 - x0 + 1
    y_span = y1 - y0 + 1
    if x_span * y_span > OVERSIZE_TILE_SPAN_GUARD:
        # Too big to materialise: enumerate only the first
        # MAX_TILES_PER_QUERY + 1 tiles — already more than any caller's
        # `len(tiles) > MAX_TILES_PER_QUERY` cap accepts, without building
        # the full cross product. These are REAL tiles of the bbox (a
        # prefix of the full enumeration), not a fabricated filler: a
        # caller that iterates the result — a warm loop, a log line —
        # sees genuine ids rather than repeated copies of the
        # perfectly-valid tile (0, 0). Dedup can't shrink the prefix
        # under the cap: ty is never wrapped and two raw columns only
        # collide when they are a full 360° apart, far beyond this many
        # consecutive tx values.
        logger.warning(
            "bbox projects to ~%d tiles; truncating enumeration at %d",
            x_span * y_span, MAX_TILES_PER_QUERY + 1,
        )
        prefix = itertools.islice(
            ((wrap_x(tx), ty) for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)),
            MAX_TILES_PER_QUERY + 1,
        )
        return list(dict.fromkeys(prefix))

    tiles = [(wrap_x(tx), ty) for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)]
    # Preserve first-seen order while deduping: wrapping can only fold two
    # raw columns onto the same tile if the box is wider than a full 360
    # degrees of longitude, i.e. radius_m far beyond any realistic query —
    # cheap insurance against a duplicate tile fetch in that pathological case.
    return list(dict.fromkeys(tiles))


def schema_fingerprint(upstream_glob: str) -> str | None:
    """First 12 hex chars of sha256 over upstream_glob's sorted column names.

    None if the schema probe itself failed (upstream unreachable) — see
    resolve_fingerprint() for how callers handle that.
    """
    cols = db.probe_schema(upstream_glob)
    if cols is None:
        return None
    return hashlib.sha256(",".join(sorted(cols)).encode()).hexdigest()[:12]


def _fingerprint_dirs(release: str, theme: str) -> list[Path]:
    d = cache_dir() / release / theme
    if not d.exists():
        return []
    return [p for p in d.iterdir() if p.is_dir()]


def _fingerprint_last_use(fp_dir: Path) -> float:
    """Newest tile-file mtime under fp_dir — its true last-*use* time.

    A cache hit bumps each tile file's mtime (os.utime in ensure_tile /
    local_paths_for_query), but NOT the containing directory's mtime (a
    dir's mtime only moves when an entry is added/removed). So ranking
    fingerprint dirs by dir mtime picks the most-recently-*created* one, not
    the most-recently-*used* — during an outage that can drop the whole
    active cache for a barely-populated newer dir (#141). Rank by the newest
    contained tile instead, falling back to the dir's own mtime if empty.
    """
    tiles = list(fp_dir.glob("*.parquet"))
    if tiles:
        return max(t.stat().st_mtime for t in tiles)
    return fp_dir.stat().st_mtime


def resolve_fingerprint(release: str, theme: str, upstream_glob: str) -> str | None:
    """The schema fingerprint to read/write release/theme tiles under.

    The common case: probe upstream_glob's current schema and return its
    fingerprint. If upstream can't be reached (schema_fingerprint returns
    None), fall back to the most-recently-used existing fingerprint
    directory for this release/theme — see the module docstring's
    "Schema fingerprinting" section for why that's the right offline
    behavior. Returns None only when upstream is unreachable AND there's no
    existing fingerprint directory to fall back to either — nothing to key
    a tile under, nothing cached to serve; callers should treat this the
    same as any other upstream-unavailable case.
    """
    fp = schema_fingerprint(upstream_glob)
    if fp is not None:
        return fp
    dirs = _fingerprint_dirs(release, theme)
    if not dirs:
        return None
    newest = max(dirs, key=_fingerprint_last_use)
    return newest.name


def tile_path(release: str, theme: str, fingerprint: str, tile: tuple[int, int]) -> Path:
    tx, ty = tile
    return cache_dir() / release / theme / fingerprint / f"tile_{ty}_{tx}.parquet"


def ensure_tile(
    con,
    release: str,
    theme: str,
    tile: tuple[int, int],
    upstream_glob: str,
    fingerprint: str | None = None,
) -> Path:
    """Local parquet path for `tile`, materializing it from upstream if needed.

    fingerprint defaults to None, meaning "resolve it from upstream_glob via
    resolve_fingerprint()" — callers that already resolved a fingerprint for
    this query (local_paths_for_query, the background materializer) pass it
    explicitly so every tile touched by the same query lands under the same
    fingerprint dir even if upstream's schema were to change mid-query.

    Raises whatever exception the upstream COPY raises if the tile isn't
    already cached and the fetch fails — callers that want cache-as-fallback
    behavior should check tile_path(...).exists() first and only call this
    when they're prepared to hit upstream.
    """
    if fingerprint is None:
        fingerprint = resolve_fingerprint(release, theme, upstream_glob)
        if fingerprint is None:
            # Upstream unreachable and nothing cached yet under any
            # fingerprint for this release/theme: there's no local tile to
            # fall back to, so let the COPY below run and raise upstream's
            # real error rather than inventing a fingerprint for a tile
            # that's about to fail to materialize anyway.
            fingerprint = "unreachable"

    path = tile_path(release, theme, fingerprint, tile)
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


def _claim_existing_tiles(
    release: str, theme: str, fingerprint: str, tiles: list[tuple[int, int]]
) -> tuple[list[Path], list[tuple[int, int]]]:
    """(on-disk tile paths claimed against eviction, tiles not on disk).

    The one implementation of the #142 critical section: each hit is
    claimed in the same locked scan that finds it on disk, so eviction
    can't delete a tile between the existence check and the claim that is
    supposed to protect it. Callers materialize the missing tiles *outside*
    this lock (a DB COPY and its own eviction pass do not belong under it).
    """
    cached, missing = [], []
    with _claims_lock:
        now = time.monotonic()
        _prune_expired_locked(now)
        deadline = now + _CLAIM_TTL_S
        for t in tiles:
            path = tile_path(release, theme, fingerprint, t)
            if path.exists():
                os.utime(path, None)  # bump mtime: this tile is recently used
                _claim_locked(str(path), deadline)
                cached.append(path)
            else:
                missing.append(t)
    return cached, missing


def cached_tile_paths(release: str, theme: str, upstream_glob: str) -> list[Path]:
    """Every tile parquet already materialized locally for release/theme's
    *currently resolved* schema fingerprint.

    Used by a GERS-id lookup (issue #41): before touching upstream at all,
    check whatever tiles the local cache already has on disk — cheap, since
    they're small local files. upstream_glob is needed (issue #63) to
    resolve which fingerprint directory is current — without it this could
    silently return stale-schema tiles. Returns [] if caching hasn't
    touched this release/theme/fingerprint yet, or if upstream is
    unreachable and no fingerprint directory exists to fall back to; never
    raises. The returned tiles are claimed against eviction (#142) so the
    caller's read — or its schema probe of one of them — can't race a
    concurrent eviction pass.
    """
    fingerprint = resolve_fingerprint(release, theme, upstream_glob)
    if fingerprint is None:
        return []
    d = cache_dir() / release / theme / fingerprint
    if not d.exists():
        return []
    paths = sorted(d.glob("*.parquet"))
    with _claims_lock:
        now = time.monotonic()
        _prune_expired_locked(now)
        deadline = now + _CLAIM_TTL_S
        existing = []
        for path in paths:
            if path.exists():
                os.utime(path, None)
                _claim_locked(str(path), deadline)
                existing.append(path)
    return existing


def cached_tile_paths_for_bbox(
    release: str, theme: str, upstream_glob: str,
    bbox: tuple[float, float, float, float],
) -> list[Path]:
    """Tiles already on disk that intersect bbox — nothing materialized.

    The bounded, cache-only counterpart to cached_tile_paths, for the
    source_sql path where the upstream must not be touched: a query should
    read the tiles its box covers, not every tile the theme has ever
    cached. Tiles found are claimed against eviction for the query's
    lifetime via the shared #142 critical section (_claim_existing_tiles).
    An oversized bbox (> MAX_TILES_PER_QUERY tiles) returns [] — with no
    upstream to fall back to, skipping is the only bounded answer.
    """
    fingerprint = resolve_fingerprint(release, theme, upstream_glob)
    if fingerprint is None:
        return []
    tiles = tiles_for_bbox(*bbox)
    if len(tiles) > MAX_TILES_PER_QUERY:
        return []
    found, _missing = _claim_existing_tiles(release, theme, fingerprint, tiles)
    return sorted(found)



# --- #142: protecting in-flight tiles from eviction ------------------------
#
# A query resolves its cache-hit tile paths (local_paths_for_query, under
# db.conn_lock), releases the lock, and only then runs the SELECT that reads
# them. In that gap a concurrent request that misses a different tile can
# call ensure_tile -> evict_if_needed, which walks the cache by mtime and
# deletes files to fit the size cap -- potentially the very tiles the first
# query just resolved. Its read then fails on a missing file and surfaces as
# a hard UpstreamUnavailable even though upstream is perfectly reachable.
#
# Rather than widening db.conn_lock across resolve-and-read (a lock-scope
# change with real deadlock and latency risk), resolved paths are recorded
# here with an expiry, and evict_if_needed skips any path whose claim is
# still live. No release call is needed -- and so nothing leaks if a query
# raises midway -- because the claim simply expires.
#
# Only cache HITS handed back to a caller are claimed. A tile freshly
# created by ensure_tile isn't claimed by its creation, so the LRU cap
# still behaves exactly as before for the warm/populate path.
_CLAIM_TTL_S = 60.0

_claims: dict[str, float] = {}
# Guards _claims AND serializes it against the eviction delete loop, so
# "this tile exists, claim it" and "this tile is unclaimed, delete it" can't
# interleave -- a snapshot-then-delete check would still leave a (small)
# window where a tile is claimed after the snapshot and deleted anyway.
#
# RLock, and deliberately never held across a DB call: local_paths_for_query
# runs under db.conn_lock, so the only lock order that ever occurs is
# conn_lock -> _claims_lock. Nothing takes conn_lock while holding this one
# (the tile COPY in ensure_tile happens outside it), so the two can't
# deadlock against each other -- cf. #145, where a non-reentrant lock
# re-entered on one thread hung a worker.
_claims_lock = threading.RLock()


def _prune_expired_locked(now: float) -> None:
    """Drop lapsed claims. Caller holds _claims_lock."""
    for p in [p for p, deadline in _claims.items() if deadline <= now]:
        del _claims[p]


def _claim_locked(path: str, deadline: float) -> None:
    """Record one claim. Caller holds _claims_lock.

    Extends, never shortens: if a concurrent claim on the same tile runs
    longer, that longer lease wins.
    """
    if _claims.get(path, 0.0) < deadline:
        _claims[path] = deadline


def claim_paths(paths: list[str], ttl_s: float = _CLAIM_TTL_S) -> None:
    """Mark `paths` as in-flight, protecting them from eviction for ttl_s."""
    if not paths:
        return
    now = time.monotonic()
    deadline = now + ttl_s
    with _claims_lock:
        # Prune here too, not just during eviction: on a cache comfortably
        # under cap, eviction may never run, and _claims would otherwise
        # accumulate an entry per distinct tile ever queried.
        _prune_expired_locked(now)
        for p in paths:
            _claim_locked(p, deadline)


def _is_claimed_locked(path: str, now: float) -> bool:
    """Whether `path` has a live claim. Caller holds _claims_lock."""
    deadline = _claims.get(path)
    return deadline is not None and deadline > now


def evict_if_needed() -> None:
    """Delete least-recently-used cached tiles until under the size cap.

    Tiles currently claimed by an in-flight query (see claim_paths, #142)
    are skipped rather than deleted: evicting one out from under a query
    that already resolved it turns a healthy cache hit into a spurious
    UpstreamUnavailable. Their size still counts toward the total, so if
    claims alone keep the cache over cap this pass simply frees what it can
    -- the cap is a target, not a hard bound, and the next pass (after the
    claims expire) collects the rest.
    """
    root = cache_dir()
    if not root.exists():
        return
    # Only tile files (tile_Y_X.parquet, old- or new-layout) are evictable.
    # The cache root also holds support tables that are NOT tiles: the
    # geocode divisions name table (geocode-divisions/table.parquet, #43)
    # and any sibling per-release tables built once and reused for the whole
    # session. They are large, built exactly once, and never re-touched, so
    # under an oldest-mtime-first sweep they are always the first casualty —
    # evicting one turns a healthy local query into a false
    # UpstreamUnavailable mid-query, and the subsequent rebuild immediately
    # re-triggers eviction (thrash, #230). They are excluded from the size
    # accounting too: they're a fixed per-release overhead, and counting
    # them against the cap would let a large table squeeze the effective
    # tile budget to zero and evict every tile on every pass.
    files = [f for f in root.rglob("*.parquet") if f.name.startswith("tile_")]
    files.sort(key=lambda p: p.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    cap = max_bytes()
    skipped = 0
    # The claim check and the unlink have to be atomic against a query
    # claiming a tile it just found on disk, so the delete loop runs under
    # _claims_lock. Only filesystem work happens inside -- the scan and sort
    # above are already done, and no DB call is made here.
    with _claims_lock:
        now = time.monotonic()
        _prune_expired_locked(now)
        i = 0
        while total > cap and i < len(files):
            f = files[i]
            i += 1
            if _is_claimed_locked(str(f), now):
                skipped += 1
                continue
            try:
                total -= f.stat().st_size
                f.unlink()
            except OSError:
                pass
    if total > cap and skipped:
        logger.info(
            "cache still %d bytes over cap after eviction: %d in-flight tile(s) "
            "were skipped to avoid evicting them mid-query; they become "
            "evictable once their claims expire",
            total - cap, skipped,
        )


def _materialize_in_background(
    release: str,
    theme: str,
    tile: tuple[int, int],
    upstream_glob: str,
    fingerprint: str,
    new_connection,
) -> None:
    """Fetch `tile` on a daemon thread with its own connection, deduped by _inflight.

    fingerprint is resolved once by the caller (local_paths_for_query) and
    threaded through here and into ensure_tile so the in-flight dedup key
    (and the tile's eventual path) can't disagree with what the triggering
    query resolved, even if upstream's schema were to change between the
    two — see the module docstring's "Schema fingerprinting" section.

    Never raises to the caller — a failed background fetch is logged and
    just leaves the tile uncached for next time (the calling query already
    got its answer from upstream directly).
    """
    key = (release, theme, fingerprint, tile)
    with _inflight_lock:
        if key in _inflight:
            return
        _inflight.add(key)

    def _run():
        try:
            ensure_tile(new_connection(), release, theme, tile, upstream_glob, fingerprint)
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

    Returns None if caching is disabled. Resolves the current schema
    fingerprint for release/theme first (issue #63 — see resolve_fingerprint
    and the module docstring's "Schema fingerprinting" section); if that
    fails (upstream unreachable and no existing fingerprint dir to fall
    back to for this release/theme), also returns None — there's nothing
    local to serve and nothing to key a new tile under, so the caller falls
    back to upstream and hits the same unreachable error there.

    If every touched tile is already cached under the resolved fingerprint,
    returns their paths without touching upstream at all — this is what
    lets queries keep answering from cache when upstream later goes down
    (issue #5's cache-as-fallback path).

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
    fingerprint = resolve_fingerprint(release, theme, upstream_glob)
    if fingerprint is None:
        return None
    tiles = tiles_for_bbox(*bbox)
    if len(tiles) > MAX_TILES_PER_QUERY:
        # Oversized bbox: don't fan out into a tile-per-thread materialization
        # storm. Fall back to a single direct upstream scan for this query.
        logger.warning(
            "query bbox touches %d tiles (> MAX_TILES_PER_QUERY=%d); skipping the "
            "tile cache and scanning upstream directly for this query",
            len(tiles), MAX_TILES_PER_QUERY,
        )
        return None

    cached, missing = _claim_existing_tiles(release, theme, fingerprint, tiles)

    if not missing:
        return [str(p) for p in cached]

    if sync_mode():
        # Claim every tile this query holds BEFORE each fetch (#158):
        # ensure_tile runs its own evict_if_needed() right after writing, so
        # a query spanning several missing tiles would otherwise evict the
        # ones it fetched earlier this loop (they're the oldest *unclaimed*
        # files) before the caller ever reads them — defeating #150 with
        # zero concurrency. The refresh must cover the hits and the
        # already-fetched tiles too, not just the tile about to be fetched:
        # a loop of sequential upstream COPYs can outlast _CLAIM_TTL_S, and
        # a claim taken only once at discovery (or at that tile's own fetch)
        # would expire mid-loop and leave the tile evictable again. Claiming
        # a not-yet-existing path is harmless: eviction only deletes files
        # that exist and skips claimed paths regardless.
        held = [str(p) for p in cached]
        for t in missing:
            held.append(str(tile_path(release, theme, fingerprint, t)))
            claim_paths(held)
            cached.append(ensure_tile(con, release, theme, t, upstream_glob, fingerprint))
        paths = [str(p) for p in cached]
        # Fresh TTL on the way out so the caller has the full window to read
        # the files, however long the fetch loop took.
        claim_paths(paths)
        return paths

    for t in missing:
        _materialize_in_background(release, theme, t, upstream_glob, fingerprint, new_connection)
    return None


def source_sql(
    theme: str,
    upstream_glob: str,
    bbox: tuple[float, float, float, float] | None,
    *,
    upstream_fallback: bool = True,
) -> str | None:
    """FROM-clause SQL for a theme: local cache tiles when available, upstream
    otherwise. The one implementation of the "cached tiles else upstream glob"
    resolution that overture.py and recreation.py previously each hand-copied
    (and let diverge); the other theme modules can migrate here too.

    With a bbox, missing tiles are materialized per local_paths_for_query's
    contract (inline under PLACEROOT_CACHE_SYNC, in the background otherwise).
    bbox None is the unbounded-lookup case (an id or name query with no
    location to bound it): only tiles already on disk are used and nothing
    new is materialized — an unbounded lookup must not trigger a world-sized
    fetch.

    upstream_fallback=False means the upstream glob must not be touched *at
    all*: no fallback scan, and no tile materialization either (with or
    without a bbox, only tiles already on disk serve) — returning None when
    nothing local can. For callers where the glob is known-unusable (the
    recreation layer's unreadable-upstream case, where a COPY from it would
    fail the query it was meant to supplement) or where an unprunable scan
    of it would be planet-scale (the layer's unbounded lookups).

    Raises duckdb.Error on cache resolution failure; callers wrap it in their
    own unavailable-upstream error the way they wrap the query itself.
    """
    if enabled():
        from placeroot import release as release_mod

        active_release = release_mod.resolve_release()
        if bbox is None:
            paths = cached_tile_paths(active_release, theme, upstream_glob)
        elif not upstream_fallback:
            paths = cached_tile_paths_for_bbox(active_release, theme, upstream_glob, bbox)
        else:
            with db.conn_lock:
                paths = local_paths_for_query(
                    db.shared_conn(), active_release, theme, bbox, upstream_glob,
                    db.new_connection,
                )
        if paths:
            joined = ", ".join(f"'{p}'" for p in paths)
            return f"read_parquet([{joined}])"
    if not upstream_fallback:
        return None
    return f"read_parquet('{upstream_glob}', hive_partitioning=1)"


def parse_warm_region(spec: str) -> tuple[float, float, float] | None:
    """Parse "lat,lon,radius_m" -> (lat, lon, radius_m), or None if malformed."""
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
