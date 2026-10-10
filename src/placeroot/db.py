"""Shared DuckDB connection management for the query layer.

This is the one place that configures a connection (httpfs, parquet
metadata cache, memory/temp dir, S3 timeouts/retries) and loads the spatial
extension; every theme module
(overture, routing, divisions, buildings) goes through it.

Concurrency (issue #24): conn_lock serializes every use of shared_conn(),
since DuckDB connections aren't safe for concurrent execute() calls.
Background tile materialization (cache.py) is the one exception, by
design: it always runs on its own connection via new_connection().

overture._conn/overture._conn_lock remain as thin aliases to shared_conn/
conn_lock (deprecated in favor of importing db directly) so existing
external references — tests included — keep working unchanged.
"""

import contextlib
import logging
import os
import threading
import time
from functools import lru_cache

import duckdb

logger = logging.getLogger(__name__)

# Threads running inside isolated_reads() get their own cursor and their own
# lock (see isolated_reads below); everything else shares the global RLock.
_isolation = threading.local()


class _ThreadAwareRLock:
    """The global connection RLock, unless this thread opted into isolation.

    All existing call sites — including the import-time aliases
    (overture._conn_lock = db.conn_lock) — hold a reference to this one
    object, so delegation has to happen inside it rather than by swapping
    the module attribute.
    """

    def __init__(self):
        self._global = threading.RLock()

    def _target(self):
        return getattr(_isolation, "lock", None) or self._global

    def acquire(self, *args, **kwargs):
        return self._target().acquire(*args, **kwargs)

    def release(self):
        return self._target().release()

    def __enter__(self):
        return self._target().__enter__()

    def __exit__(self, *exc):
        return self._target().__exit__(*exc)


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
conn_lock = _ThreadAwareRLock()

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


def _extension_directory() -> str | None:
    """Where DuckDB looks for (and installs) extensions, if the operator
    overrode DuckDB's default (~/.duckdb/extensions) via
    PLACEROOT_DUCKDB_EXTENSION_DIR — e.g. a pre-populated, read-only
    directory on an air-gapped host. None means DuckDB's own default."""
    return os.environ.get("PLACEROOT_DUCKDB_EXTENSION_DIR") or None


