import datetime
import logging
import threading

import pytest

from placeroot import release

CANNED_LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>overturemaps-us-west-2</Name>
    <Prefix>release/</Prefix>
    <Delimiter>/</Delimiter>
    <CommonPrefixes>
        <Prefix>release/2026-05-21.0/</Prefix>
    </CommonPrefixes>
    <CommonPrefixes>
        <Prefix>release/2026-06-25.0/</Prefix>
    </CommonPrefixes>
    <CommonPrefixes>
        <Prefix>release/2026-07-22.0/</Prefix>
    </CommonPrefixes>
    <CommonPrefixes>
        <Prefix>release/2026-07-22.1/</Prefix>
    </CommonPrefixes>
</ListBucketResult>
"""


@pytest.fixture(autouse=True)
def clear_cache():
    release.reset_cache()
    yield
    release.reset_cache()


def test_parse_listing_extracts_release_names_in_document_order():
    assert release.parse_listing(CANNED_LISTING) == [
        "2026-05-21.0", "2026-06-25.0", "2026-07-22.0", "2026-07-22.1",
    ]


def test_parse_listing_ignores_non_release_prefixes():
    xml = """<?xml version="1.0"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <CommonPrefixes><Prefix>release/README/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>release/2026-07-22.0/</Prefix></CommonPrefixes>
    </ListBucketResult>
    """
    assert release.parse_listing(xml) == ["2026-07-22.0"]


def test_discover_picks_newest_release_lexicographically(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return CANNED_LISTING.encode("utf-8")

    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert release._discover() == "2026-07-22.1"


def test_discover_falls_back_to_none_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("network unreachable")

    monkeypatch.setattr(release.urllib.request, "urlopen", boom)
    assert release._discover() is None


def test_discover_falls_back_to_none_on_empty_listing(monkeypatch):
    empty = """<?xml version="1.0"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></ListBucketResult>
    """

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return empty.encode("utf-8")

    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert release._discover() is None


def test_resolve_release_env_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", "1999-01-01.0")
    monkeypatch.setattr(release, "_discover", lambda: "2026-07-22.1")
    assert release.resolve_release() == "1999-01-01.0"


def test_resolve_release_falls_back_to_pin_on_discovery_error(monkeypatch):
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: None)
    assert release.resolve_release() == release.PINNED_RELEASE


def test_resolve_release_uses_discovery_when_no_override(monkeypatch):
    """Pin-first: the very first resolve answers with the pin immediately
    (no network on the cold path); the background discovery's answer is
    served from the next resolve on."""
    # These exercise the TTL/caching machinery, not #269's artifact rule:
    # stale artifacts mean discovery is adopted, which is what they assert.
    monkeypatch.setattr(release, "bundled_artifact_release", lambda: "2000-01-01.0")
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: "2026-08-01.0")
    assert release.resolve_release() == release.PINNED_RELEASE
    assert release._first_discovery_done.wait(2)
    assert release.resolve_release() == "2026-08-01.0"


def test_resolve_release_info_env_override(monkeypatch):
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", "1999-01-01.0")
    monkeypatch.setattr(release, "_discover", lambda: "2026-07-22.1")
    assert release.resolve_release_info() == {
        "release": "1999-01-01.0", "source": "env-override",
    }
    # resolve_release() must delegate to the same resolution, not re-derive.
    assert release.resolve_release() == "1999-01-01.0"


def test_resolve_release_info_discovered(monkeypatch):
    # These exercise the TTL/caching machinery, not #269's artifact rule:
    # stale artifacts mean discovery is adopted, which is what they assert.
    monkeypatch.setattr(release, "bundled_artifact_release", lambda: "2000-01-01.0")
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: "2026-08-01.0")
    release.resolve_release_info()  # pin-first; kicks background discovery
    assert release._first_discovery_done.wait(2)
    assert release.resolve_release_info() == {
        "release": "2026-08-01.0", "source": "discovered",
    }
    assert release.resolve_release() == "2026-08-01.0"


def test_resolve_release_info_pinned_fallback(monkeypatch):
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: None)
    assert release.resolve_release_info() == {
        "release": release.PINNED_RELEASE, "source": "pinned-fallback",
    }
    assert release.resolve_release() == release.PINNED_RELEASE


def test_discover_compares_patch_numerically(monkeypatch):
    """Regression: plain max() would rank 2026-07-22.9 above 2026-07-22.10."""
    listing = """<?xml version="1.0"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <CommonPrefixes><Prefix>release/2026-07-22.9/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>release/2026-07-22.10/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>release/2026-06-25.0/</Prefix></CommonPrefixes>
    </ListBucketResult>
    """

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return listing.encode("utf-8")

    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert release._discover() == "2026-07-22.10"


# --- #219: TTL re-discovery -------------------------------------------------


def test_resolve_is_cached_within_the_ttl(monkeypatch):
    # These exercise the TTL/caching machinery, not #269's artifact rule:
    # stale artifacts mean discovery is adopted, which is what they assert.
    monkeypatch.setattr(release, "bundled_artifact_release", lambda: "2000-01-01.0")
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.delenv("PLACEROOT_RELEASE_TTL_HOURS", raising=False)
    calls = []
    monkeypatch.setattr(release, "_discover", lambda: calls.append(1) or "2026-08-01.0")
    release.resolve_release()  # pin-first; background discovery runs once
    assert release._first_discovery_done.wait(2)
    assert release.resolve_release() == "2026-08-01.0"
    assert release.resolve_release() == "2026-08-01.0"
    assert len(calls) == 1  # later resolves served from the TTL cache


def test_expired_ttl_rolls_over_to_the_new_release(monkeypatch):
    # These exercise the TTL/caching machinery, not #269's artifact rule:
    # stale artifacts mean discovery is adopted, which is what they assert.
    monkeypatch.setattr(release, "bundled_artifact_release", lambda: "2000-01-01.0")
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "0")
    releases = iter(["2026-08-01.0", "2026-08-20.0"])
    monkeypatch.setattr(release, "_discover", lambda: next(releases))
    release.resolve_release()  # pin-first; background discovery -> 2026-08-01.0
    assert release._first_discovery_done.wait(2)
    # TTL 0: the next resolve re-checks and rolls straight to the newer one.
    assert release.resolve_release() == "2026-08-20.0"


def test_failed_recheck_keeps_the_previous_release_not_the_pin(monkeypatch):
    # These exercise the TTL/caching machinery, not #269's artifact rule:
    # stale artifacts mean discovery is adopted, which is what they assert.
    monkeypatch.setattr(release, "bundled_artifact_release", lambda: "2000-01-01.0")
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "0")
    answers = iter(["2026-08-01.0", None, None])
    monkeypatch.setattr(release, "_discover", lambda: next(answers))
    release.resolve_release_info()  # pin-first
    assert release._first_discovery_done.wait(2)
    assert release.resolve_release_info() == {
        "release": "2026-08-01.0", "source": "discovered",
    }
    # Discovery now fails; a release that worked a moment ago beats the pin.
    info = release.resolve_release_info()
    assert info["release"] == "2026-08-01.0"
    assert info["source"] == "discovered"


def test_first_resolution_failure_still_falls_back_to_pin(monkeypatch):
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "0")
    monkeypatch.setattr(release, "_discover", lambda: None)
    assert release.resolve_release_info()["source"] == "pinned-fallback"


def test_garbage_ttl_env_uses_the_default(monkeypatch):
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "not-a-number")
    assert release._ttl_s() == release.DEFAULT_TTL_HOURS * 3600.0


def test_non_positive_ttl_re_checks_every_call_and_warns_once(monkeypatch, caplog):
    """TTL <= 0 keeps its literal meaning (the rollover tests rely on it), but
    it belongs nowhere near production, so the operator hears about it once."""
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "0")
    with caplog.at_level(logging.WARNING, logger="placeroot.release"):
        assert release._ttl_s() == 0.0
        assert release._ttl_s() == 0.0
    warned = "PLACEROOT_RELEASE_TTL_HOURS is <= 0"
    assert len([r for r in caplog.records if warned in r.getMessage()]) == 1


# --- #219: concurrent re-discovery ------------------------------------------


def _seed(monkeypatch, release_name):
    """Put a resolved release in the TTL cache, with the TTL already expired.

    Pin-first: the process's first resolve answers the pin and discovers in
    the background, so seeding waits for that discovery, then resolves once
    more (TTL 0) to confirm the seeded answer is what the cache serves."""
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "0")
    monkeypatch.setattr(release, "_discover", lambda: release_name)
    release.resolve_release()  # pin-first; kicks background discovery
    assert release._first_discovery_done.wait(2)
    assert release.resolve_release() == release_name


def _parked_discover():
    """A _discover that blocks until released, so a refresh can be held mid-flight.

    Returns (discover, started, finish): `started` is set once the refresher
    is inside the call, `finish` releases it.
    """
    started, finish = threading.Event(), threading.Event()

    def discover():
        started.set()
        assert finish.wait(10), "test deadlock: parked _discover never released"
        return "2026-08-20.0"

    return discover, started, finish


def test_one_thread_refreshes_while_others_keep_the_previous_answer(monkeypatch):
    """The whole point of _refreshing: a slow re-check must not queue every
    other caller behind a network round-trip for the length of the timeout."""
    _seed(monkeypatch, "2026-07-22.0")
    discover, started, finish = _parked_discover()
    monkeypatch.setattr(release, "_discover", discover)

    refresher = threading.Thread(target=release.resolve_release)
    refresher.start()
    assert started.wait(10), "refresher never entered discovery"

    # The refresher is parked in _discover. A second caller must be served the
    # previous answer promptly; without the _refreshing guard it would start
    # its own discovery and block here instead.
    answers = []
    other = threading.Thread(target=lambda: answers.append(release.resolve_release()))
    other.start()
    other.join(3)
    assert not other.is_alive(), "a second caller blocked behind the in-flight refresh"
    assert answers == ["2026-07-22.0"]

    finish.set()
    refresher.join(10)
    assert release._cached == {"release": "2026-08-20.0", "source": "discovered"}


def test_a_refresh_that_outlives_reset_cache_does_not_clobber_the_newer_answer(monkeypatch):
    """reset_cache() disowns an in-flight refresh: the pre-reset answer is
    older than whatever resolved after the reset, and must neither replace it
    nor re-stamp its TTL."""
    _seed(monkeypatch, "2026-01-01.0")
    discover, started, finish = _parked_discover()  # would resolve 2026-08-20.0
    monkeypatch.setattr(release, "_discover", discover)

    refresher = threading.Thread(target=release.resolve_release)
    refresher.start()
    assert started.wait(10), "refresher never entered discovery"

    release.reset_cache()
    monkeypatch.setattr(release, "_discover", lambda: "2026-09-09.0")
    # Post-reset the cache is empty again, so this resolve is pin-first too;
    # its background discovery finds 2026-09-09.0.
    assert release.resolve_release() == release.PINNED_RELEASE
    assert release._first_discovery_done.wait(2)
    assert release.resolve_release() == "2026-09-09.0"

    finish.set()
    refresher.join(10)
    assert release._cached == {"release": "2026-09-09.0", "source": "discovered"}
    assert release._refreshing is False


def test_an_exception_escaping_discovery_does_not_freeze_the_cache(monkeypatch):
    """_discover swallows Exception but not KeyboardInterrupt; without the
    try/except the escaping one would leave _refreshing set, and the cache
    would silently never re-check again for the life of the process."""
    _seed(monkeypatch, "2026-01-01.0")

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(release, "_discover", interrupted)
    with pytest.raises(KeyboardInterrupt):
        release.resolve_release()
    assert release._refreshing is False

    monkeypatch.setattr(release, "_discover", lambda: "2026-09-09.0")
    assert release.resolve_release() == "2026-09-09.0"


# --- #219: staleness --------------------------------------------------------


def test_age_days_reads_the_date_in_the_release_name():
    old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    assert release.age_days(f"{old}.0") == 90
    assert release.age_days("garbage") is None


def test_is_stale_past_the_threshold(monkeypatch):
    monkeypatch.delenv("PLACEROOT_STALE_RELEASE_DAYS", raising=False)
    fresh = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    assert release.is_stale(f"{fresh}.0") is False
    assert release.is_stale(f"{old}.0") is True
    monkeypatch.setenv("PLACEROOT_STALE_RELEASE_DAYS", "100")
    assert release.is_stale(f"{old}.0") is False


def test_stale_days_env_is_floored_at_one_day(monkeypatch):
    """0 or negative would mark a release published today as stale."""
    monkeypatch.setenv("PLACEROOT_STALE_RELEASE_DAYS", "not-a-number")
    assert release._stale_days_threshold() == release.DEFAULT_STALE_DAYS
    monkeypatch.setenv("PLACEROOT_STALE_RELEASE_DAYS", "0")
    assert release._stale_days_threshold() == 1
    monkeypatch.setenv("PLACEROOT_STALE_RELEASE_DAYS", "-5")
    assert release._stale_days_threshold() == 1
    assert release.is_stale(datetime.date.today().isoformat() + ".0") is False


def test_env_override_warns_about_a_stale_vintage_once_and_never_dials_out(monkeypatch, caplog):
    """An operator-pinned ancient release is the least visible way to serve old
    data — nothing fails — so the override path warns too. It stays
    zero-network, and warns once per value rather than once per query."""
    old = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", f"{old}.0")

    def no_network():
        raise AssertionError("the env override must not trigger discovery")

    monkeypatch.setattr(release, "_discover", no_network)
    with caplog.at_level(logging.WARNING, logger="placeroot.release"):
        assert release.resolve_release() == f"{old}.0"
        assert release.resolve_release() == f"{old}.0"
    stale_warnings = [r for r in caplog.records if "days old" in r.getMessage()]
    assert len(stale_warnings) == 1
    assert "400 days old" in stale_warnings[0].getMessage()


def test_data_version_payload_carries_age_and_stale_flag(monkeypatch):
    from placeroot import resources

    old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", f"{old}.0")
    payload = resources.data_version_payload()
    assert payload["age_days"] == 90
    assert payload["stale"] is True
    fresh = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", f"{fresh}.0")
    release.reset_cache()
    payload = resources.data_version_payload()
    assert payload["age_days"] == 5
    assert "stale" not in payload


# --- #269: the artifact release wins until it goes stale --------------------


def test_a_newer_release_is_reported_not_adopted_while_artifacts_are_current(
    monkeypatch,
):
    """Every acceleration this package ships is keyed by release and misses
    on any other one, so adopting a release the wheel has no artifacts for
    takes cold queries from seconds to tens of seconds — silently, and
    roughly monthly. The newer release is surfaced instead of taken."""
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "bundled_artifact_release", lambda: "2026-07-22.0")
    monkeypatch.setattr(release, "age_days", lambda name: 3)
    monkeypatch.setattr(release, "_discover", lambda: "2026-08-19.0")

    release.resolve_release()  # pin-first; kicks the background discovery
    assert release._first_discovery_done.wait(2)
    info = release.resolve_release_info()

    assert info["release"] == "2026-07-22.0"
    assert info["source"] == "artifact-pinned"
    assert info["newer_release"] == "2026-08-19.0"


def test_a_stale_artifact_release_gives_way_to_fresher_data(monkeypatch):
    """The trade is bounded in both directions: nobody sits on a half-year-old
    vintage because they never upgraded the package."""
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "bundled_artifact_release", lambda: "2020-01-01.0")
    monkeypatch.setattr(release, "_discover", lambda: "2026-08-19.0")

    release.resolve_release()
    assert release._first_discovery_done.wait(2)
    info = release.resolve_release_info()

    assert info["release"] == "2026-08-19.0"
    assert info["source"] == "discovered"
    assert "newer_release" not in info


def test_matching_discovery_is_plain_discovered(monkeypatch):
    """No artifact wrinkle when upstream and the wheel agree."""
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "bundled_artifact_release", lambda: "2026-08-19.0")
    monkeypatch.setattr(release, "_discover", lambda: "2026-08-19.0")

    release.resolve_release()
    assert release._first_discovery_done.wait(2)
    info = release.resolve_release_info()

    assert info == {"release": "2026-08-19.0", "source": "discovered"}


def test_bundled_artifact_release_is_read_from_the_shipped_files():
    """Read, not assumed equal to the pin, so a half-done pin bump (pin moved,
    artifacts not regenerated) is visible rather than silently claiming
    acceleration the wheel cannot deliver."""
    assert release.bundled_artifact_release() == release.PINNED_RELEASE


# --- #375: available_releases() ---------------------------------------------


def test_available_releases_parses_the_listing_and_sorts_ascending(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return CANNED_LISTING.encode("utf-8")

    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert release.available_releases() == [
        "2026-05-21.0",
        "2026-06-25.0",
        "2026-07-22.0",
        "2026-07-22.1",
    ]


def test_available_releases_sorts_patch_component_numerically(monkeypatch):
    """Regression: plain string sort would rank "2026-07-22.9" above
    "2026-07-22.10"."""
    listing = """<?xml version="1.0"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <CommonPrefixes><Prefix>release/2026-07-22.10/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>release/2026-07-22.9/</Prefix></CommonPrefixes>
        <CommonPrefixes><Prefix>release/2026-06-25.0/</Prefix></CommonPrefixes>
    </ListBucketResult>
    """

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return listing.encode("utf-8")

    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert release.available_releases() == ["2026-06-25.0", "2026-07-22.9", "2026-07-22.10"]


def test_available_releases_returns_empty_list_on_network_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("network unreachable")

    monkeypatch.setattr(release.urllib.request, "urlopen", boom)
    assert release.available_releases() == []


def test_available_releases_returns_empty_list_on_empty_listing(monkeypatch):
    empty = """<?xml version="1.0"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></ListBucketResult>
    """

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return empty.encode("utf-8")

    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert release.available_releases() == []


def test_available_releases_is_cached_within_the_ttl(monkeypatch):
    monkeypatch.delenv("PLACEROOT_RELEASE_TTL_HOURS", raising=False)
    calls = []

    def fake_urlopen(*a, **k):
        calls.append(1)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return CANNED_LISTING.encode("utf-8")

        return FakeResponse()

    monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)
    first = release.available_releases()
    second = release.available_releases()
    assert (
        first
        == second
        == [
            "2026-05-21.0",
            "2026-06-25.0",
            "2026-07-22.0",
            "2026-07-22.1",
        ]
    )
    assert len(calls) == 1  # second call served from the TTL cache


def test_reset_cache_clears_the_available_releases_cache(monkeypatch):
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "6")
    calls = []

    def fake_urlopen(*a, **k):
        calls.append(1)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return CANNED_LISTING.encode("utf-8")

        return FakeResponse()

    monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)
    release.available_releases()
    release.reset_cache()
    release.available_releases()
    assert len(calls) == 2  # reset_cache() forced a re-fetch, not served from cache


def test_bundled_artifact_release_requires_every_set_to_agree(monkeypatch, tmp_path):
    """A pin bump means running three separate generator scripts. Doing two of
    them must not report the third's acceleration as present: the release
    every set covers is the one the wheel can actually deliver."""
    data = tmp_path / "data"
    (data / "manifests" / "2026-09-24.0").mkdir(parents=True)
    (data / "manifests" / "2026-07-22.0").mkdir(parents=True)
    (data / "geocode-index" / "2026-07-22.0").mkdir(parents=True)
    (data / "land-cover-grid").mkdir(parents=True)
    (data / "land-cover-grid" / "2026-07-22.0.parquet").write_bytes(b"")
    (data / "land-cover-grid" / "2026-09-24.0.parquet").write_bytes(b"")

    from importlib import resources

    monkeypatch.setattr(
        resources, "files", lambda _pkg: tmp_path, raising=True
    )

    # manifests and the grid have the newer release; the geocode index does
    # not — so the newer one is not claimed.
    assert release.bundled_artifact_release() == "2026-07-22.0"
