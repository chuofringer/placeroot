"""Tests for the data_version MCP tool (issue #120).

Covers all three resolution sources: env override, live discovery, and the
pinned fallback. Each case exercises release.resolve_release_info() through
the server.data_version() tool body and cross-checks that resolve_release()
(still used by every other consumer) delegates to the same cached value.

release.resolve_release()/resolve_release_info() are process-lifetime
lru_caches, so every case here resets them via release.reset_cache() after
mutating env/mocks (monkeypatch auto-undoes the mutation itself, but the
cache would otherwise keep serving a stale value into whichever test runs
next) and again on teardown via the autouse fixture below.
"""

import pytest

from placeroot import release, server


@pytest.fixture(autouse=True)
def clear_cache():
    release.reset_cache()
    yield
    release.reset_cache()


def test_data_version_env_override(monkeypatch):
    monkeypatch.setenv("PLACEROOT_OVERTURE_RELEASE", "2099-01-01.0")
    release.reset_cache()

    result = server.data_version()

    assert result["release"] == "2099-01-01.0"
    assert result["source"] == "env-override"
    assert result["release_date"] == "2099-01-01"
    assert release.resolve_release() == "2099-01-01.0"


def test_data_version_discovered(monkeypatch):
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: "2030-05-20.2")
    release.reset_cache()

    result = server.data_version()

    assert result["release"] == "2030-05-20.2"
    assert result["source"] == "discovered"
    assert result["release_date"] == "2030-05-20"
    assert release.resolve_release() == "2030-05-20.2"


def test_data_version_pinned_fallback(monkeypatch):
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: None)
    release.reset_cache()

    result = server.data_version()

    assert result["release"] == release.PINNED_RELEASE
    assert result["source"] == "pinned-fallback"
    assert result["release_date"] == release.PINNED_RELEASE.rsplit(".", 1)[0]
    assert release.resolve_release() == release.PINNED_RELEASE


def test_data_version_includes_note(monkeypatch):
    # Deterministic/offline: avoid a real network discovery call in tests.
    monkeypatch.delenv("PLACEROOT_OVERTURE_RELEASE", raising=False)
    monkeypatch.setattr(release, "_discover", lambda: None)
    release.reset_cache()

    result = server.data_version()

    assert "note" in result
    assert isinstance(result["note"], str) and result["note"]
