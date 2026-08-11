"""Shared DuckDB connection management for the query layer.

This is the one place that configures a connection (httpfs, object cache,
S3 timeouts/retries) and loads the spatial extension; every theme module
(overture, routing, divisions, buildings) goes through it.

Concurrency (issue #24): conn_lock serializes every use of shared_conn(),
since DuckDB connections aren't safe for concurrent execute() calls.
Background tile materialization (cache.py) is the one exception, by
design: it always runs on its own connection via new_connection().

overture._conn/overture._conn_lock remain as thin aliases to shared_conn/
conn_lock (deprecated in favor of importing db directly) so existing
external references — tests included — keep working unchanged.
"""

import logging
import os
import threading
import time
from functools import lru_cache

import duckdb

logger = logging.getLogger(__name__)

# Guards every use of shared_conn(). See the module docstring above.
#
# Reentrant (RLock, #145): the cache-path schema probe re-enters this lock
# from the same thread — _from_source holds conn_lock, then calls into
# cache.resolve_fingerprint -> db.probe_schema, which itself takes conn_lock.
# A plain Lock self-deadlocks that thread whenever the inner probe isn't a
# cache hit (e.g. its lru entry was evicted under concurrent load). RLock
# allows the same-thread re-acquire while still fully serializing *across*
# threads — which is all the invariant needs, since a single thread never
# runs two shared_conn().execute() calls at once (they're sequential).
conn_lock = threading.RLock()

_spatial_loaded = False



# Region the public Overture bucket lives in — the default unless
# PLACEROOT_S3_REGION overrides it (issue #20's switchover: a mirror on a
# different S3-compatible service almost always has its own region name).
DEFAULT_S3_REGION = "us-west-2"


def _sql_str(value: str) -> str:
    """A single-quoted SQL string literal with embedded single quotes escaped.

    DuckDB's `SET <opt> = '...'` takes a literal, not a bind parameter, so
    values interpolated into one (the S3 region/endpoint from the
    environment, and mirror credentials) must have any single quote doubled.
    Otherwise a value containing one — an S3 secret key for a self-hosted
    endpoint can be arbitrary bytes — breaks the statement or injects SQL.
    """
    return "'" + value.replace("'", "''") + "'"


def _s3_region() -> str:
    return os.environ.get("PLACEROOT_S3_REGION", DEFAULT_S3_REGION)


def _s3_endpoint() -> str | None:
    """Custom S3-compatible endpoint (R2/minio/self-hosted), or None for
    plain AWS S3 — set via PLACEROOT_S3_ENDPOINT (issue #20)."""
    return os.environ.get("PLACEROOT_S3_ENDPOINT") or None


