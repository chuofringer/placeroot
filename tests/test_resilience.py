"""Issue #5 (degrade gracefully) and the #8 cache-as-fallback integration."""

import duckdb
import pytest

from placeroot import overture, server

from .conftest import CENTER_LAT, CENTER_LON, FIXTURE_PATH


def _fixture_missing(tmp_path, column: str):
    """A copy of the fixture with `column` dropped, for schema-probe tests."""
    out = tmp_path / f"missing_{column}.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * EXCLUDE ({column}) FROM read_parquet('{FIXTURE_PATH}')) "
        f"TO '{out}' (FORMAT PARQUET)"
    )
    return out


def test_find_places_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert result["error"] == "upstream_unavailable"
    assert result["retry_advised"] is True
    assert "detail" in result


def test_summarize_area_structured_error_on_unreachable_upstream(tmp_path):
    overture.set_data_path(str(tmp_path / "does-not-exist" / "*.parquet"))
    result = server.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert result["error"] == "upstream_unavailable"
    assert result["retry_advised"] is True


def test_missing_bbox_raises_schema_degraded_via_overture(tmp_path):
    degraded_fixture = _fixture_missing(tmp_path, "bbox")
    overture.set_data_path(str(degraded_fixture))
    with pytest.raises(overture.SchemaDegraded) as exc_info:
        overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert "bbox" in exc_info.value.missing


def test_missing_bbox_surfaces_as_structured_error_via_server(tmp_path):
    degraded_fixture = _fixture_missing(tmp_path, "bbox")
    overture.set_data_path(str(degraded_fixture))
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert result["error"] == "schema_degraded"
    assert "bbox" in result["missing_columns"]


def test_missing_confidence_degrades_gracefully_instead_of_failing(tmp_path):
    degraded_fixture = _fixture_missing(tmp_path, "confidence")
    overture.set_data_path(str(degraded_fixture))
    rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=5)
    assert rows  # tool still answers
    assert all(r["confidence"] is None for r in rows)
    assert overture.degraded_fields() == ["confidence"]


def test_missing_confidence_surfaces_degraded_fields_via_server(tmp_path):
    degraded_fixture = _fixture_missing(tmp_path, "confidence")
    overture.set_data_path(str(degraded_fixture))
    result = server.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=5)
    assert "error" not in result
    assert result["degraded_fields"] == ["confidence"]


def test_missing_basic_category_degrades_summarize_area(tmp_path):
    degraded_fixture = _fixture_missing(tmp_path, "basic_category")
    overture.set_data_path(str(degraded_fixture))
    result = overture.summarize_area(CENTER_LAT, CENTER_LON, radius_m=1000)
    assert result["top_categories"] == []
    assert result["uncategorized_count"] == result["total_places"]
    assert overture.degraded_fields() == ["basic_category"]


def test_serves_from_cache_when_upstream_goes_away(tmp_path, monkeypatch):
    """#5 + #8 integration: a warm cache keeps answering after upstream disappears."""
    monkeypatch.setenv("PLACEROOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("PLACEROOT_CACHE", raising=False)  # enabled (the default)

    upstream = tmp_path / "upstream.parquet"
    upstream.write_bytes(FIXTURE_PATH.read_bytes())
    overture.set_data_path(str(upstream))

    warm_rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    assert warm_rows

    upstream.unlink()  # upstream is now gone; cached tiles must still cover this query

    cold_rows = overture.find_places(CENTER_LAT, CENTER_LON, radius_m=1000, limit=10)
    assert cold_rows == warm_rows
