"""Discover the current Overture Maps release, with a pinned fallback.

Overture ships a new release roughly monthly under
s3://overturemaps-us-west-2/release/<release>/. We list that prefix via the
bucket's public HTTPS listing endpoint (anonymous, no SDK, no new deps) and
take the lexicographically greatest release name — Overture's release
naming (YYYY-MM-DD.N) sorts correctly as a plain string. Discovery is
best-effort: any failure (network, parsing, empty listing) falls back to a
pinned known-good release rather than failing the request.

Freshness (#219): the resolved release is cached with a TTL (default 6h,
PLACEROOT_RELEASE_TTL_HOURS) instead of for the process lifetime, so a
long-running server rolls over to a new Overture release without a restart.
The re-check is lazy — it happens on the first resolve after expiry, and
only one thread pays for it while others keep the previous answer — and a
FAILED re-check keeps the previously-resolved release rather than dropping
to the pin (a release that worked a moment ago beats a months-old pin).
Rollover mid-flight is safe for the caches (tile paths and support-table
paths embed the release, so a query can never read release A's files
through release B's path); a single query that resolves the release more
than once around a rollover boundary can do part of its work against each
release, which is two self-consistent reads, not corruption.

Staleness (#219): release names carry their date, so `age_days()` and
`is_stale()` (threshold PLACEROOT_STALE_RELEASE_DAYS, default 60) let
data_version and the logs say "this vintage is older than Overture's
cadence" instead of leaving the reader to know the cadence themselves.
"""

import datetime
import logging
import os
import re
import threading
import time
import urllib.request
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

# Known-good release, the fallback when discovery has never succeeded this
# process. Bumping it is a one-line change here, but the fixtures and docs
# pin the same string — grep for the old value and regenerate fixtures when
# moving it. The weekly overture-canary workflow (#219) compares this pin
# against upstream's newest release and probes the schema, so a stale pin
# or a breaking upstream change opens an issue instead of waiting for a
# bug report.
PINNED_RELEASE = "2026-07-22.0"

LISTING_URL = (
    "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
    "?list-type=2&prefix=release/&delimiter=/"
)

_RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def parse_listing(xml_text: str) -> list[str]:
    """Extract release names from an S3 ListObjectsV2 XML listing.

    Expects CommonPrefixes entries like "release/2026-07-22.0/" (produced by
    the delimiter=/ query param); returns just the release name portion,
    filtered to ones that look like a real release.
    """
    root = ElementTree.fromstring(xml_text)
    prefixes = root.findall("s3:CommonPrefixes/s3:Prefix", _S3_NS)
    releases = []
    for prefix in prefixes:
        name = (prefix.text or "").removeprefix("release/").rstrip("/")
        if _RELEASE_RE.match(name):
            releases.append(name)
    return releases


def _numeric_patch_key(r: str) -> tuple[str, int]:
    """Sort key for a release name: plain max() would rank "2026-07-22.9"
    above "2026-07-22.10" (string comparison), so compare the date portion
    as a string (it already sorts correctly) and the patch component as an
    int."""
    return (r[: r.rindex(".")], int(r[r.rindex(".") + 1 :]))


def _fetch_listing(timeout_s: float = 5.0) -> list[str] | None:
    """Best-effort: every live release name from the bucket listing, in
    whatever order parse_listing() returns them, or None on any failure.

    Shared by _discover() (wants the newest) and available_releases() (wants
    the whole list) so there is exactly one place that talks to S3.
    """
    try:
        with urllib.request.urlopen(LISTING_URL, timeout=timeout_s) as resp:  # noqa: S310
            xml_text = resp.read().decode("utf-8")
        releases = parse_listing(xml_text)
    except Exception as e:  # noqa: BLE001 - any failure here must fall back, never raise
        logger.warning("Overture release listing failed: %s", e)
        return None
    if not releases:
        logger.warning("Overture release listing found no releases")
        return None
    return releases


