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
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: "2026-08-01.0")
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
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: "2026-08-01.0")
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
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.delenv("PLACEROOT_RELEASE_TTL_HOURS", raising=False)
    calls = []
    monkeypatch.setattr(release, "_discover", lambda: calls.append(1) or "2026-08-01.0")
    assert release.resolve_release() == "2026-08-01.0"
    assert release.resolve_release() == "2026-08-01.0"
    assert len(calls) == 1  # second resolve served from the TTL cache


def test_expired_ttl_rolls_over_to_the_new_release(monkeypatch):
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "0")
    releases = iter(["2026-07-22.0", "2026-08-20.0"])
    monkeypatch.setattr(release, "_discover", lambda: next(releases))
    assert release.resolve_release() == "2026-07-22.0"
    assert release.resolve_release() == "2026-08-20.0"  # TTL 0: re-check every call


def test_failed_recheck_keeps_the_previous_release_not_the_pin(monkeypatch):
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setenv("PLACEROOT_RELEASE_TTL_HOURS", "0")
    answers = iter(["2026-08-01.0", None, None])
    monkeypatch.setattr(release, "_discover", lambda: next(answers))
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


# --- #219: staleness --------------------------------------------------------


def test_age_days_reads_the_date_in_the_release_name():
    import datetime

    old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    assert release.age_days(f"{old}.0") == 90
    assert release.age_days("garbage") is None


def test_is_stale_past_the_threshold(monkeypatch):
    import datetime

    monkeypatch.delenv("PLACEROOT_STALE_RELEASE_DAYS", raising=False)
    fresh = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    assert release.is_stale(f"{fresh}.0") is False
    assert release.is_stale(f"{old}.0") is True
    monkeypatch.setenv("PLACEROOT_STALE_RELEASE_DAYS", "100")
    assert release.is_stale(f"{old}.0") is False


def test_data_version_payload_carries_age_and_stale_flag(monkeypatch):
    import datetime

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