def _configure(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # An MCP tool call isn't an interactive terminal; a progress bar just
    # clutters (or, piped through a wrapping process, can garble) output.
    con.execute("SET enable_progress_bar=false;")
    con.execute(f"SET s3_region={_sql_str(_s3_region())};")
    endpoint = _s3_endpoint()
    if endpoint:
        # A mirror target (issue #20): R2/minio/self-hosted S3 generally
        # expect path-style addressing and, unlike the public Overture
        # bucket, are usually private — read credentials from the
        # environment if the operator set them, anonymous otherwise.
        con.execute(f"SET s3_endpoint={_sql_str(endpoint)};")
        con.execute("SET s3_url_style='path';")
        access_key = os.environ.get("PLACEROOT_S3_ACCESS_KEY_ID", "")
        secret_key = os.environ.get("PLACEROOT_S3_SECRET_ACCESS_KEY", "")
        con.execute(f"SET s3_access_key_id={_sql_str(access_key)};")
        con.execute(f"SET s3_secret_access_key={_sql_str(secret_key)};")
    else:
        con.execute("SET s3_access_key_id='';")  # public bucket: anonymous access
        con.execute("SET s3_secret_access_key='';")
    # Caches parquet footer/metadata per connection (issue #31): a repeat
    # query against the same file on the same connection skips the ~5s cold
    # footer-read cost. Combined with overture.warm_metadata's startup
    # pre-warm, this is often already paid before a real query arrives.
    con.execute("SET enable_object_cache=true;")
    # Remote scans are IO-bound, and the first query against a theme pays
    # one parquet-footer read per file (Overture themes span hundreds of
    # files). DuckDB parallelizes those reads across threads, so more
    # threads than cores is the right call here: measured on the buildings
    # theme (512 files), the cold metadata pass drops from ~52s at the
    # 8-thread default to ~25s at 64 and ~22s at 96. Local compute is unaffected in
    # practice — the extra threads idle when work is CPU-bound.
    try:
        threads = max(1, int(os.environ.get("PLACEROOT_DUCKDB_THREADS", 96)))
        con.execute(f"SET threads={threads};")
    except (ValueError, duckdb.Error) as e:
        logger.warning("Could not raise DuckDB thread count: %s", e)
    # Cache HTTP metadata (HEAD results, file handles) across queries on
    # this connection too — shaves repeat round-trips off every scan that
    # touches the same remote files this process has seen before.
    try:
        con.execute("SET enable_http_metadata_cache=true;")
    except duckdb.Error as e:
        logger.debug("enable_http_metadata_cache unavailable: %s", e)
    # Bounded timeout + retry on remote scans (issue #5): a slow or down
    # upstream fails fast instead of hanging a tool call.
    try:
        con.execute("SET http_timeout=5000;")  # ms
        con.execute("SET http_retries=2;")
        con.execute("SET http_retry_wait_ms=200;")
        con.execute("SET http_retry_backoff=2;")
    except duckdb.Error as e:
        logger.warning("Could not set httpfs timeout/retry options: %s", e)
    return con


@lru_cache(maxsize=1)
def shared_conn() -> duckdb.DuckDBPyConnection:
    """The one shared DuckDB connection every query-layer module reuses.

    Callers must hold conn_lock around any query they run against it.
    """
    return _configure(duckdb.connect())


def new_connection() -> duckdb.DuckDBPyConnection:
    """An independently-usable connection for background work: a *cursor*
    of the shared instance, not a fresh database.

    Background tile materialization (cache.py) and one-off local-table
    builds (geocode.py's #43 divisions table) must not share shared_conn()
    with a main-thread query — DuckDB connections aren't safe for
    concurrent use, but cursors of one instance are exactly the documented
    way to use one database from many threads. The instance being shared
    is the point, not a convenience: DuckDB's parquet metadata/object
    cache is per *instance*, and the cold cost of a theme is one footer
    read per file (buildings: 512 files, measured ~50s at default
    threads). On separate instances every background COPY and every warm
    pre-read paid that pass again for nothing; on cursors it is paid once
    per process, and a warm run on any cursor warms every query. Instance
    settings (httpfs, s3, threads) are already applied by shared_conn's
    _configure.
    """
    return shared_conn().cursor()


def warm_globs_async(globs: list[str]) -> None:
    """Warm the shared instance's parquet metadata for globs, in the
    background, off the query lock.

    For tools that know at entry they are about to touch a cold theme
    (gers_lookup spans three, geocode_address two): a cursor pays the
    footer pass concurrently with whatever the tool is already doing on
    the shared connection, so by the time the tool's own query reaches
    that theme its metadata is warm. Best-effort — failures are the next
    query's problem to surface properly.
    """
    def _warm():
        for glob in globs:
            try:
                new_connection().execute(
                    f"SELECT * FROM read_parquet('{glob}', hive_partitioning=1) LIMIT 0"
                )
            except duckdb.Error as e:
                logger.debug("speculative metadata warm failed for %s: %s", glob, e)

    threading.Thread(target=_warm, daemon=True).start()


def ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once.

    Lock-held internally. Idempotent and cheap to call on every query that
    needs ST_* functions (divisions.py, buildings.py, routing.py all do);
    the actual INSTALL/LOAD only ever runs once per process.
    """
    global _spatial_loaded
    if _spatial_loaded:
        return
    with conn_lock:
        if _spatial_loaded:
            return
        shared_conn().execute("INSTALL spatial; LOAD spatial;")
        _spatial_loaded = True


@lru_cache(maxsize=8)
def _probe_schema_cached(glob: str) -> frozenset:
    """Column names present in glob's dataset. Raises duckdb.Error if the probe fails.

    lru_cache memoizes only successful returns — a raised exception is NOT
    cached — so a transient probe failure is retried on the next call rather
    than poisoning the cache for the process lifetime (#144). Successful
    schemas stay cached (LRU, maxsize=8) since the LIMIT 0 metadata read,
    while cheap, isn't free to redo on every query.
    """
    with conn_lock:
        cols = shared_conn().execute(
            f"SELECT * FROM read_parquet('{glob}') LIMIT 0"
        ).description
    return frozenset(c[0] for c in cols)


# A failed probe is not retried for this long. Without it, a deployment
# whose upstream is down pays the probe's network timeout — and logs its
# warning — on every call site that consults the schema, several times per
# query (degraded_fields, the recreation layer, cache fingerprinting all
# probe). Short enough that recovery is near-automatic, long enough that
# the steady-state cost of a degraded upstream is one probe a minute.
PROBE_FAILURE_RETRY_S = 60.0
_probe_failed_at: dict[str, float] = {}


def probe_schema(glob: str) -> frozenset | None:
    """Column names present in glob's dataset, or None if the probe itself failed.

    A failed probe (upstream down, glob unreadable) is treated as "unknown,
    assume nothing missing" — the actual query that follows hits the same
    problem and surfaces it as UpstreamUnavailable instead. Failures are
    memoized for PROBE_FAILURE_RETRY_S — long enough to keep a degraded
    deployment from paying a network timeout and a warning per call, short
    enough that it heals within a minute of its upstream (#144's concern,
    a transient blip permanently blinding schema-drift detection, stays
    addressed: the memo expires). Successes stay cached indefinitely (see
    _probe_schema_cached).
    """
    failed_at = _probe_failed_at.get(glob)
    if failed_at is not None:
        if time.monotonic() - failed_at < PROBE_FAILURE_RETRY_S:
            return None
        _probe_failed_at.pop(glob, None)
    try:
        result = _probe_schema_cached(glob)
    except duckdb.Error as e:
        _probe_failed_at[glob] = time.monotonic()
        logger.warning("Schema probe failed for %s (not retried for %.0fs): %s",
                       glob, PROBE_FAILURE_RETRY_S, e)
        return None
    _probe_failed_at.pop(glob, None)
    return result