def _discover(timeout_s: float = 5.0) -> str | None:
    """Best-effort: newest release name, or None if discovery fails."""
    releases = _fetch_listing(timeout_s)
    if releases is None:
        logger.warning("Overture release discovery failed, using pinned release")
        return None
    return max(releases, key=_numeric_patch_key)


# available_releases()'s own cache — deliberately not sharing state with the
# resolve-release cache below. It answers a different question ("what
# releases exist") with none of resolve_release()'s wrinkles (no env
# override, no artifact-pin trade, no pin-first/background-discovery dance,
# no per-refresh generation bookkeeping), so folding it into that machinery
# would just make the harder cache answer for the simpler one. reset_cache()
# still clears both, so tests get one lever.
_releases_lock = threading.Lock()
_releases_cached: list[str] | None = None
_releases_cached_at: float = 0.0

DEFAULT_TTL_HOURS = 6.0
DEFAULT_STALE_DAYS = 60

# TTL cache state. _cached is the last successful resolution; _cached_at is
# monotonic so wall-clock jumps can't expire (or eternally refresh) it.
# _refreshing lets exactly one thread pay for an expired re-check while
# every other caller keeps getting the previous answer — a 5s discovery
# timeout must never stall the query path TTL-wide.
#
# _generation is bumped by reset_cache(). A refresher captures it before its
# (unlocked, slow) discovery and re-checks it before writing back, so a
# refresh that started before a reset can never overwrite — and re-stamp the
# TTL of — whatever resolved after that reset.
_lock = threading.Lock()
# Set when the process's first background discovery has finished (found
# something or not) — a deterministic join point for tests and warm-starts.
_first_discovery_done = threading.Event()
_cached: dict | None = None
_cached_at: float = 0.0
_refreshing = False
_generation = 0
_ttl_warned = False
_override_warned: str | None = None


def _ttl_s() -> float:
    """Cache TTL in seconds, from PLACEROOT_RELEASE_TTL_HOURS.

    A value <= 0 means "re-check on every resolve" — deliberate, and what the
    tests use to force a rollover without sleeping. It is a poor production
    setting, though: _upstream_glob() resolves the release for every query, so
    a non-positive TTL puts a discovery round-trip on the hot path. Warn the
    operator once rather than silently clamping, since the tests depend on the
    literal meaning.
    """
    global _ttl_warned
    try:
        ttl_s = float(os.environ.get("PLACEROOT_RELEASE_TTL_HOURS", DEFAULT_TTL_HOURS)) * 3600.0
    except ValueError:
        return DEFAULT_TTL_HOURS * 3600.0
    if ttl_s <= 0 and not _ttl_warned:
        _ttl_warned = True
        logger.warning(
            "PLACEROOT_RELEASE_TTL_HOURS is <= 0: the Overture release will be "
            "re-discovered on every resolve, which puts a network round-trip on "
            "the query path. Set it to a positive number of hours (default %g) "
            "unless you are testing rollover.",
            DEFAULT_TTL_HOURS,
        )
    return ttl_s


def _stale_days_threshold() -> int:
    """Staleness threshold in days, floored at 1 — a 0 or negative threshold
    would mark every release, including one published today, as stale."""
    try:
        return max(1, int(os.environ.get("PLACEROOT_STALE_RELEASE_DAYS", DEFAULT_STALE_DAYS)))
    except ValueError:
        return DEFAULT_STALE_DAYS


def age_days(release_name: str) -> int | None:
    """Whole days since the date carried in the release name, or None if the
    name doesn't parse (an env override is validated, but stay defensive)."""
    try:
        released = datetime.date.fromisoformat(release_name.rsplit(".", 1)[0])
    except ValueError:
        return None
    return max(0, (datetime.date.today() - released).days)


def is_stale(release_name: str) -> bool:
    """Older than the staleness threshold (default 60 days — Overture ships
    ~monthly, so two missed releases)."""
    days = age_days(release_name)
    return days is not None and days > _stale_days_threshold()


