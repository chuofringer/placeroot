"""Discover the current Overture Maps release, with a pinned fallback.

Overture ships a new release roughly monthly under
s3://overturemaps-us-west-2/release/<release>/. We list that prefix via the
bucket's public HTTPS listing endpoint (anonymous, no SDK, no new deps) and
take the lexicographically greatest release name — Overture's release
naming (YYYY-MM-DD.N) sorts correctly as a plain string. Discovery is
best-effort and cached for the process lifetime: any failure (network,
parsing, empty listing) falls back to a pinned known-good release rather
than failing the request.
"""

import logging
import os
import re
import urllib.request
from functools import lru_cache
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

# Known-good release, updated occasionally by hand. Discovery failures (and
# tests) fall back to this rather than ever failing closed.
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


def _discover(timeout_s: float = 5.0) -> str | None:
    """Best-effort: newest release name, or None if discovery fails."""
    try:
        with urllib.request.urlopen(LISTING_URL, timeout=timeout_s) as resp:  # noqa: S310
            xml_text = resp.read().decode("utf-8")
        releases = parse_listing(xml_text)
    except Exception as e:  # noqa: BLE001 - any failure here must fall back, never raise
        logger.warning("Overture release discovery failed, using pinned release: %s", e)
        return None
    if not releases:
        logger.warning("Overture release discovery found no releases, using pinned release")
        return None
    # Plain max() would sort "2026-07-22.9" above "2026-07-22.10"; compare
    # the patch component numerically.
    return max(releases, key=lambda r: (r[: r.rindex(".")], int(r[r.rindex(".") + 1 :])))


@lru_cache(maxsize=1)
def resolve_release() -> str:
    """Active Overture release: env override, then discovery, then the pin.

    Cached for the process lifetime (discovery is a network call we don't
    want repeated on every query) — call reset_cache() to force a re-check.
    """
    env_release = os.environ.get("PLACEROOT_OVERTURE_RELEASE")
    if env_release:
        # The release becomes a path/glob segment (…/release/<release>/…), so
        # validate the operator-supplied override against the same shape the
        # discovery path already enforces — a stray "../" or other junk here
        # would otherwise flow straight into an S3 glob. Not agent-reachable
        # today (no tool takes a release arg), but cheap defense in depth.
        if _RELEASE_RE.match(env_release):
            return env_release
        logger.warning(
            "PLACEROOT_OVERTURE_RELEASE=%r doesn't look like a release "
            "(YYYY-MM-DD.N); ignoring it and falling back to discovery/pin.",
            env_release,
        )
    return _discover() or PINNED_RELEASE


def reset_cache() -> None:
    """Clear the process-lifetime cache. Used by tests and rare hot-reload."""
    resolve_release.cache_clear()
