"""Shared DuckDB connection management for the query layer (issue #40).

overture.py and routing.py each used to configure and hold their own
DuckDB connection/lock/schema-probe (fully separate — routing.py's split
predated, and missed, issue #31's connection-factory fix), and
divisions.py/buildings.py each re-implemented "load spatial once" locally
(buildings.py's copy skipped the connection lock — a real bug the HTTP
concurrency audit missed). This is now the one place that configures a
connection (httpfs, object cache, S3 timeouts/retries) and loads spatial.

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
from functools import lru_cache

import duckdb

logger = logging.getLogger(__name__)

# Guards every use of shared_conn(). See the module docstring above.
conn_lock = threading.Lock()

_spatial_loaded = False



# Region the public Overture bucket lives in — the default unless
# PLACEROOT_S3_REGION overrides it (issue #20's switchover: a mirror on a
# different S3-compatible service almost always has its own region name).
DEFAULT_S3_REGION = "us-west-2"


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
    con.execute(f"SET s3_region='{_s3_region()}';")
    endpoint = _s3_endpoint()
    if endpoint:
        # A mirror target (issue #20): R2/minio/self-hosted S3 generally
        # expect path-style addressing and, unlike the public Overture
        # bucket, are usually private — read credentials from the
        # environment if the operator set them, anonymous otherwise.
        con.execute(f"SET s3_endpoint='{endpoint}';")
        con.execute("SET s3_url_style='path';")
        access_key = os.environ.get("PLACEROOT_S3_ACCESS_KEY_ID", "")
        secret_key = os.environ.get("PLACEROOT_S3_SECRET_ACCESS_KEY", "")
        con.execute(f"SET s3_access_key_id='{access_key}';")
        con.execute(f"SET s3_secret_access_key='{secret_key}';")
    else:
        con.execute("SET s3_access_key_id='';")  # public bucket: anonymous access
        con.execute("SET s3_secret_access_key='';")
    # Caches parquet footer/metadata per connection (issue #31): a repeat
    # query against the same file on the same connection skips the ~5s cold
    # footer-read cost. Combined with overture.warm_metadata's startup
    # pre-warm, this is often already paid before a real query arrives.
    con.execute("SET enable_object_cache=true;")
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
    """A fresh, independently-configured connection for background work.

    Background tile materialization (cache.py) and one-off local-table
    builds (geocode.py's #43 divisions table) must not share shared_conn()
    with a main-thread query — DuckDB connections aren't safe for
    concurrent use. Pass this (uncalled) as a connection factory.
    """
    return _configure(duckdb.connect())


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
def probe_schema(glob: str) -> frozenset | None:
    """Column names present in glob's dataset, or None if the probe itself failed.

    A failed probe (upstream down, glob unreadable) is treated as "unknown,
    assume nothing missing" — the actual query that follows hits the same
    problem and surfaces it as UpstreamUnavailable instead.
    """
    try:
        with conn_lock:
            cols = shared_conn().execute(
                f"SELECT * FROM read_parquet('{glob}') LIMIT 0"
            ).description
        return frozenset(c[0] for c in cols)
    except duckdb.Error as e:
        logger.warning("Schema probe failed for %s: %s", glob, e)
        return None