def _warn_if_stale(info: dict) -> None:
    """One log line per (re-)resolution when the vintage is suspect. Piggybacks
    on the TTL cadence, so a long-running stale server re-warns every TTL
    window rather than only once at startup (#219)."""
    days = age_days(info["release"])
    if days is None:
        return
    if is_stale(info["release"]):
        logger.warning(
            "active Overture release %s is %d days old (threshold %d) — "
            "%s; data served from an old vintage",
            info["release"], days, _stale_days_threshold(),
            "discovery keeps failing and the pinned fallback has gone stale"
            if info["source"] == "pinned-fallback"
            else "check upstream discovery and the deployment's egress",
        )


def _warn_if_stale_once(info: dict) -> None:
    """_warn_if_stale for the env-override path, which has no TTL to pace it.

    resolve_release_info() runs several times per query and the override path
    returns before touching the cache, so warn once per distinct override
    value instead of once per call.
    """
    global _override_warned
    if _override_warned == info["release"]:
        return
    _override_warned = info["release"]
    _warn_if_stale(info)


def bundled_artifact_release() -> str:
    """The release *every* bundled artifact set was built for.

    Read from the shipped files rather than assumed equal to PINNED_RELEASE,
    so a half-done pin bump (pin moved, artifacts not regenerated) is visible
    instead of silently claiming acceleration the wheel cannot deliver.

    All three sets have to agree. They are keyed by release independently —
    data/manifests/<release>/, data/geocode-index/<release>/,
    data/land-cover-grid/<release>.parquet — and regenerating only some of
    them is exactly the mistake a pin bump invites (three separate generator
    scripts, one release). Reporting the manifests' release alone would then
    tell an operator `artifacts: matched` while the geocode index silently
    missed, which is the specific lie this function exists to prevent. When
    they disagree, the oldest wins: that is the release the wheel can
    actually deliver every acceleration on.

    Falls back to the pin when the files can't be read at all — the pin is
    what the artifacts target by construction.
    """
    try:
        from importlib import resources

        data = resources.files("placeroot") / "data"
        per_set = [
            {p.name for p in (data / "manifests").iterdir() if _RELEASE_RE.match(p.name)},
            {p.name for p in (data / "geocode-index").iterdir() if _RELEASE_RE.match(p.name)},
            {
                p.name[: -len(".parquet")]
                for p in (data / "land-cover-grid").iterdir()
                if p.name.endswith(".parquet") and _RELEASE_RE.match(p.name[: -len(".parquet")])
            },
        ]
        complete = set.intersection(*per_set)
        if complete:
            return max(complete)
        logger.warning(
            "bundled artifacts disagree on release (%s); no release has all "
            "three sets, so cold queries lose some acceleration.",
            " vs ".join(",".join(sorted(s)) or "none" for s in per_set),
        )
    except (OSError, TypeError, ModuleNotFoundError):
        pass
    return PINNED_RELEASE


def _resolved(discovered: str | None) -> dict:
    """A resolution dict for a discovery result: the release, or the pin.

    #269: discovering a *newer* release than the wheel's artifacts is not a
    reason to adopt it immediately. Every acceleration this package ships —
    file manifests, the stage-0 geocode index, the coarse land-cover grid —
    is keyed by release and simply misses on any other one, taking cold
    queries from seconds back to the tens of seconds they used to cost. The
    miss is fail-safe (a mismatched artifact is never read, so answers stay
    correct) but it is silent, and it would land on every user roughly
    monthly, days after a release they did not ask for.

    So the artifact release wins while it is still current, and loses once
    it is stale (PLACEROOT_STALE_RELEASE_DAYS, default 60 — two missed
    Overture releases). That bounds the trade in both directions: nobody
    silently gets slow because upstream shipped yesterday, and nobody
    silently sits on a half-year-old vintage because they never upgraded
    the package. Past the bound, freshness wins and the log says why.
    """
    if not discovered:
        return {"release": PINNED_RELEASE, "source": "pinned-fallback"}
    artifacts = bundled_artifact_release()
    if discovered != artifacts and not is_stale(artifacts):
        logger.info(
            "Overture %s is available; staying on %s, which this build's "
            "bundled artifacts target (upgrade placeroot to move up, or set "
            "PLACEROOT_OVERTURE_RELEASE=%s to take the newer data now and "
            "give up the bundled acceleration).",
            discovered, artifacts, discovered,
        )
        return {
            "release": artifacts,
            "source": "artifact-pinned",
            "newer_release": discovered,
        }
    if discovered != artifacts:
        logger.warning(
            "Adopting Overture %s: this build's bundled artifacts target %s, "
            "which is now %s days old. Cold queries will be slower until "
            "placeroot is upgraded to a build whose artifacts match.",
            discovered, artifacts, age_days(artifacts),
        )
    return {"release": discovered, "source": "discovered"}


