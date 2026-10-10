"""Process startup for the PlaceRoot MCP server: the argv parser and main(), the background
warmers kicked off at launch, the warmup_city tile warm, and the cache-hint policy
advertised with the listing tools.

Moved verbatim out of server.py (no behaviour change). main() and the warm-start path reach
the server instance (mcp, BASE_INSTRUCTIONS) and the helpers tests patch (_warm_start,
_warm_metadata_async, _prewarm_region) through the placeroot.server module at call time.
The log channel stays "placeroot.server". server.py re-exports every name.
"""

import argparse
import logging
import os
import sys
import threading

from mcp.server.caching import CacheableMethod, CacheHint

from placeroot import (
    cache,
    db,
    geo,
    home_region,
    overture,
    progress,
    release,
)
from placeroot import geocode as geocoding

# Same log channel as when this lived in server.py: caplog and log config
# key off "placeroot.server", not this module's name.
logger = logging.getLogger("placeroot.server")


def _server():
    """The placeroot.server module, looked up at call time.

    main() and the warm-start path use the server instance and the helpers tests
    patch on placeroot.server, so they are reached through that module.
    """
    return sys.modules["placeroot.server"]


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8321


def _warmup_is_cached(lat: float, lon: float, radius_m: float) -> bool:
    """True if warmup would not COPY — cache off, or both themes already on disk."""
    if not cache.enabled():
        return True
    radius_m = min(max(float(radius_m), 0.0), MAX_WARMUP_RADIUS_M)
    bbox = geo.bbox_around(lat, lon, radius_m)
    rel = release.resolve_release()
    for theme, type_ in _WARMUP_THEMES:
        glob = overture.upstream_glob(theme=theme, type_=type_)
        if not cache.bbox_is_cached(rel, theme, bbox, glob):
            return False
    return True


DEFAULT_WARMUP_RADIUS_M = 8000.0
MAX_WARMUP_RADIUS_M = 25_000.0

# Themes the first real question typically hits. places first ("what's
# around downtown"); transportation tiles next (the routing graph is
# still built on the first route). Buildings stay out: a metro bbox
# fans into too many 0.0625° tiles.
_WARMUP_THEMES: tuple[tuple[str, str], ...] = (
    ("places", "place"),
    ("transportation", "segment"),
)


def _prewarm_region(lat: float, lon: float, radius_m: float) -> dict:
    """Materialize existing-cache tiles for a metro bbox.

    Shared by warmup_city, _warm_start, and autowarm (first city-scale
    resolve). Same cache.py tiles — no second cache, no extra remote API.
    Tiles are not a built street graph.
    """
    radius_m = min(max(float(radius_m), 0.0), MAX_WARMUP_RADIUS_M)
    if not cache.enabled():
        return {
            "lat": lat,
            "lon": lon,
            "radius_m": radius_m,
            "status": "cache_disabled",
            "themes": [],
            "note": (
                "The tile cache is off (PLACEROOT_CACHE=off), so there is "
                "nothing to pre-warm. Repeat queries will still hit upstream."
            ),
        }
    bbox = geo.bbox_around(lat, lon, radius_m)
    themes = []
    # Do not hold conn_lock across the warmup. prewarm_bbox COPYs run
    # on new_connection() cursors (the same path background fetches
    # use), so other tools can keep answering between tiles/themes.
    # Holding the lock here was a server-wide stall at 25 km.
    for theme, type_ in _WARMUP_THEMES:
        with db.conn_lock:
            con = db.shared_conn()
        glob = overture.upstream_glob(theme=theme, type_=type_)
        themes.append(
            cache.prewarm_bbox(
                con,
                release.resolve_release(),
                theme,
                bbox,
                glob,
                db.new_connection,
            )
        )
    statuses = {row["status"] for row in themes}
    graph = progress.format_eta(*progress.GRAPH_BUILD_S)
    coverage = (
        "Places and transportation tiles are cached; buildings are not. "
        f"The first route still builds the street graph ({graph})."
    )
    if statuses <= {"already_warm"}:
        status = "already_warm"
        note = (
            f"This area is already cached. Later place searches over it should be fast. {coverage}"
        )
    elif "partial" in statuses and not (statuses & {"upstream_unavailable", "too_large"}):
        status = "partial"
        note = (
            "Some tiles for this area are cached; a heavy theme stopped "
            "at the inline-tile cap so warmup would not monopolize the "
            f"server. {coverage}"
        )
    elif statuses <= {"warmed", "already_warm"}:
        status = "warmed"
        note = (
            "Places and transportation tiles for this area are cached. "
            "Place searches over this city should now be fast. "
            f"{coverage}"
        )
    elif "upstream_unavailable" in statuses and statuses & {
        "warmed",
        "already_warm",
        "partial",
    }:
        status = "partial"
        note = "Some themes cached; others could not reach upstream."
    elif "too_large" in statuses:
        status = "too_large"
        note = "The radius covers too many tiles; try a smaller radius_m."
    else:
        status = "failed"
        note = "Warmup could not cache this area; the next query will scan upstream."
    return {
        "lat": lat,
        "lon": lon,
        "radius_m": radius_m,
        "status": status,
        "themes": themes,
        "note": note,
    }


