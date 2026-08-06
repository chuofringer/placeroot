"""Server-side switchover for issue #20: PLACEROOT_UPSTREAM_BASE re-points
every theme's glob at a mirror instead of the public Overture bucket, and
PLACEROOT_S3_ENDPOINT/PLACEROOT_S3_REGION configure the DuckDB connection for
a non-AWS S3-compatible target (R2/minio/self-hosted S3).

The autouse offline_data fixture (conftest.py) points every theme at a
fixture file via overture.set_data_path, which — correctly — takes
precedence over PLACEROOT_UPSTREAM_BASE (a fixture override is a stronger,
more specific statement of "read from here" than the base-URL env var).
Tests here clear that override for the theme under test to exercise the
live-glob construction path PLACEROOT_UPSTREAM_BASE actually affects.
"""

import duckdb
import pytest

from placeroot import overture, release


@pytest.fixture(autouse=True)
def _clear_places_override():
    """Undo conftest's fixture override for the places theme so
    _upstream_glob falls through to its live-glob construction, which is
    what PLACEROOT_UPSTREAM_BASE actually changes."""
    overture.set_data_path(None)
    yield
    # conftest's own teardown re-clears; nothing further needed here.


def test_default_upstream_base_is_the_public_overture_bucket(monkeypatch):
    monkeypatch.delenv("PLACEROOT_UPSTREAM_BASE", raising=False)
    glob = overture._upstream_glob("places", "place")
    assert glob == (
        f"s3://overturemaps-us-west-2/release/{release.PINNED_RELEASE}"
        "/theme=places/type=place/*"
    )


def test_upstream_base_env_var_replaces_the_bucket_root(monkeypatch):
    monkeypatch.setenv("PLACEROOT_UPSTREAM_BASE", "s3://my-mirror-bucket/overture")
    glob = overture._upstream_glob("places", "place")
    assert glob == (
        f"s3://my-mirror-bucket/overture/{release.PINNED_RELEASE}/theme=places/type=place/*"
    )


def test_upstream_base_env_var_applies_to_every_theme_type(monkeypatch):
    monkeypatch.setenv("PLACEROOT_UPSTREAM_BASE", "s3://my-mirror-bucket/overture")
    overture.set_data_path(None, theme="divisions")
    glob = overture._upstream_glob("divisions", "division_area")
    assert glob == (
        f"s3://my-mirror-bucket/overture/{release.PINNED_RELEASE}"
        "/theme=divisions/type=division_area/*"
    )


def test_upstream_base_env_var_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("PLACEROOT_UPSTREAM_BASE", "s3://my-mirror-bucket/overture/")
    glob = overture._upstream_glob("places", "place")
    assert glob == (
        f"s3://my-mirror-bucket/overture/{release.PINNED_RELEASE}/theme=places/type=place/*"
    )


def test_upstream_base_env_var_supports_local_directory_mirror(monkeypatch, tmp_path):
    monkeypatch.setenv("PLACEROOT_UPSTREAM_BASE", str(tmp_path))
    glob = overture._upstream_glob("places", "place")
    assert glob == f"{tmp_path}/{release.PINNED_RELEASE}/theme=places/type=place/*"


def test_fixture_override_still_wins_over_upstream_base(monkeypatch, tmp_path):
    """set_data_path (used by tests, and conceivably an operator pinning one
    theme to a specific dataset) is a stronger statement than the base-URL
    env var and must keep taking precedence — regression guard for the
    override-precedence order _upstream_glob documents."""
    monkeypatch.setenv("PLACEROOT_UPSTREAM_BASE", "s3://my-mirror-bucket/overture")
    fixture = tmp_path / "fixture.parquet"
    overture.set_data_path(str(fixture))
    assert overture._upstream_glob("places", "place") == str(fixture)


# --- Endpoint/region wiring into the DuckDB connection --------------------


def test_default_connection_uses_public_bucket_region_and_anonymous_access(monkeypatch):
    monkeypatch.delenv("PLACEROOT_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("PLACEROOT_S3_REGION", raising=False)
    con = overture._configure(duckdb.connect())
    assert con.execute("SELECT current_setting('s3_region')").fetchone()[0] == "us-west-2"
    assert con.execute("SELECT current_setting('s3_access_key_id')").fetchone()[0] == ""


def test_s3_region_env_var_reaches_the_connection(monkeypatch):
    monkeypatch.delenv("PLACEROOT_S3_ENDPOINT", raising=False)
    monkeypatch.setenv("PLACEROOT_S3_REGION", "auto")
    con = overture._configure(duckdb.connect())
    assert con.execute("SELECT current_setting('s3_region')").fetchone()[0] == "auto"


def test_s3_endpoint_env_var_reaches_the_connection(monkeypatch):
    monkeypatch.setenv("PLACEROOT_S3_ENDPOINT", "my-account.r2.cloudflarestorage.com")
    monkeypatch.delenv("PLACEROOT_S3_REGION", raising=False)
    con = overture._configure(duckdb.connect())
    endpoint = con.execute("SELECT current_setting('s3_endpoint')").fetchone()[0]
    assert endpoint == "my-account.r2.cloudflarestorage.com"
    url_style = con.execute("SELECT current_setting('s3_url_style')").fetchone()[0]
    assert url_style == "path"


def test_s3_access_keys_reach_the_connection_when_endpoint_is_set(monkeypatch):
    monkeypatch.setenv("PLACEROOT_S3_ENDPOINT", "my-account.r2.cloudflarestorage.com")
    monkeypatch.setenv("PLACEROOT_S3_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("PLACEROOT_S3_SECRET_ACCESS_KEY", "secretsecret")
    con = overture._configure(duckdb.connect())
    assert con.execute("SELECT current_setting('s3_access_key_id')").fetchone()[0] == "AKIDEXAMPLE"
    assert (
        con.execute("SELECT current_setting('s3_secret_access_key')").fetchone()[0]
        == "secretsecret"
    )


def test_no_endpoint_means_anonymous_even_with_custom_region(monkeypatch):
    """A plain AWS S3 target isn't PLACEROOT_S3_ENDPOINT territory, but the
    server's own read path never needs credentials — endpoint is what
    switches the connection out of the public bucket's anonymous mode."""
    monkeypatch.delenv("PLACEROOT_S3_ENDPOINT", raising=False)
    monkeypatch.setenv("PLACEROOT_S3_REGION", "eu-west-1")
    con = overture._configure(duckdb.connect())
    assert con.execute("SELECT current_setting('s3_access_key_id')").fetchone()[0] == ""