def resolve_release_info() -> dict:
    """Active release + how it was resolved: {"release": str, "source": ...}.

    source is one of: "env-override", "discovered", "pinned-fallback".
    Cached with a TTL (default 6h, PLACEROOT_RELEASE_TTL_HOURS) so a
    long-running server rolls over to a new Overture release without a
    restart. A failed re-check keeps the previously-resolved release —
    only a process that has never resolved anything falls to the pin.
    Call reset_cache() to force a re-check.
    """
    global _cached, _cached_at, _refreshing
    env_release = os.environ.get("PLACEROOT_OVERTURE_RELEASE")
    if env_release:
        # The release becomes a path/glob segment (…/release/<release>/…), so
        # validate the operator-supplied override against the same shape the
        # discovery path already enforces — a stray "../" or other junk here
        # would otherwise flow straight into an S3 glob. Not agent-reachable
        # today (no tool takes a release arg), but cheap defense in depth.
        if _RELEASE_RE.match(env_release):
            info = {"release": env_release, "source": "env-override"}
            # An operator can pin an ancient vintage too, and it is the case
            # least likely to be noticed (nothing is failing) — so this path
            # warns as well. Still zero-network: no discovery, no cache write.
            _warn_if_stale_once(info)
            return info
        logger.warning(
            "PLACEROOT_OVERTURE_RELEASE=%r doesn't look like a release "
            "(YYYY-MM-DD.N); ignoring it and falling back to discovery/pin.",
            env_release,
        )
    with _lock:
        now = time.monotonic()
        if _cached is not None and (now - _cached_at < _ttl_s() or _refreshing):
            return _cached
        if _cached is None:
            # First resolution of the process: answer with the pinned
            # release *immediately* and let discovery refresh in the
            # background. The pin is the release the wheel's bundled
            # manifests/schemas/index were built for, releases are
            # immutable, and blocking every cold first query on a network
            # round-trip (up to the 5s discovery timeout on a bad day) buys
            # nothing but latency — if discovery later finds a newer
            # release, the ordinary TTL machinery rolls the process over to
            # it exactly as it always did.
            _cached = {"release": PINNED_RELEASE, "source": "pinned-fallback"}
            # Deliberate freshness-for-latency trade: the first queries of
            # a fresh process may use the pinned release even when a newer
            # one exists, until the background discovery lands (seconds).
            # data_version reports source="pinned-fallback" for exactly
            # this window, so the state is visible, and every bundled
            # artifact (manifests, schemas, geocode index) targets the pin
            # — the release those queries are fastest and most complete on.
            # Stamped now, like any resolution: if the background discovery
            # succeeds it overwrites both the answer and the stamp; if it
            # finds nothing, the pin serves for a normal TTL rather than
            # every subsequent resolve re-attempting discovery inline.
            _cached_at = time.monotonic()
            _refreshing = True
            generation = _generation

            def _first_discovery():
                global _cached, _cached_at, _refreshing
                discovered = None
                try:
                    discovered = _discover()
                finally:
                    with _lock:
                        if generation != _generation:
                            # reset_cache() disowned this thread; a newer
                            # resolution owns _refreshing and the done
                            # event now — touching either would release
                            # the new discovery's claim and wake waiters
                            # on an answer that isn't theirs.
                            return
                        _refreshing = False
                        if discovered:
                            _cached = _resolved(discovered)
                            _cached_at = time.monotonic()
                            _warn_if_stale(_cached)
                        elif _cached is not None:
                            # Discovery failed and the pin serves: the
                            # stale-vintage warning the blocking path used
                            # to emit must still fire — a silently ancient
                            # pin is the least noticeable failure mode.
                            _warn_if_stale(_cached)
                    _first_discovery_done.set()

            _first_discovery_done.clear()
            threading.Thread(target=_first_discovery, daemon=True).start()
            return _cached
        # Expired and nobody else is refreshing: this thread refreshes outside
        # the lock; everyone else keeps the previous answer.
        _refreshing = True
        generation = _generation
        previous = _cached
    try:
        discovered = _discover()
    except BaseException:
        # _discover() swallows Exception, but not a KeyboardInterrupt landing
        # in its 5s urlopen. Releasing the claim here is what keeps that from
        # pinning _refreshing True — and with it the cache — for the rest of
        # the process's life.
        with _lock:
            if generation == _generation:
                _refreshing = False
        raise
    with _lock:
        if generation != _generation:
            # reset_cache() ran while we were discovering, and already cleared
            # _refreshing. Our answer predates the reset, so it must not
            # overwrite whatever resolved after it: that would regress the
            # cache to an older release and re-stamp its TTL. Hand our result
            # to this caller without installing it.
            return _cached if _cached is not None else _resolved(discovered)
        _refreshing = False
        if discovered:
            if discovered != previous["release"]:
                logger.info(
                    "Overture release rollover: %s -> %s (tile/table caches are "
                    "release-keyed; old-release files age out under the size cap)",
                    previous["release"], discovered,
                )
            _cached = {"release": discovered, "source": "discovered"}
        else:
            logger.warning(
                "Overture release re-check failed; keeping previously resolved "
                "release %s until the next check",
                previous["release"],
            )
            _cached = previous
        # Stamp even on failure: retry next TTL window, not on every query.
        _cached_at = time.monotonic()
        _warn_if_stale(_cached)
        return _cached