# MCP 2026-07-28 caching hints (SEP-2549). The spec requires a `ttlMs` and a
# `cacheScope` on every `resultType: "complete"` listing result; the SDK's
# default is ttlMs=0 ("immediately stale"), which is valid but throws away the
# whole point for a server whose listings are frozen at build time.
#
# Why 24 hours: our listings are a pure function of the installed placeroot
# version and PLACEROOT_TOOLS. Nothing at runtime can change them — no tool is
# registered after startup, and we never send notifications/tools/list_changed
# — so the only event that invalidates a cached listing is the operator
# upgrading the package. TTL is therefore a bound on how long a client could
# keep showing a pre-upgrade tool list, and one day is the honest trade: it
# spares a re-fetch of a ~33k-token schema surface on every session within a
# day, while an upgrade is visible by the next one. A week would buy almost
# nothing extra (sessions cluster well inside a day) for seven times the
# staleness window; 0 is what we'd declare if the surface could move at
# runtime, and it can't.
#
# Why "public": these listings carry no caller-specific data. PlaceRoot is
# keyless, does no per-caller filtering, and returns the same bytes to every
# request on a given process, so a shared gateway may serve one caller's copy
# to another.
#
# Two of the six cacheable methods are deliberately left at the SDK default
# (ttlMs=0/private), for the same reason: their bodies carry the resolved
# Overture release, which is discovered from S3 at process start rather than
# baked into the build, so a day-long shared cache could outlive the value.
#   `resources/read` — placeroot://data-version reports the release directly.
#   `server/discover` — its DiscoverResult carries `instructions`, and main()
#       appends "Backed by Overture Maps release {release}." to those at
#       startup (the SDK's default handler reads them at call time). A 24h
#       public entry would keep serving the pre-restart release string — to
#       other callers too, under "public" — after an operator restarts onto a
#       new Overture release, and that string is model-visible grounding.
_LISTING_TTL_MS = 24 * 60 * 60 * 1000
_LISTING_CACHE_HINT = CacheHint(ttl_ms=_LISTING_TTL_MS, scope="public")
CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": _LISTING_CACHE_HINT,
    "prompts/list": _LISTING_CACHE_HINT,
    "resources/list": _LISTING_CACHE_HINT,
    "resources/templates/list": _LISTING_CACHE_HINT,
}


def _warm_start() -> None:
    """Best-effort cache pre-warm for PLACEROOT_WARM_REGION. Never blocks or raises.

    "Never blocks" refers to startup not being able to hang or crash on
    this — the call itself is synchronous (cache.prewarm_bbox force_sync),
    since this already only runs once, at startup, specifically to
    materialize the home region's tiles before real traffic arrives.
    """
    spec = os.environ.get("PLACEROOT_WARM_REGION")
    if not spec or not cache.enabled():
        return
    parsed = cache.parse_warm_region(spec)
    if parsed is None:
        logger.warning("PLACEROOT_WARM_REGION=%r is malformed, expected 'lat,lon,radius_m'", spec)
        return
    lat, lon, radius_m = parsed
    try:
        _server()._prewarm_region(lat, lon, radius_m)
    except Exception as e:  # noqa: BLE001 - warm-on-start must never break startup
        logger.warning("PLACEROOT_WARM_REGION pre-warm failed (continuing): %s", e)