def load_extension(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """LOAD a DuckDB extension, installing it first only if LOAD fails.

    `INSTALL x; LOAD x` makes every connection go through INSTALL's
    install-path logic (a write into extension_directory, and a download
    from extensions.duckdb.org when the extension isn't there). LOAD alone
    is purely local: a machine that already has the extension — from an
    earlier run, or a pre-populated, possibly read-only
    PLACEROOT_DUCKDB_EXTENSION_DIR — loads it without ever considering the
    network. Only a genuinely missing extension pays the download, and if
    that fails too the error is DuckDB's own, naming the extension and URL.
    """
    try:
        con.execute(f"LOAD {name};")
        return
    except duckdb.Error as e:
        logger.debug("LOAD %s failed (%s); trying INSTALL first", name, e)
    con.execute(f"INSTALL {name}; LOAD {name};")


def _cache_dir() -> str:
    """The placeroot cache directory, resolved the way cache.cache_dir()
    does (PLACEROOT_CACHE_DIR, else ~/.cache/placeroot) without importing
    cache.py — it imports this module, and _configure runs early."""
    return os.environ.get("PLACEROOT_CACHE_DIR") or os.path.expanduser(
        "~/.cache/placeroot"
    )


# httpfs' http_timeout is in SECONDS (DuckDB >= 1.1; default 30). An earlier
# version of this file set 5000 believing the unit was milliseconds — that
# is ~83 minutes, the opposite of issue #5's fail-fast intent.
DEFAULT_HTTP_TIMEOUT_S = 30


def _http_timeout_s() -> int:
    raw = os.environ.get("PLACEROOT_HTTP_TIMEOUT_S", "")
    try:
        return max(1, int(raw)) if raw else DEFAULT_HTTP_TIMEOUT_S
    except ValueError:
        logger.warning(
            "Ignoring PLACEROOT_HTTP_TIMEOUT_S=%r (not an integer); using %ds",
            raw, DEFAULT_HTTP_TIMEOUT_S,
        )
        return DEFAULT_HTTP_TIMEOUT_S


def _configure(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    ext_dir = _extension_directory()
    if ext_dir:
        # Must precede the first LOAD: it is where LOAD looks.
        con.execute(f"SET extension_directory={_sql_str(ext_dir)};")
    load_extension(con, "httpfs")
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
    # Cache parquet footers/metadata across queries on this instance (issue
    # #31): a repeat query against a file this process has already scanned
    # skips the cold footer read (~5s per theme on a warm link, far more
    # cold). Combined with overture.warm_metadata's startup pre-warm, that
    # cost is often already paid before a real query arrives. Note this is
    # `parquet_metadata_cache` (default false), NOT `enable_object_cache`:
    # in DuckDB 1.5 the latter is documented by duckdb_settings() as a
    # "[PLACEHOLDER] Legacy setting - does nothing". The external file cache
    # (enable_external_file_cache, default true) separately keeps read
    # ranges of remote files in memory; its default validation mode stays
    # on, since locally materialized tiles (cache.py) are rewritten in place.
    con.execute("SET parquet_metadata_cache=true;")
    # Memory and spill location. The thread count below is high, so give
    # operators a knob for DuckDB's memory ceiling (default: DuckDB's own,
    # ~80% of RAM) and keep spill files under the placeroot cache dir rather
    # than DuckDB's default `.tmp` relative to whatever the cwd happens to be.
    memory_limit = os.environ.get("PLACEROOT_DUCKDB_MEMORY_LIMIT", "").strip()
    if memory_limit:
        try:
            con.execute(f"SET memory_limit={_sql_str(memory_limit)};")
        except duckdb.Error as e:
            logger.warning(
                "Ignoring PLACEROOT_DUCKDB_MEMORY_LIMIT=%r: %s", memory_limit, e
            )
    try:
        temp_dir = os.path.join(_cache_dir(), "duckdb_tmp")
        con.execute(f"SET temp_directory={_sql_str(temp_dir)};")
    except duckdb.Error as e:
        logger.warning("Could not set DuckDB temp_directory: %s", e)
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
    # upstream fails fast instead of hanging a tool call. http_timeout is
    # in seconds (see DEFAULT_HTTP_TIMEOUT_S); PLACEROOT_HTTP_TIMEOUT_S
    # overrides it.
    try:
        con.execute(f"SET http_timeout={_http_timeout_s()};")  # seconds
        con.execute("SET http_retries=2;")
        con.execute("SET http_retry_wait_ms=200;")
        con.execute("SET http_retry_backoff=2;")
    except duckdb.Error as e:
        logger.warning("Could not set httpfs timeout/retry options: %s", e)
    return con


# Serializes first creation of the shared instance. lru_cache does not
# serialize a cold first call, and isolated_reads() runs off conn_lock —
# without this, two parallel workers on a cold process would each connect
# and configure their own DuckDB instance, binding one worker's cursor (and
# everything it warms) to an orphan that loses the cache race.
_instance_lock = threading.Lock()
_instance: duckdb.DuckDBPyConnection | None = None


def _shared_instance() -> duckdb.DuckDBPyConnection:
    global _instance
    inst = _instance
    if inst is None:
        with _instance_lock:
            if _instance is None:
                _instance = _configure(duckdb.connect())
            inst = _instance
    return inst


def shared_conn() -> duckdb.DuckDBPyConnection:
    """The one shared DuckDB connection every query-layer module reuses.

    Callers must hold conn_lock around any query they run against it.
    Inside isolated_reads() this returns the thread's private cursor
    instead, so parallel read-only work stops contending.
    """
    override = getattr(_isolation, "conn", None)
    if override is not None:
        return override
    return _shared_instance()


@contextlib.contextmanager
def isolated_reads():
    """Run this thread's queries on a private cursor with a private lock.

    Cursors of one DuckDB instance are the documented way to use one
    database from many threads (see new_connection); the instance-level
    parquet/object caches stay shared. With the private lock in place of
    the global one, two threads doing read-only work (from_to resolving
    both ends, #328) genuinely overlap instead of serializing — measured
    on the c15 corpus walk, the cold name-pair peek halves.

    Read-only by contract: writes that parallel readers could trigger stay
    serialized by their own dedicated locks — geocode's _build_lock dedups
    the background table-build spawn, and its _blocking_build_lock
    serializes the blocking (foreground) divisions builds — which this does
    not replace.
    """
    if getattr(_isolation, "conn", None) is not None:
        yield
        return
    _isolation.conn = _shared_instance().cursor()
    _isolation.lock = threading.RLock()
    try:
        yield
    finally:
        try:
            _isolation.conn.close()
        except duckdb.Error:  # pragma: no cover - close is best-effort
            pass
        _isolation.conn = None
        _isolation.lock = None


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


def ensure_spatial() -> None:
    """Load DuckDB's spatial extension on the shared connection, once.

    Lock-held internally. Idempotent and cheap to call on every query that
    needs ST_* functions (divisions.py, buildings.py, routing.py all do);
    the actual LOAD (and INSTALL, if the extension is missing locally — see
    load_extension) only ever runs once per process.
    """
    global _spatial_loaded
    if _spatial_loaded:
        return
    with conn_lock:
        if _spatial_loaded:
            return
        load_extension(shared_conn(), "spatial")
        _spatial_loaded = True


# Sized above the number of distinct theme/type globs a process touches
# (a dozen or so across places, divisions, buildings, transportation, base
# subtypes, plus mirror/override variants): a smaller cache evicted live
# globs under normal use and re-probed them over the network.
@lru_cache(maxsize=32)
def _probe_schema_cached(glob: str) -> frozenset:
    """Column names present in glob's dataset. Raises duckdb.Error if the probe fails.

    lru_cache memoizes only successful returns — a raised exception is NOT
    cached — so a transient probe failure is retried on the next call rather
    than poisoning the cache for the process lifetime (#144). Successful
    schemas stay cached (LRU, maxsize=32) since the LIMIT 0 metadata read,
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

    Bundled-manifest fast path: for a default-base glob of a bundled
    release the columns are a recorded fact (schemas are as immutable as
    the files), answered with zero network. Everything else probes live.

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
    from placeroot import manifest

    bundled = manifest.bundled_columns(glob)
    if bundled is not None:
        return bundled
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