def resolve_release() -> str:
    """Active Overture release: env override, then discovery, then the pin.

    TTL-cached (see resolve_release_info) — call reset_cache() to force a
    re-check.
    """
    return resolve_release_info()["release"]


def available_releases() -> list[str]:
    """Every live release name, ascending (oldest first).

    For #309-style callers that need to query more than one release at once
    (overture.upstream_glob(..., release=...)) or just want to show the
    valid diff window (data_version). Best-effort like _discover(): a
    network failure, a parse error, or an empty listing all come back as []
    rather than raising — this is a convenience, never something the query
    path depends on to answer a request.

    TTL-cached (same PLACEROOT_RELEASE_TTL_HOURS as resolve_release()) but
    in its own cache variable — see the comment above _releases_lock for
    why this doesn't share resolve_release()'s cache/refresh machinery.
    Sorted with the same numeric-patch-aware comparison _discover() uses
    ("2026-07-22.9" must sort below "2026-07-22.10").
    """
    global _releases_cached, _releases_cached_at
    with _releases_lock:
        now = time.monotonic()
        if _releases_cached is not None and now - _releases_cached_at < _ttl_s():
            return list(_releases_cached)
    releases = _fetch_listing()
    if releases is None:
        return []
    ordered = sorted(releases, key=_numeric_patch_key)
    with _releases_lock:
        _releases_cached = ordered
        _releases_cached_at = time.monotonic()
    return list(ordered)


def reset_cache() -> None:
    """Clear the TTL cache. Used by tests and rare hot-reload.

    Bumping _generation disowns any refresh already in flight, so its result
    is discarded rather than written over the next resolution.
    """
    global _cached, _cached_at, _refreshing, _generation, _ttl_warned, _override_warned
    global _releases_cached, _releases_cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0
        _refreshing = False
        _generation += 1
        _ttl_warned = False
        _override_warned = None
    with _releases_lock:
        _releases_cached = None
        _releases_cached_at = 0.0