def _warm_metadata_async() -> None:
    """Kick off the shared connection's parquet-metadata pre-warm (issue #31)
    on a daemon thread so it doesn't delay startup, only the first query.
    """
    threading.Thread(target=overture.warm_metadata, daemon=True).start()


def _warm_divisions() -> None:
    """Thread target for _warm_divisions_async: build (or reuse) the #43
    local divisions name table, logging and swallowing anything that goes
    wrong rather than letting it become an unhandled exception on a daemon
    thread. geocode._local_divisions_table() already logs and degrades
    internally for the failure modes it recognizes (duckdb.Error,
    UpstreamUnavailable); this is a last-resort backstop for anything else.
    """
    try:
        geocoding._local_divisions_table()
    except Exception as e:  # noqa: BLE001 - warm-on-start must never break startup
        logger.warning("divisions-table pre-warm failed (continuing): %s", e)


def _warm_divisions_async() -> None:
    """Kick off geocode.py's #43 local divisions name-table materialization
    (issue #93) on a daemon thread at startup, mirroring
    _warm_metadata_async — so the ~20-30s one-time build (cold extension
    load plus a full COPY of the divisions theme) is already done, or at
    least underway, before the first real geocode()/resolve_place() call
    pays for it silently.

    A no-op when caching is off (PLACEROOT_CACHE=off) — checked here,
    before a thread is even spawned, rather than relying on
    _local_divisions_table's own cache.enabled() check, so this function's
    behavior is visible without reading into geocode.py.
    """
    if not cache.enabled():
        return
    threading.Thread(target=_warm_divisions, daemon=True).start()


def _warm_home_async() -> None:
    """Kick off #406's home-region resolution (PLACEROOT_HOME today; MCP
    roots stubbed, see home_region.resolve_home_from_roots) on a daemon
    thread at startup, mirroring _warm_divisions_async.

    Resolving here — rather than waiting for the first geocode/resolve_place
    call to do it lazily — both warms geocode.py's ranking bias ahead of
    real traffic and, when it resolves, schedules the same background tile
    warm a city-scale resolve gets (autowarm.py). Never blocks startup;
    schedule_autowarm itself already no-ops when PLACEROOT_CACHE=off, so no
    extra gating is needed here.
    """
    threading.Thread(target=home_region.kick_home_autowarm, daemon=True).start()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="placeroot",
        description="PlaceRoot MCP server — ground AI agents in open map data.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve the streamable-HTTP transport instead of stdio (the default).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HTTP_HOST,
        help=f"Host to bind in --http mode (default: {DEFAULT_HTTP_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Port to bind in --http mode (default: {DEFAULT_HTTP_PORT}).",
    )
    return parser


def parse_transport_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args into transport config (mode/host/port). Extracted from
    main() so the mode-selection logic is directly unit-testable without
    starting a server.
    """
    return _build_arg_parser().parse_args(argv)


def main() -> None:
    args = parse_transport_args()
    active_release = release.resolve_release()
    # MCPServer.instructions is a read-only property over the low-level
    # server, which is what the initialize response actually reads from.
    _server().mcp._lowlevel_server.instructions = (
        f"{_server().BASE_INSTRUCTIONS} Backed by Overture Maps release {active_release}."
    )
    _server()._warm_metadata_async()
    _warm_divisions_async()
    _server()._warm_start()
    _warm_home_async()
    if args.http:
        logger.info("placeroot: streamable-HTTP on http://%s:%s/mcp", args.host, args.port)
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            # The SDK only auto-enables DNS-rebinding/Origin protection for the
            # loopback literals, and placeroot configures no authentication —
            # so a non-loopback bind exposes every tool, unauthenticated, to
            # anyone who can reach this host:port. Warn loudly; the operator
            # must front it with a reverse proxy / auth layer (see README).
            logger.warning(
                "placeroot is bound to a NON-LOOPBACK host (%s) with NO "
                "authentication — every tool is exposed to anyone who can reach "
                "%s:%s. Put a reverse proxy / auth layer in front of it before "
                "using this beyond a trusted local network.",
                args.host,
                args.host,
                args.port,
            )
        _server().mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        _server().mcp.run()
